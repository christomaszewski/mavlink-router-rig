# Copyright 2026 Chris Tomaszewski
# SPDX-License-Identifier: Apache-2.0
"""The service deployed the way a vehicle deploys it: through the real `rig` CLI, from a
deployment tree (vehicle.yaml + services.yaml + config/infra/<name>.yaml).

Needs a checkout of rig; point RIG_CLI at its `rig` script:

    RIG_CLI=~/ws/rig/rig python3 -m unittest discover -s tests/functional -p 'test_rig_deploy.py'

Every test builds its own throwaway deployment (unique vehicle id, so unique compose projects)
and its own RIG_HOME, so nothing of the operator's rig state is read or written.
"""
import itertools
import json
import os
from pathlib import Path
import shutil
import sys
import time
import unittest

import mav
from harness import IMAGE, REPO, RouterCase, docker, ports, run

try:                    # the host half needs PyYAML; the peer container imports this module too
    import yaml
    RIGGING = yaml.safe_load((REPO / "rigging.yaml").read_text())
except ImportError:
    yaml = RIGGING = None

RIG_CLI = os.environ.get("RIG_CLI")
_IDS = itertools.count(1)


def net_config(p, **extra):
    return {
        "general": {"tcp_server_port": p.server},
        "udp_endpoints": [
            {"name": "a", "mode": "server", "address": "127.0.0.1", "port": p.a},
            {"name": "b", "mode": "server", "address": "127.0.0.1", "port": p.b}],
        **extra,
    }


@unittest.skipUnless(RIG_CLI, "set RIG_CLI to the `rig` script of a rig checkout")
class RigDeployTests(RouterCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(Path(RIG_CLI).is_file(), f"RIG_CLI={RIG_CLI} is not a file")
        self.vehicle_id = f"t{os.getpid()}n{next(_IDS)}"
        self.root = self.workdir / "deploy"
        self.instances = []
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("RIG_", "COMPOSE_", "ROS_", "RMW_"))}
        env.update({
            "RIG_HOME": str(self.workdir / "rig-home"),
            "RIG_VEHICLE_LOCAL": str(self.workdir / "no-vehicle-local.yaml"),
            "MAVROUTER_PYTHON": sys.executable,          # the launcher's interpreter (PyYAML)
            "MAVROUTER_IMAGE": IMAGE,
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}",
        })
        self.env = env
        self.addCleanup(self.remove_containers)

    # ---- helpers ------------------------------------------------------------------------------------

    def rig(self, *argv, root=None, check=True):
        return run(sys.executable, RIG_CLI, "--root", root or self.root, *argv, env=self.env,
                   check=check, timeout=300)

    def project(self, instance):
        return f"{instance}-vehicle-{self.vehicle_id}"

    def remove_containers(self):
        for instance in self.instances:
            found = docker("ps", "-aq", "--filter",
                           f"label=com.docker.compose.project={self.project(instance)}",
                           check=False).stdout.split()
            if found:
                docker("rm", "-f", *found, check=False)
            staged = REPO / "var" / "run" / f"{instance}.conf"
            if staged.exists():
                staged.unlink()

    def instance_name(self, label):
        """A name no real instance uses: the launcher stages <name>.conf in the service repo."""
        name = f"{label}{self.vehicle_id}"
        self.instances.append(name)
        return name

    def deployment(self, configs, service=REPO, data_dir=None, images=None):
        """Write a deployment tree with one infra row per {instance name: router config}."""
        (self.root / "config" / "infra").mkdir(parents=True, exist_ok=True)
        rows = []
        for index, (name, config) in enumerate(configs.items()):
            path = self.root / "config" / "infra" / f"{name}.yaml"
            path.write_text(yaml.safe_dump({"service": "mavlink-router", "name": name, **config},
                                           sort_keys=False))
            rows.append({"name": name, "service": "mavlink-router",
                         "config": f"config/infra/{name}.yaml", "order": 10 * (index + 1)})
        vehicle = {"vehicle": "e2e", "vehicle_id": self.vehicle_id,
                   # default: no registry — the image in the local store
                   "images": images or {"registry": "", "tag": ""},
                   "run_capture": False, "infra": rows}
        if data_dir:
            vehicle["data_dir"] = str(data_dir)
        (self.root / "vehicle.yaml").write_text(yaml.safe_dump(vehicle, sort_keys=False))
        (self.root / "services.yaml").write_text(yaml.safe_dump(
            {"services": {"mavlink-router": {"path": str(service)}}}))

    def service_copy(self):
        """What a deployment has of this repo once vendored: descriptor, launch surface, examples."""
        target = self.workdir / "mavlink-router-rig"
        for rel in ["rigging.yaml", *RIGGING["launch_surface"], *RIGGING["examples"]]:
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, target / rel)
        return target

    def status(self):
        report = json.loads(self.rig("status", "--format", "json").stdout)
        return report, {stack["sensor"]: stack for stack in report["stacks"]}

    def wait_status(self, instance, **wanted):
        deadline = time.monotonic() + 90
        while True:
            _, stacks = self.status()
            if all(stacks[instance].get(key) == value for key, value in wanted.items()):
                return stacks[instance]
            if time.monotonic() >= deadline:
                self.fail(f"rig status never reported {wanted} for {instance}: {stacks[instance]}")
            time.sleep(1)

    def container(self, instance):
        return docker("ps", "-q", "--filter",
                      f"label=com.docker.compose.project={self.project(instance)}").stdout.strip()

    def router_argv(self, instance):
        return docker("exec", self.container(instance), "sh", "-c",
                      "tr '\\0' ' ' < /proc/1/cmdline").stdout

    # ---- tests ----------------------------------------------------------------------------------------

    def test_up_status_logs_down_with_flight_logs_in_the_rig_run(self):
        p = ports("server a b")
        name = self.instance_name("fcu")
        data = self.data_dir()
        self.deployment({name: net_config(p, logging={"enabled": True, "telemetry": True})},
                        data_dir=data)
        self.rig("doctor")
        _, stacks = self.status()
        self.assertEqual((stacks[name]["service"], stacks[name]["state"]),
                         ("mavlink-router", "down"))

        self.rig("up")
        self.wait_status(name, state="running", health="healthy")
        container = self.container(name)
        self.assertTrue(container, "rig up started no container under its project name")

        # rig opened a run, and the router logs into it
        report, _ = self.status()
        first_run = os.readlink(data / "current")
        self.assertRegex(first_run, r"^runs/\d{8}T\d{6}Z_")
        self.assertTrue(report["run"], "rig status reports no open run")
        in_run = f"/data/{first_run}/mavlink-router/{name}"
        self.assertIn(f"--log {in_run}", self.router_argv(name))
        tree = self.peers(flight, a=p.a, b=p.b, data=data)
        self.assertEqual(list(tree), [in_run])

        self.assertIn("Opened TCP Server", self.rig("logs", name, "--tail", "100").stdout
                      + self.rig("logs", name, "--tail", "100").stderr)
        rendered = self.rig("config", name)
        self.assertIn(f"image: {IMAGE}", rendered.stdout)

        # a second `up` changes nothing; down with --end-run seals the run
        self.rig("up")
        self.assertEqual(self.container(name), container)
        self.rig("down", "--end-run")
        self.assertEqual(self.container(name), "")
        self.assertFalse(os.path.lexists(data / "current"), "the run was not sealed")

        # the next up opens the next run, and the logs follow it
        time.sleep(1.1)                                   # run ids have one-second resolution
        self.rig("up")
        self.wait_status(name, state="running", health="healthy")
        second_run = os.readlink(data / "current")
        self.assertNotEqual(second_run, first_run)
        tree = self.peers(flight, a=p.a, b=p.b, data=data)
        self.assertEqual(sorted(tree), sorted([in_run, f"/data/{second_run}/mavlink-router/{name}"]))
        self.assertEqual(len(tree[in_run]), 1, "the sealed run's log was touched")
        self.rig("down")
        self.assertEqual(self.container(name), "")

    def test_deploys_the_image_from_the_fleet_registry(self):
        """The production path: vehicle.yaml names a registry and a tag, and the vehicle runs
        <registry>/mavlink_router:<tag> — what `rig build` pushed — pulled on demand."""
        p = ports("server a b registry")
        registry = f"127.0.0.1:{p.registry}"
        ref = f"{registry}/mavlink_router:e2e-{self.vehicle_id}"
        server = f"mavrouter-test-registry-{self.vehicle_id}"
        docker("run", "-d", "--name", server, "-p", f"{registry}:5000", "registry:2")
        self.addCleanup(docker, "rm", "-f", "-v", server, check=False)
        self.addCleanup(docker, "image", "rm", ref, check=False)
        docker("tag", IMAGE, ref)
        for _ in range(20):                       # the registry needs a moment to listen
            if docker("push", ref, check=False).returncode == 0:
                break
            time.sleep(0.5)
        else:
            self.fail("could not push to the throwaway registry")
        docker("image", "rm", ref)                # only the registry has it under that name now

        name = self.instance_name("fcu")
        self.deployment({name: net_config(p)},
                        images={"registry": registry, "tag": f"e2e-{self.vehicle_id}"})
        del self.env["MAVROUTER_IMAGE"]           # no per-service override: the fleet policy
        self.assertIn(f"image: {ref}", self.rig("config", name).stdout)
        self.rig("pull")
        self.rig("up")
        self.wait_status(name, state="running", health="healthy")
        self.assertEqual(docker("inspect", "-f", "{{.Config.Image}}",
                                self.container(name)).stdout.strip(), ref)
        self.peers(two_peers, a=p.a, b=p.b)
        self.rig("down")

    def test_doctor_refuses_two_instances_on_one_port(self):
        p1, p2 = ports("server a b"), ports("server a b")
        one, two = self.instance_name("fcu"), self.instance_name("payload")
        clash = net_config(p2)
        clash["udp_endpoints"][0]["port"] = p1.a          # the first server-mode UDP port
        self.deployment({one: net_config(p1), two: clash})
        result = self.rig("doctor", check=False)
        self.assertNotEqual(result.returncode, 0, "doctor accepted a host-port clash")
        self.assertIn(str(p1.a), result.stdout + result.stderr)
        result = self.rig("up", check=False)
        self.assertNotEqual(result.returncode, 0, "up went ahead despite the clash")
        self.assertEqual(self.container(one) + self.container(two), "")

        clash = net_config(p2)
        clash["general"]["tcp_server_port"] = p1.server   # the TCP server port
        self.deployment({one: net_config(p1), two: clash})
        result = self.rig("doctor", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(p1.server), result.stdout + result.stderr)

        self.deployment({one: net_config(p1), two: net_config(p2)})
        self.rig("doctor")
        self.rig("up")
        self.wait_status(one, state="running")
        self.wait_status(two, state="running")
        self.peers(two_routers, one_a=p1.a, one_b=p1.b, two_a=p2.a, two_b=p2.b)
        self.rig("down", one)
        self.assertEqual(self.container(one), "")
        self.assertTrue(self.container(two), "taking one instance down took the other with it")
        self.rig("down")

    def test_vendored_launch_surface_is_enough_to_deploy(self):
        p = ports("server a b")
        name = self.instance_name("fcu")
        self.deployment({name: net_config(p)})
        self.rig("vendor", "mavlink-router")
        vendored = self.root / "services" / "mavlink-router"
        shipped = sorted(str(path.relative_to(vendored)) for path in vendored.rglob("*")
                         if path.is_file() and path.name != ".vendored.yaml")
        self.assertEqual(shipped, sorted(["rigging.yaml", *RIGGING["launch_surface"],
                                          *RIGGING["examples"]]))
        # deploy from the vendored copy alone — no checkout of this repo involved
        self.deployment({name: net_config(p)}, service="services/mavlink-router")
        self.rig("doctor")
        self.rig("up")
        self.wait_status(name, state="running", health="healthy")
        self.assertTrue((vendored / "var" / "run" / f"{name}.conf").is_file())
        self.peers(two_peers, a=p.a, b=p.b)
        self.rig("down")
        self.assertEqual(self.container(name), "")

    def scaffold(self, name, *extra):
        """A deployment as `rig init` writes it (rig add only edits that generated form)."""
        target = self.workdir / name
        self.rig("init", target, "--vehicle-id", "7", "--no-git", *extra, root=self.workdir)
        return target

    def infra_rows(self, root):
        rows = yaml.safe_load((root / "vehicle.yaml").read_text())["infra"] or []
        return [(row["name"], row["service"], row["config"], row.get("enabled", True))
                for row in rows]

    def test_rig_init_and_add_wire_the_example_config(self):
        """The two ways a deployment gets this service: `rig init --infra` and `rig add`. Both
        hand the operator the example config as the instance config, enabled."""
        service = self.service_copy()
        example = (REPO / RIGGING["examples"][0]).read_text()
        wired = [("fcu", "mavlink-router", "config/infra/fcu.yaml", True)]

        fresh = self.scaffold("by-init", "--infra", service)
        added = self.scaffold("by-add")
        self.rig("add", service, root=added)
        for root in (fresh, added):
            self.assertEqual((root / "config" / "infra" / "fcu.yaml").read_text(), example)
            self.assertEqual(self.infra_rows(root), wired)
            routes = yaml.safe_load((root / "services.yaml").read_text())["services"]
            self.assertEqual((root / routes["mavlink-router"]["path"]).resolve(),
                             service.resolve())
            self.rig("doctor", root=root)
            # the example is a complete instance: rig renders its compose, serial device and all
            rendered = self.rig("config", "fcu", root=root).stdout
            self.assertIn("name: fcu-vehicle-7", rendered)
            self.assertIn("/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0",
                          rendered)
            dry = self.rig("up", "--dry-run", root=root)
            self.assertIn("mavlink-router-up", dry.stdout + dry.stderr)

    def test_rig_add_under_another_name(self):
        """A second router on a vehicle: `rig add --as` copies the example as a nameless profile
        and the vehicle.yaml row names the instance."""
        service = self.service_copy()
        root = self.scaffold("renamed")
        self.rig("add", service, "--as", "payload", root=root)
        config = yaml.safe_load((root / "config" / "infra" / "payload.yaml").read_text())
        self.assertNotIn("name", config, "the example's own name must not survive --as")
        self.assertEqual(config["service"], "mavlink-router")
        self.assertEqual(self.infra_rows(root),
                         [("payload", "mavlink-router", "config/infra/payload.yaml", True)])
        self.rig("doctor", root=root)
        self.assertIn("name: payload-vehicle-7", self.rig("config", "payload", root=root).stdout)


# ---- peer halves ------------------------------------------------------------------------------------

def _exchange(a_port, b_port):
    a = mav.UdpPeer(1, mav.COMP_AUTOPILOT, "a", router=a_port)
    b = mav.UdpPeer(255, 190, "b", router=b_port)
    mav.introduce(a, b)
    for counter in range(5):
        b.expect(a.send(mav.system_time(counter)))
        a.expect(b.send(mav.system_time(100 + counter)))
    return a, b


def two_peers(p):
    _exchange(p.a, p.b)


def two_routers(p):
    _exchange(p.one_a, p.one_b)
    _exchange(p.two_a, p.two_b)


def flight(p):
    """Telemetry from an autopilot, then the tree of log files under the data dir."""
    import time
    from test_logging import _tlog, _tree
    a, b = _exchange(p.a, p.b)
    last = a.send(mav.system_time(999))
    b.expect(last)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        tree = _tree()
        if any(last in [raw for _, raw in _tlog(f"{directory}/{name}")]
               for directory, names in tree.items() for name in names):
            return tree
        time.sleep(0.1)
    raise AssertionError(f"the telemetry never reached a log file: {_tree()}")


if __name__ == "__main__":
    unittest.main()
