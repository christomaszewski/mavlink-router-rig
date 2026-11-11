# Copyright 2026 Chris Tomaszewski
# SPDX-License-Identifier: Apache-2.0
"""Harness for the functional tests: a real router container, started by the real launcher from a
rig config, with real MAVLink peers on its endpoints.

Two halves, because of `network_mode: host`:

  * the HOST half (unittest) writes an instance config, runs `mavlink-router-up <config> up -d`
    and waits for the image's own health probe;
  * the PEER half is a plain function in the same test module, run by `RouterCase.peers()` inside
    a throwaway container that shares the router's network namespace (`--network host`). The
    router's sockets live in the Docker host's namespace, which on Docker Desktop is the VM, not
    the machine running the tests — peers started there would talk to a different loopback.

The peer container runs the PEER IMAGE: the image under test plus python3 (`apk add`), committed
once per image id. It also carries the router binary and entrypoint, which allows a second mode,
`RouterCase.embedded()`: no router container at all — the peer function starts the router itself
(mav.LocalRouter) in its own container. That is how the serial tests give the router a
pseudo-terminal as its UART, and how the IPv6 test gets a loopback that works everywhere.

Environment:
  MAVROUTER_IMAGE              image under test (default mavlink_router:latest)
  MAVROUTER_TEST_PORT_BASE     first of the host ports the tests bind (default 25600; one run
                               uses fewer than 1000 above it)
  MAVROUTER_TEST_KEEP=1        leave containers, configs and data dirs behind for inspection
"""
import functools
import importlib
import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent     # (in the peer container HERE is /tests and this is unused)
LAUNCHER = REPO / "mavlink-router-up"
RENDER = REPO / "tools" / "render_conf.py"
IMAGE = os.environ.get("MAVROUTER_IMAGE", "mavlink_router:latest")
KEEP = os.environ.get("MAVROUTER_TEST_KEEP") == "1"
_PORTS = itertools.count(int(os.environ.get("MAVROUTER_TEST_PORT_BASE", "25600")))
_NAMES = itertools.count(1)
# rig-owned variables a surrounding session may have exported: a test run is standalone.
_RIG_ENV = ("RIG_RUNTIME_IMAGE_PINS", "RIG_IMAGE_REGISTRY", "RIG_IMAGE_TAG", "RIG_IMAGE_PINS",
            "RIG_DATA_DIR", "COMPOSE_PROJECT_NAME")


def run(*argv, check=True, env=None, timeout=180, **kwargs):
    result = subprocess.run([str(arg) for arg in argv], capture_output=True, text=True, env=env,
                            timeout=timeout, **kwargs)
    if check and result.returncode != 0:
        raise AssertionError(f"{' '.join(str(arg) for arg in argv)} failed "
                             f"({result.returncode}):\n{result.stdout}{result.stderr}")
    return result


def docker(*argv, **kwargs):
    return run("docker", *argv, **kwargs)


@functools.lru_cache(maxsize=None)
def image_inspect(ref=IMAGE):
    result = docker("image", "inspect", ref, check=False)
    if result.returncode != 0:
        raise AssertionError(
            f"image '{ref}' is not in the local store. Build it first, e.g.\n"
            "    docker build -f docker/Dockerfile.runtime -t mavlink_router:latest .\n"
            "or point MAVROUTER_IMAGE at the image to test.")
    return json.loads(result.stdout)[0]


def image_is_fork():
    """Whether the image under test was built from the fork pin (it records its source)."""
    source = image_inspect()["Config"]["Labels"].get("org.opencontainers.image.source", "")
    return "christomaszewski" in source


@functools.lru_cache(maxsize=None)
def peer_image():
    """The image under test + python3, built once per image id (see the module docstring)."""
    base = image_inspect()["Id"]
    tag = f"mavlink-router-test-peer:{base.split(':')[-1][:12]}"
    if docker("image", "inspect", tag, check=False).returncode == 0:
        return tag
    name = f"mavlink-router-test-peer-build-{os.getpid()}"
    try:
        docker("run", "--name", name, "--entrypoint", "apk", IMAGE, "add", "--no-cache", "python3")
        docker("commit", "--change", 'ENTRYPOINT ["python3"]', "--change", "HEALTHCHECK NONE",
               name, tag)
    finally:
        docker("rm", "-f", name, check=False)
    return tag


def ports(names):
    """Fresh host ports, one per whitespace-separated name. Never reused within a run: a router
    that just went down may leave its TCP port in TIME_WAIT."""
    return types.SimpleNamespace(**{name: next(_PORTS) for name in names.split()})


def launcher_env(extra=None):
    env = {key: value for key, value in os.environ.items() if key not in _RIG_ENV}
    env["MAVROUTER_IMAGE"] = IMAGE
    env.update(extra or {})
    return env


class Router:
    """One service instance, driven only through `mavlink-router-up`."""

    def __init__(self, case, config, name=None, env=None):
        self.case = case
        self.name = name or f"t{os.getpid()}n{next(_NAMES)}"
        self.config = {"service": "mavlink-router", "name": self.name, **config}
        self.path = case.workdir / f"{self.name}.yaml"
        self.env = launcher_env(env)
        self.write()

    def write(self, config=None):
        import yaml
        if config is not None:
            self.config = {"service": "mavlink-router", "name": self.name, **config}
        self.path.write_text(yaml.safe_dump(self.config, sort_keys=False))

    def launch(self, *verb, check=True):
        return run(LAUNCHER, self.path, *verb, env=self.env, check=check)

    def up(self, wait=True):
        self.launch("up", "-d")
        if wait:
            self.wait_healthy()
        return self.container()

    def down(self):
        self.launch("down", "--timeout", "5", check=False)

    def container(self):
        return self.launch("ps", "-q").stdout.strip()

    def inspect(self, template, container=None):
        return docker("inspect", "-f", template, container or self.container()).stdout.strip()

    def wait_healthy(self, timeout=20.0):
        """The image's own probe, retried while the router binds its ports."""
        container = self.container()
        self.case.assertTrue(container, "the launcher started no container")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if docker("exec", container, "/usr/local/bin/healthcheck.sh", check=False,
                      ).returncode == 0:
                return container
            time.sleep(0.2)
        self.case.fail(f"router '{self.name}' never became healthy:\n{self.logs()}")

    def logs(self):
        result = self.launch("logs", "--no-color", check=False)
        return result.stdout + result.stderr

    def argv(self):
        """The command line of the router process (pid 1 in the container)."""
        return docker("exec", self.container(), "sh", "-c",
                      "tr '\\0' ' ' < /proc/1/cmdline").stdout.strip()

    def conf(self):
        """The main.conf the running router was started on."""
        return docker("exec", self.container(), "cat", "/etc/mavlink-router/main.conf").stdout

    def cleanup(self):
        if KEEP:
            return
        self.down()
        staged = REPO / "var" / "run" / f"{self.name}.conf"
        if staged.exists():
            staged.unlink()


class RouterCase(unittest.TestCase):
    """Base class: per-test working directory, routers that are taken down afterwards, and
    `peers()` to run the peer half of a test next to the router."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        image_inspect()  # fail every test of the class with one clear message, not a skip

    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp(prefix="mavrouter-test."))
        self.addCleanup(self._remove_workdir)
        self.routers = []

    def _remove_workdir(self):
        if KEEP:
            sys.stderr.write(f"\nkept: {self.workdir}\n")
            return
        # the router wrote its logs as root: remove them from a container first
        docker("run", "--rm", "--entrypoint", "/bin/sh", "-v", f"{self.workdir}:/work", IMAGE,
               "-c", "rm -rf /work/data", check=False)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def router(self, config, up=True, **kwargs):
        router = Router(self, config, **kwargs)
        self.routers.append(router)
        self.addCleanup(router.cleanup)
        if up:
            router.up()
        return router

    def data_dir(self, open_run=None):
        """A fleet data dir for the logging overlay; `open_run` creates runs/<id> and points
        `current` at it, the way rig's run registry does."""
        data = self.workdir / "data"
        data.mkdir(exist_ok=True)
        if open_run:
            self.open_run(open_run)
        return data

    def open_run(self, run_id):
        data = self.workdir / "data"
        (data / "runs" / run_id).mkdir(parents=True, exist_ok=True)
        link = data / "current"
        if link.is_symlink():
            link.unlink()
        link.symlink_to(f"runs/{run_id}")

    def peers(self, function, args=None, data=None, conf=None, host_network=True, timeout=120,
              **extra):
        """Run `function(p)` — a module-level function of the calling test module — in the peer
        container. `args` (a ports() namespace or a dict) plus `extra` arrive as attributes of
        `p`; what the function returns (JSON-able) comes back. `data` mounts a data dir at /data,
        `conf` a rendered main.conf where the entrypoint looks for it."""
        payload = dict(vars(args) if isinstance(args, types.SimpleNamespace) else args or {})
        payload.update(extra)
        options = ["-v", f"{HERE}:/tests:ro"]
        if host_network:
            options += ["--network", "host"]
        if data:
            options += ["-v", f"{data}:/data"]
        if conf:
            options += ["-v", f"{conf}:/etc/mavlink-router/main.conf:ro"]
        result = docker("run", "--rm", *options, peer_image(),
                        "/tests/harness.py", function.__module__, function.__name__,
                        json.dumps(payload), check=False, timeout=timeout)
        if result.returncode != 0:
            logs = "".join(f"\n--- router {router.name} ---\n{router.logs()}"
                           for router in self.routers)
            self.fail(f"{function.__name__} failed in the peer container:\n"
                      f"{result.stdout}{result.stderr}{logs}")
        lines = result.stdout.strip().splitlines()
        return json.loads(lines[-1]) if lines else None

    def embedded(self, function, config, args=None, **kwargs):
        """Like peers(), but with NO router container: `config` is rendered and mounted as
        main.conf, and the peer function starts the router itself (mav.LocalRouter) inside the
        peer container, which keeps its own network namespace."""
        return self.peers(function, args, conf=self.render(config), host_network=False, **kwargs)

    def render(self, config, name="embedded"):
        """Render a config to a main.conf file with the launcher's renderer (for the tests that
        start the router themselves instead of through the launcher)."""
        import yaml
        source = self.workdir / f"{name}.yaml"
        source.write_text(yaml.safe_dump({"service": "mavlink-router", "name": name, **config},
                                         sort_keys=False))
        target = self.workdir / f"{name}.conf"
        target.write_text(run(sys.executable, RENDER, source).stdout)
        return target


def _main():
    """Peer-container entry: harness.py <module> <function> <json args>."""
    module, function, payload = sys.argv[1:4]
    result = getattr(importlib.import_module(module), function)(
        types.SimpleNamespace(**json.loads(payload)))
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    _main()
