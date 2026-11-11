# Copyright 2026 Chris Tomaszewski
# SPDX-License-Identifier: Apache-2.0
"""The service as an operator (or rig) drives it: up, status, logs, down; what a config edit does
to a running instance; several instances on one host; and what happens when the router dies or
cannot start."""
import json
import os
import time
import unittest

import mav
from harness import IMAGE, RouterCase, docker, image_inspect, ports


def net_config(p, **general):
    return {
        "general": {"tcp_server_port": p.server, **general},
        "udp_endpoints": [
            {"name": "a", "mode": "server", "address": "127.0.0.1", "port": p.a},
            {"name": "b", "mode": "server", "address": "127.0.0.1", "port": p.b}],
    }


class LifecycleTests(RouterCase):
    def wait(self, what, check, timeout=60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = check()
            if result:
                return result
            time.sleep(0.5)
        self.fail(f"timed out waiting for {what}")

    def test_docker_reports_the_container_healthy(self):
        """The image's HEALTHCHECK, as Docker (and through it `rig status`) sees it."""
        p = ports("server a b")
        router = self.router(net_config(p))
        self.wait("Docker to report the container healthy",
                  lambda: router.inspect("{{.State.Health.Status}}") == "healthy")
        self.assertEqual(router.inspect("{{.HostConfig.RestartPolicy.Name}}"), "unless-stopped")
        self.assertEqual(router.inspect("{{.HostConfig.NetworkMode}}"), "host")

    def test_status_and_logs(self):
        p = ports("server a b")
        router = self.router(net_config(p))
        rows = [json.loads(line) for line in
                router.launch("status", "--format", "json").stdout.splitlines() if line.strip()]
        self.assertEqual([(row["Service"], row["State"]) for row in rows], [("router", "running")])
        self.assertEqual(rows[0]["Project"], f"mavlink-router_{router.name}")
        self.assertIn(f"Opened TCP Server [", router.launch("logs").stdout)
        self.assertIn("mavlink-router version", router.launch("logs", "--tail", "50").stdout)
        router.down()
        self.assertEqual(router.container(), "")
        self.assertEqual(router.launch("down").returncode, 0, "down twice is not an error")

    def test_a_config_edit_replaces_the_container_and_nothing_else_does(self):
        p = ports("server a b moved")
        router = self.router(net_config(p))
        first = router.container()
        self.assertEqual(router.up(), first, "an unchanged config recreated the container")
        router.launch("status")
        router.launch("config")
        self.assertEqual(router.container(), first, "a read-only verb touched the container")

        edited = net_config(p)
        edited["udp_endpoints"][1]["port"] = p.moved
        router.write(edited)
        second = router.up()
        self.assertNotEqual(second, first, "the router kept running its old config")
        self.assertIn(f"Port = {p.moved}\n", router.conf())
        self.peers(traffic, a=p.a, b=p.moved)          # and it routes on the new port

    def test_two_instances_side_by_side(self):
        p1, p2 = ports("server a b"), ports("server a b")
        one, two = self.router(net_config(p1)), self.router(net_config(p2))
        self.assertNotEqual(one.container(), two.container())
        for router in (one, two):
            self.assertEqual(router.inspect('{{index .Config.Labels "com.docker.compose.project"}}'),
                             f"mavlink-router_{router.name}")
        # separate MAVLink networks: nothing crosses from one router to the other
        self.peers(instances_are_separate, one_a=p1.a, one_b=p1.b, two_a=p2.a, two_b=p2.b)
        one.down()
        self.assertEqual(one.container(), "")
        self.assertEqual(two.inspect("{{.State.Running}}"), "true")
        self.peers(traffic, a=p2.a, b=p2.b)

    def test_inherited_project_name_is_honoured(self):
        """rig exports COMPOSE_PROJECT_NAME per instance; the launcher must not override it."""
        p = ports("server a b")
        project = f"fcu-vehicle-t{os.getpid()}"
        router = self.router(net_config(p), env={"COMPOSE_PROJECT_NAME": project})
        self.assertEqual(router.inspect('{{index .Config.Labels "com.docker.compose.project"}}'),
                         project)
        self.assertEqual(router.inspect("{{.Name}}"), f"/{project}-router-1")
        router.down()
        self.assertEqual(docker("ps", "-aq", "--filter",
                                f"label=com.docker.compose.project={project}").stdout.strip(), "")

    def test_pinned_up_runs_exactly_the_image_rig_validated(self):
        """A rig replay/simulation session exports RIG_RUNTIME_IMAGE_PINS (image ref -> image
        ID): `up` must start that very image ID, and refuse a pin set that is not its own."""
        p = ports("server a b")
        image_id = image_inspect()["Id"]
        pins = json.dumps({"version": 1, "images": {IMAGE: image_id}})
        router = self.router(net_config(p), env={"RIG_RUNTIME_IMAGE_PINS": pins})
        self.assertEqual(router.inspect("{{.Image}}"), image_id)
        self.assertEqual(router.inspect("{{.Config.Image}}"), image_id)
        self.peers(traffic, a=p.a, b=p.b)

        stale = json.dumps({"version": 1, "images": {"some/other:image": image_id}})
        other = self.router(net_config(ports("server a b")), up=False,
                            env={"RIG_RUNTIME_IMAGE_PINS": stale})
        result = other.launch("up", "-d", check=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("runtime image selection changed", result.stderr)
        self.assertEqual(other.container(), "")

    def test_router_is_restarted_when_it_exits(self):
        p = ports("server a b")
        router = self.router(net_config(p))
        container = router.container()
        self.peers(traffic, a=p.a, b=p.b)
        docker("exec", container, "kill", "-TERM", "1")       # the router process goes away
        self.wait("Docker to restart the container",
                  lambda: router.inspect("{{.RestartCount}}", container) == "1", timeout=30)
        router.wait_healthy()
        self.assertEqual(router.container(), container, "restarted in place, not recreated")
        self.peers(traffic, a=p.a, b=p.b)

    def test_missing_serial_device_fails_up_by_name(self):
        p = ports("server")
        device = f"/dev/serial/by-id/usb-not-plugged-in-{os.getpid()}"
        router = self.router({
            "general": {"tcp_server_port": p.server},
            "uart_endpoints": [{"name": "fcu", "device": device, "baud": 57600}],
        }, up=False)
        result = router.launch("up", "-d", check=False)
        self.assertNotEqual(result.returncode, 0, "up succeeded without the serial device")
        self.assertIn(device, result.stdout + result.stderr)
        running = docker("ps", "-q", "--filter",
                         f"label=com.docker.compose.project=mavlink-router_{router.name}")
        self.assertEqual(running.stdout.strip(), "")

    def test_udp_port_clash_is_a_restart_loop_with_a_clear_log(self):
        """Two instances configured onto one host port: the second cannot bind. `rig doctor`
        catches this before `up` (rigging.yaml host_ports); standalone, this is what it looks
        like — the container never turns healthy, and its log names the address."""
        p1, p2 = ports("server a b"), ports("server a b")
        self.router(net_config(p1))
        clash = net_config(p2)
        clash["udp_endpoints"][0]["port"] = p1.a
        second = self.router(clash, up=False)
        second.launch("up", "-d")
        container = second.container()
        self.wait("the second router to fail and be restarted",
                  lambda: int(second.inspect("{{.RestartCount}}", container)) >= 1, timeout=30)
        self.assertRegex(second.logs(), rf"Error binding IPv4 socket for 127\.0\.0\.1:{p1.a}")


# ---- peer halves ------------------------------------------------------------------------------------

def traffic(p):
    a = mav.UdpPeer(1, 1, "a", router=p.a)
    b = mav.UdpPeer(255, 190, "b", router=p.b)
    mav.introduce(a, b)
    for counter in range(5):
        b.expect(a.send(mav.system_time(counter)))
        a.expect(b.send(mav.system_time(100 + counter)))


def instances_are_separate(p):
    one_a = mav.UdpPeer(1, 1, "router one, a", router=p.one_a)
    one_b = mav.UdpPeer(255, 190, "router one, b", router=p.one_b)
    two_a = mav.UdpPeer(2, 1, "router two, a", router=p.two_a)
    two_b = mav.UdpPeer(254, 190, "router two, b", router=p.two_b)
    mav.introduce(one_a, one_b)
    mav.introduce(two_a, two_b)
    one_b.expect(one_a.send(mav.system_time(1)))
    two_b.expect(two_a.send(mav.system_time(2)))
    strangers = [frame for peer, own in ((one_a, 255), (one_b, 1), (two_a, 254), (two_b, 2))
                 for frame in peer.seen + peer.collect(0.3) if frame.sysid != own]
    assert not strangers, f"traffic crossed between the two routers: {mav.listing(strangers)}"


if __name__ == "__main__":
    unittest.main()
