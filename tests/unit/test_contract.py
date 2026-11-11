#!/usr/bin/env python3
# Copyright 2026 Chris Tomaszewski
# SPDX-License-Identifier: Apache-2.0
"""The repo's rig-facing surface, checked without rig and without a Docker daemon: the pins that
are written down more than once agree, the launch surface is complete, the build entrypoint
turns rig's build env into the right docker invocations, and the launcher's image-plan verbs
answer in the shapes rig parses.

    python3 -m unittest discover -s tests/unit -v

(`rig certify --repo . --require-build-check` is the other half — rig's own view of the same
contract; CI runs both.)
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
RIGGING = yaml.safe_load((ROOT / "rigging.yaml").read_text())
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = "sha256:" + "a" * 64

# A `docker` that records its argv and answers a buildx build's --metadata-file like BuildKit.
DOCKER_STUB = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if "--metadata-file" in args:
    with open(args[args.index("--metadata-file") + 1], "w") as meta:
        json.dump({{"containerimage.digest": "{DIGEST}"}}, meta)
"""


def load_renderer():
    spec = importlib.util.spec_from_file_location("render_conf", ROOT / "tools" / "render_conf.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pin(name):
    """(url, version) of sources/<name>.repos — parsed as YAML, unlike the build script's awk."""
    data = yaml.safe_load((ROOT / "sources" / f"{name}.repos").read_text())
    repo = data["repositories"]["mavlink-router"]
    return repo["url"], repo["version"]


def pin_names():
    return sorted(path.stem for path in (ROOT / "sources").glob("*.repos"))


class PinTests(unittest.TestCase):
    def test_pins_are_exact_commits(self):
        self.assertEqual(pin_names(), ["fork", "upstream"])
        for name in pin_names():
            url, version = pin(name)
            self.assertTrue(url.startswith("https://") and url.endswith(".git"), url)
            self.assertRegex(version, SHA, f"sources/{name}.repos must pin a full commit SHA")

    def test_default_source_names_a_pin(self):
        self.assertIn((ROOT / "sources" / "DEFAULT").read_text().strip(), pin_names())

    def test_rigging_build_sources_repeat_the_pin_files(self):
        """rig mirrors/exports what the rigging declares; the build script builds what the pin
        file says. One pin bumped without the other is a build that fetches the wrong rev."""
        declared = {s["name"]: (s["repo"], s["ref"]) for s in RIGGING["build"]["sources"]}
        self.assertEqual(declared, {name: pin(name) for name in pin_names()})

    def test_dockerfile_defaults_are_the_upstream_pin(self):
        text = (ROOT / "docker" / "Dockerfile.runtime").read_text()
        defaults = dict(re.findall(r"^ARG (MAVROUTER_(?:URL|REF))=(\S+)$", text, re.M))
        self.assertEqual((defaults["MAVROUTER_URL"], defaults["MAVROUTER_REF"]), pin("upstream"))

    def test_dockerfile_names_no_frontend_image(self):
        """A `# syntax=` line makes even a fully cached build resolve the frontend on Docker Hub,
        which `rig build --offline` cannot do."""
        first = (ROOT / "docker" / "Dockerfile.runtime").read_text().splitlines()[0]
        self.assertNotRegex(first, r"^#\s*syntax\s*=")


class DescriptorTests(unittest.TestCase):
    def test_launch_surface_is_complete(self):
        surface = RIGGING["launch_surface"]
        for rel in [*surface, *RIGGING["examples"], RIGGING["build"]["command"]]:
            self.assertTrue((ROOT / rel).is_file(), f"declared but missing: {rel}")
        self.assertIn(RIGGING["launcher"], surface)
        self.assertTrue(os.access(ROOT / RIGGING["launcher"], os.X_OK))
        self.assertTrue(os.access(ROOT / RIGGING["build"]["command"], os.X_OK))
        # Everything the launcher reaches for at run time must be vendored with it.
        launcher = (ROOT / RIGGING["launcher"]).read_text()
        referenced = set()
        for path, templated in re.findall(r'\$REPO/((?:docker|tools)/[\w./-]+)(\$slot[\w./-]*)?',
                                          launcher):
            if templated:  # compose.deploy.serial$slot.yaml: one overlay per further UART
                slots = re.search(r"for slot in ([\d ]+);", launcher).group(1).split()
                referenced |= {path + templated.replace("$slot", slot) for slot in slots}
            else:
                referenced.add(path)
        self.assertTrue(referenced)
        self.assertEqual(len([rel for rel in referenced if "serial" in rel]),
                         load_renderer()._MAX_UARTS, "one serial overlay per supported UART")
        self.assertLessEqual(referenced, set(surface), "launcher uses files outside launch_surface")
        # ...and the build context must never ride along onto the vehicle.
        self.assertFalse([rel for rel in surface if "Dockerfile" in rel or "build" in rel])

    def test_declared_images_match_the_compose_and_the_build(self):
        (image,) = RIGGING["build"]["images"]
        compose = (ROOT / "docker" / "compose" / "compose.deploy.yaml").read_text()
        override = RIGGING["images"]["override_env"][image]
        self.assertIn(f"${{{override}:-", compose)
        self.assertIn(f"/}}{image}:${{RIG_IMAGE_TAG:-latest}}", compose)
        self.assertIn(f'"${{REGISTRY}}/{image}:${{TAG}}"',
                      (ROOT / "tools" / "build_image.sh").read_text())

    def test_image_verbs_are_declared_with_their_helper(self):
        self.assertEqual(RIGGING["verbs"]["images"], "images")
        self.assertEqual(RIGGING["verbs"]["runtime-images"], "runtime-images")
        self.assertIn("tools/runtime_images.py", RIGGING["launch_surface"])


class BuildScriptTests(unittest.TestCase):
    """tools/build_image.sh against a recording docker stub, in a scratch copy of the repo."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo with spaces"
        for rel in ("tools/build_image.sh", "tools/rig-build.sh", "docker/Dockerfile.runtime",
                    "sources/DEFAULT", "sources/upstream.repos", "sources/fork.repos"):
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, target)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "docker").write_text(DOCKER_STUB)
        (bin_dir / "docker").chmod(0o755)
        self.log = self.root / "docker.jsonl"
        self.env = {"PATH": f"{bin_dir}:{Path(sys.executable).parent}:{os.defpath}",
                    "HOME": str(self.root), "TMPDIR": str(self.root), "STUB_LOG": str(self.log)}

    def build(self, *args, **env):
        self.log.write_text("")
        result = subprocess.run([str(self.repo / "tools" / "build_image.sh"), *args],
                                env={**self.env, **env}, capture_output=True, text=True,
                                cwd=self.repo)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return result, calls

    def export(self, **rows):
        """A $RIG_BUILD_SOURCES tree as rig writes it: <name>/ per source + sources.tsv."""
        sources = self.root / "rig sources"
        sources.mkdir(exist_ok=True)
        lines = []
        for name, (repo, ref) in rows.items():
            (sources / name).mkdir(exist_ok=True)
            (sources / name / "meson.build").write_text("project('mavlink-router')\n")
            lines.append("\t".join((name, repo, ref, ref)))
        (sources / "sources.tsv").write_text("".join(line + "\n" for line in lines))
        return str(sources)

    @staticmethod
    def build_args(call):
        return dict(arg.split("=", 1) for flag, arg in zip(call, call[1:]) if flag == "--build-arg")

    def test_standalone_build_clones_the_default_pin_and_pushes(self):
        result, calls = self.build("reg.example:5000", "v1")
        self.assertEqual(result.returncode, 0, result.stderr)
        build, push = calls
        self.assertEqual(build[:3], ["build", "-t", "reg.example:5000/mavlink_router:v1"])
        url, ref = pin("upstream")
        self.assertEqual(self.build_args(build), {"MAVROUTER_URL": url, "MAVROUTER_REF": ref})
        self.assertEqual(build[-3:], ["-f", str(self.repo / "docker/Dockerfile.runtime"),
                                      str(self.repo)])
        self.assertNotIn("--build-context", build)
        self.assertNotIn("--no-cache", build)
        self.assertEqual(push, ["push", "reg.example:5000/mavlink_router:v1"])

    def test_source_toggle_tag_default_and_local_only(self):
        result, calls = self.build("reg", MAVROUTER_SOURCE="fork", PUSH="0",
                                   MAVROUTER_BUILD_PLATFORM="linux/arm64")
        self.assertEqual(result.returncode, 0, result.stderr)
        (build,) = calls  # PUSH=0: no push
        self.assertEqual(build[:3], ["build", "-t", "reg/mavlink_router:latest"])
        url, ref = pin("fork")
        self.assertEqual(self.build_args(build), {"MAVROUTER_URL": url, "MAVROUTER_REF": ref})
        self.assertEqual(build[build.index("--platform") + 1], "linux/arm64")

    def test_rig_portable_build_publishes_and_reports_the_digest(self):
        report = self.root / "result.jsonl"
        result, calls = self.build("reg", "t", RIG_BUILD_PLATFORMS="linux/arm64,linux/amd64",
                                   RIG_BUILD_RESULT=str(report), RIG_BUILD_NO_CACHE="1",
                                   MAVROUTER_BUILD_PLATFORM="linux/riscv64")  # rig's matrix wins
        self.assertEqual(result.returncode, 0, result.stderr)
        (build,) = calls
        self.assertEqual(build[:2], ["buildx", "build"])
        self.assertEqual([arg for flag, arg in zip(build, build[1:]) if flag == "--platform"],
                         ["linux/arm64,linux/amd64"])
        for flag in ("--push", "--no-cache", "--pull"):
            self.assertIn(flag, build)
        self.assertNotIn("--load", build)
        self.assertEqual([json.loads(line) for line in report.read_text().splitlines()],
                         [{"ref": "reg/mavlink_router:t", "digest": DIGEST}])

    def test_offline_build_keeps_pull_off(self):
        sources = self.export(upstream=pin("upstream"), fork=pin("fork"))
        result, calls = self.build("reg", "t", RIG_BUILD_SOURCES=sources, RIG_OFFLINE="1",
                                   RIG_BUILD_PULL="0", RIG_BUILD_NO_CACHE="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--no-cache", calls[0])
        self.assertNotIn("--pull", calls[0])

    def test_rig_exported_source_replaces_the_clone(self):
        sources = self.export(upstream=pin("upstream"), fork=pin("fork"))
        for name in ("upstream", "fork"):
            result, calls = self.build("reg", "t", RIG_BUILD_SOURCES=sources, MAVROUTER_SOURCE=name)
            self.assertEqual(result.returncode, 0, result.stderr)
            build = calls[0]
            self.assertEqual(build[build.index("--build-context") + 1],
                             f"mavrouter-src={sources}/{name}")
            url, ref = pin(name)
            self.assertEqual(self.build_args(build), {"MAVROUTER_URL": url, "MAVROUTER_REF": ref})

    def test_drifted_rigging_pin_is_refused_before_any_build(self):
        url, _ = pin("upstream")
        sources = self.export(upstream=(url, "0" * 40))
        result, calls = self.build("reg", "t", RIG_BUILD_SOURCES=sources)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("disagrees with", result.stderr)
        self.assertEqual(calls, [])

    def test_offline_never_falls_back_to_cloning(self):
        for env in ({}, {"RIG_BUILD_SOURCES": self.export(fork=pin("fork"))}):
            result, calls = self.build("reg", "t", RIG_OFFLINE="1", **env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("offline build", result.stderr)
            self.assertEqual(calls, [])

    def test_online_build_without_an_export_for_this_source_still_clones(self):
        result, calls = self.build("reg", "t", RIG_BUILD_SOURCES=self.export(fork=pin("fork")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--build-context", calls[0])

    def test_bad_source_names_and_pins_are_refused(self):
        for source in ("nope", "../sources/upstream", "UP stream", ""):
            env = {"MAVROUTER_SOURCE": source} if source else {}
            if not source:
                (self.repo / "sources" / "DEFAULT").write_text("\n")
            result, calls = self.build("reg", "t", **env)
            self.assertNotEqual(result.returncode, 0, source)
            self.assertEqual(calls, [], source)
        shutil.copy2(ROOT / "sources" / "DEFAULT", self.repo / "sources" / "DEFAULT")
        pin_file = self.repo / "sources" / "upstream.repos"
        original = pin_file.read_text()
        for version in ("--upload-pack=x", "v4;reboot", ""):
            pin_file.write_text(re.sub(r"version: \S*", f"version: {version}", original))
            result, calls = self.build("reg", "t")
            self.assertNotEqual(result.returncode, 0, version)
            self.assertEqual(calls, [], version)

    def test_registry_argument_is_required(self):
        result, calls = self.build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage", result.stderr)
        self.assertEqual(calls, [])


HAVE_COMPOSE = bool(shutil.which("docker")) and subprocess.run(
    ["docker", "compose", "version"], capture_output=True).returncode == 0
NEEDS_COMPOSE = "needs the docker compose plugin (client-side render only; no daemon)"


class SurfaceCase(unittest.TestCase):
    """The launcher run from a scratch copy of exactly the declared launch surface — what a
    vendored deployment has — with a clean environment."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        for rel in RIGGING["launch_surface"]:
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, target)
        self.config = self.root / "fleet.yaml"
        keep = ("PATH", "HOME", "TMPDIR", "DOCKER_CONFIG", "DOCKER_HOST", "DOCKER_CONTEXT")
        self.env = {k: v for k, v in os.environ.items() if k in keep}
        self.env["MAVROUTER_PYTHON"] = sys.executable

    def launch(self, *verb, **env):
        return subprocess.run([str(self.repo / "mavlink-router-up"), str(self.config), *verb],
                              env={**self.env, **env}, capture_output=True, text=True)


@unittest.skipUnless(HAVE_COMPOSE, NEEDS_COMPOSE)
class ImagePlanTests(SurfaceCase):
    """`images` / `runtime-images`: the shapes rig parses, with nothing rendered or started."""

    def setUp(self):
        super().setUp()
        # per-vehicle identity still unresolved, as in rig's templated-artifact image plan
        self.config.write_text("service: mavlink-router\nname: fcu\n"
                               "udp_endpoints:\n  - {name: gcs, mode: normal,"
                               " address: '10.160.{{vehicle_id}}.80', port: 14550}\n")

    def query(self, verb, **env):
        result = self.launch(verb, **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.repo / "var").exists(), "an image query must not stage anything")
        return result.stdout

    def test_images_is_a_yaml_list_of_resolved_refs(self):
        self.assertEqual(yaml.safe_load(self.query("images")), ["mavlink_router:latest"])
        fleet = {"RIG_IMAGE_REGISTRY": "reg.example:5000", "RIG_IMAGE_TAG": "v9"}
        self.assertEqual(yaml.safe_load(self.query("images", **fleet)),
                         ["reg.example:5000/mavlink_router:v9"])

    def test_runtime_images_is_the_version_1_inventory(self):
        fleet = {"RIG_IMAGE_REGISTRY": "reg.example:5000", "RIG_IMAGE_TAG": "v9",
                 "COMPOSE_PROJECT_NAME": "fcu-vehicle-7"}
        self.assertEqual(json.loads(self.query("runtime-images", **fleet)),
                         {"version": 1, "images": ["reg.example:5000/mavlink_router:v9"]})

    def test_override_env_carries_a_digest_pin_into_the_plan(self):
        pinned = f"reg.example:5000/mavlink_router@{DIGEST}"
        self.assertEqual(json.loads(self.query("runtime-images", MAVROUTER_IMAGE=pinned)),
                         {"version": 1, "images": [pinned]})

    def test_pinned_up_refuses_an_image_set_it_was_not_given(self):
        """RIG_RUNTIME_IMAGE_PINS present = a rig session: `up` must go through the pin check
        (and fail closed) instead of a plain `docker compose up` that could pull anything."""
        self.config.write_text("name: fcu\nudp_endpoints:\n"
                               "  - {name: gcs, mode: normal, address: 127.0.0.1, port: 14550}\n")
        pins = json.dumps({"version": 1, "images": {"some/other:image": DIGEST}})
        result = self.launch("up", "-d", RIG_RUNTIME_IMAGE_PINS=pins)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("runtime image selection changed", result.stderr)


@unittest.skipUnless(HAVE_COMPOSE, NEEDS_COMPOSE)
class ComposeModelTests(SurfaceCase):
    """What `up` would hand to Docker for a given config: the merged compose model, rendered
    client-side by `mavlink-router-up <config> config`."""

    def model(self, text, **env):
        self.config.write_text(text)
        result = self.launch("config", "--format", "json", **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        model = json.loads(result.stdout)
        self.assertEqual(list(model["services"]), ["router"])
        return model, model["services"]["router"]

    NET = ("name: bench\ngeneral: {tcp_server_port: 5790}\n"
           "udp_endpoints: [{name: gcs, mode: server, address: 0.0.0.0, port: 14550}]\n")

    def test_network_only_instance(self):
        model, router = self.model(self.NET)
        self.assertEqual(model["name"], "mavlink-router_bench")
        self.assertEqual(router["image"], "mavlink_router:latest")
        self.assertEqual(router["network_mode"], "host")
        self.assertEqual(router["restart"], "unless-stopped")
        self.assertEqual(router["environment"], {"MAVROUTER_HEALTH_PORT": "5790"})
        self.assertNotIn("devices", router)
        self.assertNotIn("group_add", router)
        self.assertNotIn("container_name", router)
        staged = self.repo / "var" / "run" / "bench.conf"
        (mount,) = router["volumes"]
        self.assertEqual((mount["type"], mount["source"], mount["target"], mount.get("read_only")),
                         ("bind", str(staged), "/etc/mavlink-router/main.conf", True))
        self.assertIn("Port = 14550", staged.read_text())
        import hashlib
        self.assertEqual(router["labels"]["mavlink-router-rig.conf-sha256"],
                         hashlib.sha256(staged.read_bytes()).hexdigest())

    def test_project_name_and_image_follow_the_fleet_env(self):
        model, router = self.model(self.NET, COMPOSE_PROJECT_NAME="bench-vehicle-7",
                                   RIG_IMAGE_REGISTRY="reg.example:5000", RIG_IMAGE_TAG="v9")
        self.assertEqual(model["name"], "bench-vehicle-7")
        self.assertEqual(router["image"], "reg.example:5000/mavlink_router:v9")
        _, router = self.model(self.NET, MAVROUTER_IMAGE="local/dev:1",
                               RIG_IMAGE_REGISTRY="reg.example:5000", RIG_IMAGE_TAG="v9")
        self.assertEqual(router["image"], "local/dev:1")

    def test_disabled_tcp_server_switches_the_health_probe(self):
        _, router = self.model(self.NET.replace("5790", "0"))
        self.assertEqual(router["environment"]["MAVROUTER_HEALTH_PORT"], "0")

    def test_example_config_passes_its_serial_device_through(self):
        device = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0"
        _, router = self.model((ROOT / RIGGING["examples"][0]).read_text())
        self.assertEqual([(d["source"], d["target"]) for d in router["devices"]],
                         [(device, device)])
        self.assertEqual(router["group_add"], ["dialout"])
        self.assertEqual(len(router["volumes"]), 1, "no data bind unless logging is enabled")

    def test_every_uart_is_passed_through_in_order(self):
        devices = ["/dev/ttyTHS1", "/dev/serial/by-id/usb-radio-if00", "/dev/ttyUSB7",
                   "/dev/ttyACM0"]
        rows = "".join(f"  - {{name: u{n}, device: {path}}}\n" for n, path in enumerate(devices))
        for count in (2, 3, 4):
            _, router = self.model("name: multi\nuart_endpoints:\n"
                                   + "".join(rows.splitlines(keepends=True)[:count]))
            self.assertEqual([(d["source"], d["target"]) for d in router["devices"]],
                             [(path, path) for path in devices[:count]])
            self.assertEqual(router["group_add"], ["dialout"])
            conf = (self.repo / "var" / "run" / "multi.conf").read_text()
            self.assertEqual(conf.count("[UartEndpoint "), count)

    def test_logging_binds_the_data_dir_and_names_the_instance(self):
        text = self.NET + "logging: {enabled: true, telemetry: true}\n"
        data = self.root / "data"
        _, router = self.model(text, RIG_DATA_DIR=str(data))
        self.assertEqual(router["environment"], {"MAVROUTER_HEALTH_PORT": "5790",
                                                 "MAVROUTER_LOG_INSTANCE": "bench"})
        binds = {mount["target"]: mount["source"] for mount in router["volumes"]}
        self.assertEqual(binds["/data"], str(data))
        conf = (self.repo / "var" / "run" / "bench.conf").read_text()
        self.assertIn("Log = /data/mavlink-router/bench\n", conf)
        self.assertNotIn(str(data), json.dumps(router["labels"]))

    def test_logging_without_a_data_dir_is_refused_by_name(self):
        self.config.write_text(self.NET + "logging: {enabled: true}\n")
        result = self.launch("config")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RIG_DATA_DIR", result.stderr)

    def test_a_rejected_config_stops_before_compose(self):
        self.config.write_text("name: bench\ngeneral: {tcp_server_prot: 1}\n")
        result = self.launch("up", "-d")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip().splitlines()[-1].split(":")[0], "render_conf")
        self.assertFalse((self.repo / "var").exists())

    def test_status_is_compose_ps(self):
        self.config.write_text(self.NET)
        result = self.launch("status", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker compose ps", result.stdout)


if __name__ == "__main__":
    unittest.main()
