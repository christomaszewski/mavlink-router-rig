#!/usr/bin/env python3
# Copyright 2026 Chris Tomaszewski
# SPDX-License-Identifier: Apache-2.0
"""render_conf.py: what it renders, what it refuses, and how it stages the result.

    python3 -m unittest discover -s tests/unit -v
"""
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]
RENDER = ROOT / "tools" / "render_conf.py"
EXAMPLE = ROOT / "config" / "mavlink-router.example.yaml"


def load_renderer():
    spec = importlib.util.spec_from_file_location("render_conf", RENDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RenderCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def config(self, text):
        path = self.tmp / "config.yaml"
        path.write_text(textwrap.dedent(text))
        return path

    def run_render(self, *args):
        return subprocess.run([sys.executable, str(RENDER), *map(str, args)],
                              capture_output=True, text=True)

    def render(self, text, *flags):
        result = self.run_render(*flags, self.config(text))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def assert_rejected(self, text, expect):
        """Every mode must refuse the config, with a one-line message and no traceback."""
        config = self.config(text)
        for flags in ((), ("--env",), ("--stage", self.tmp / "stage")):
            result = self.run_render(*flags, config)
            self.assertEqual(result.returncode, 2, f"{flags}: {result.stdout}{result.stderr}")
            self.assertEqual(result.stdout, "", flags)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
            self.assertIn(expect, result.stderr)
        self.assertFalse((self.tmp / "stage").exists(), "a refused config must stage nothing")


class RenderTests(RenderCase):
    def test_example_config_renders_upstream_keys(self):
        result = self.run_render(EXAMPLE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, textwrap.dedent("""\
            # rendered by render_conf.py for instance 'fcu' — edit the rig config, not this file

            [General]
            TcpServerPort = 5760
            DebugLogLevel = info

            [UartEndpoint fcu]
            Device = /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0
            Baud = 500000

            [UdpEndpoint gcs]
            Mode = Server
            Address = 0.0.0.0
            Port = 14550

            [UdpEndpoint autonomy]
            Mode = Normal
            Address = 127.0.0.1
            Port = 14540
            """))

    def test_every_supported_key(self):
        out = self.render("""
            name: full
            general:
              tcp_server_port: 0
              report_stats: true
              debug_log_level: trace
              dedup_period_ms: 100
              mavlink_dialect: ardupilotmega
              sniffer_sysid: 254
            logging:
              enabled: true
              mode: while-armed
              telemetry: true
              log_system_id: 1
              max_log_files: 50
              min_free_space_mb: 512
            uart_endpoints:
              - {name: fcu, device: /dev/ttyTHS1, baud: [57600, 921600], flow_control: true,
                 tx_queue_size: 280}
              - {name: sik, device: /dev/ttyUSB0, baud: 57600}
            udp_endpoints:
              - {name: auto, mode: normal, address: "[::1]"}
              - name: radio
                mode: server
                address: 0.0.0.0
                port: 14550
                tx_queue_size: 65536
                filters: {allowmsgidout: [0, 1], BlockSrcSysIn: 255, Group: radios}
            tcp_endpoints:
              - {name: cloud, address: 192.168.1.20, port: 5761, retry_timeout: 0,
                 tx_queue_size: 1048576, filters: {AllowSrcCompOut: "1, 190", Group: 2}}
            """)
        self.assertEqual(out, textwrap.dedent("""\
            # rendered by render_conf.py for instance 'full' — edit the rig config, not this file

            [General]
            TcpServerPort = 0
            ReportStats = true
            DebugLogLevel = trace
            DeduplicationPeriod = 100
            MavlinkDialect = ardupilotmega
            SnifferSysid = 254
            Log = /data/mavlink-router/full
            LogMode = while-armed
            LogTelemetry = true
            LogSystemId = 1
            MaxLogFiles = 50
            MinFreeSpace = 536870912

            [UartEndpoint fcu]
            Device = /dev/ttyTHS1
            Baud = 57600,921600
            FlowControl = true
            TxQueueSize = 280

            [UartEndpoint sik]
            Device = /dev/ttyUSB0
            Baud = 57600

            [UdpEndpoint auto]
            Mode = Normal
            Address = [::1]

            [UdpEndpoint radio]
            Mode = Server
            Address = 0.0.0.0
            Port = 14550
            TxQueueSize = 65536
            AllowMsgIdOut = 0,1
            BlockSrcSysIn = 255
            Group = radios

            [TcpEndpoint cloud]
            Address = 192.168.1.20
            Port = 5761
            RetryTimeout = 0
            TxQueueSize = 1048576
            AllowSrcCompOut = 1,190
            Group = 2
            """))

    def test_every_filter_key(self):
        """All twelve id filters and Group, on each endpoint kind, in upstream's own casing."""
        filters = {key: [1, 2] for key in load_renderer()._FILTER_KEYS if key != "Group"}
        filters["Group"] = "g"
        spelled = ", ".join(f"{key}: {value}" for key, value in filters.items())
        out = self.render(f"""
            name: f
            uart_endpoints: [{{name: u, device: /dev/ttyS0, filters: {{{spelled}}}}}]
            udp_endpoints: [{{name: d, mode: normal, address: 127.0.0.1, filters: {{{spelled}}}}}]
            tcp_endpoints: [{{name: t, address: 127.0.0.1, port: 1, filters: {{{spelled}}}}}]
            """)
        for key in filters:
            expected = f"{key} = g" if key == "Group" else f"{key} = 1,2"
            self.assertEqual(out.count(expected + "\n"), 3, key)

    def test_example_documents_every_supported_key(self):
        """config/mavlink-router.example.yaml is the reference for operators: each key the
        renderer accepts must appear in it, active or commented out."""
        renderer = load_renderer()
        text = EXAMPLE.read_text()
        keys = (renderer._TOP_KEYS | renderer._GENERAL_KEYS | renderer._LOGGING_KEYS
                | renderer._UART_KEYS | renderer._UDP_KEYS | renderer._TCP_KEYS)
        missing = sorted(key for key in keys if not re.search(rf"(?m)^[#\s-]*{key}:", text))
        self.assertEqual(missing, [], "keys the example config does not show")
        missing = sorted(key for key in renderer._FILTER_KEYS if key not in text)
        self.assertEqual(missing, [], "filter keys the example config does not show")
        for choices in (renderer._LOG_LEVELS, renderer._DIALECTS, renderer._LOG_MODES,
                        renderer._UDP_MODES):
            self.assertEqual([choice for choice in choices if choice not in text], [])

    def test_omitted_keys_keep_upstream_defaults(self):
        self.assertEqual(self.render("name: bare\n"), textwrap.dedent("""\
            # rendered by render_conf.py for instance 'bare' — edit the rig config, not this file

            [General]
            """))

    def test_disabled_logging_renders_nothing_but_is_still_validated(self):
        self.assertNotIn("Log", self.render("name: n\nlogging: {enabled: false, mode: always}\n"))
        self.assert_rejected("name: n\nlogging: {enabled: false, mode: sometimes}\n",
                             "logging.mode")

    def test_env_tokens(self):
        env = self.render("""
            name: net
            general: {tcp_server_port: 5790}
            udp_endpoints: [{name: gcs, mode: server, address: 0.0.0.0, port: 14550}]
            """, "--env")
        self.assertEqual(dict(line.split("=", 1) for line in env.splitlines()), {
            "SERVICE": "mavlink-router", "NAME": "net", "TYPE": "net", "DEVICE": "''",
            "DEVICE_2": "''", "DEVICE_3": "''", "DEVICE_4": "''",
            "HEALTH_PORT": "5790", "LOGGING": "0"})
        result = self.run_render("--env", EXAMPLE)
        tokens = dict(line.split("=", 1) for line in result.stdout.splitlines())
        self.assertEqual(tokens["TYPE"], "serial")
        self.assertEqual(shlex.split(tokens["DEVICE"]),
                         ["/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0"])
        self.assertEqual(tokens["HEALTH_PORT"], "5760")
        self.assertEqual(tokens["DEVICE_2"], "''")

    def test_each_uart_gets_its_own_device_token(self):
        """One serial compose overlay per device: DEVICE, DEVICE_2, ... in endpoint order."""
        env = self.render("""
            name: multi
            uart_endpoints:
              - {name: fcu, device: /dev/ttyTHS1}
              - {name: radio, device: "/dev/serial/by-id/usb-Silicon Labs-if00"}
              - {name: gimbal, device: /dev/ttyUSB7}
            """.replace("Silicon Labs", "Silicon_Labs"), "--env")
        tokens = dict(line.split("=", 1) for line in env.splitlines())
        self.assertEqual([tokens[key] for key in ("TYPE", "DEVICE", "DEVICE_2", "DEVICE_3",
                                                  "DEVICE_4")],
                         ["serial", "/dev/ttyTHS1", "/dev/serial/by-id/usb-Silicon_Labs-if00",
                          "/dev/ttyUSB7", "''"])

    def test_unknown_keys_are_refused(self):
        self.assert_rejected("name: n\ngeneral: {tcp_server_prot: 5760}\n", "tcp_server_prot")
        self.assert_rejected("name: n\nloging: {enabled: true}\n", "loging")
        self.assert_rejected(
            "name: n\nudp_endpoints: [{name: a, mode: normal, address: 127.0.0.1, prot: 1}]\n",
            "prot")
        self.assert_rejected(
            "name: n\nudp_endpoints: [{name: a, mode: normal, address: 127.0.0.1,"
            " filters: {AllowMsgOut: 1}}]\n", "AllowMsgOut")

    def test_values_upstream_would_refuse_at_startup(self):
        udp = "name: n\nudp_endpoints: [{name: a, mode: %s, address: %s, port: %s}]\n"
        self.assert_rejected(udp % ("server", "0.0.0.0", 0), "port")
        self.assert_rejected(udp % ("normal", "127.0.0.1", 65536), "port")
        self.assert_rejected(udp % ("server", "0.0.0.0", '"14550"'), "port")
        self.assert_rejected(udp % ("normal", "gcs.local", 14550), "IP literal")
        self.assert_rejected(udp % ("normal", '"::1"', 14550), "IP literal")  # needs brackets
        self.assert_rejected(udp % ("normal", '"[FE80::1]"', 14550), "IP literal")  # lowercase
        self.assert_rejected(udp % ("eavesdropping", "0.0.0.0", 14550), "mode")
        self.assert_rejected("name: n\nudp_endpoints: [{name: a, mode: server, address: 0.0.0.0}]\n",
                             "server mode requires")
        self.assert_rejected("name: n\ntcp_endpoints: [{name: a, address: 10.0.0.1}]\n", "port")
        self.assert_rejected("name: n\ngeneral: {tcp_server_port: 70000}\n", "tcp_server_port")
        self.assert_rejected("name: n\ngeneral: {tcp_server_port: true}\n", "tcp_server_port")
        self.assert_rejected("name: n\ngeneral: {debug_log_level: verbose}\n", "debug_log_level")
        self.assert_rejected("name: n\ngeneral: {mavlink_dialect: px4}\n", "mavlink_dialect")
        self.assert_rejected("name: n\nlogging: {enabled: true, log_system_id: 0}\n",
                             "log_system_id")
        self.assert_rejected("name: n\nlogging: {enabled: true, max_log_files: -3}\n",
                             "max_log_files")
        self.assert_rejected("name: n\nlogging: {enabled: 1}\n", "logging.enabled")
        self.assert_rejected(
            "name: n\nuart_endpoints: [{name: a, device: /dev/ttyUSB0, baud: fast}]\n", "baud")
        self.assert_rejected(
            "name: n\nudp_endpoints: [{name: a, mode: normal, address: 127.0.0.1,"
            " filters: {BlockSrcSysIn: 256}}]\n", "BlockSrcSysIn")
        self.assert_rejected("name: n\ngeneral: {sniffer_sysid: 256}\n", "sniffer_sysid")
        self.assert_rejected("name: n\ngeneral: {dedup_period_ms: -1}\n", "dedup_period_ms")
        self.assert_rejected("name: n\ngeneral: {report_stats: 1}\n", "report_stats")
        # the fork's own accepted range for TxQueueSize: one maximum-size frame .. 1 MiB
        queue = "name: n\nudp_endpoints: [{name: a, mode: normal, address: 127.0.0.1," \
                " tx_queue_size: %s}]\n"
        for bad in (279, 1048577, 0, "big", "8192.5"):
            self.assert_rejected(queue % bad, "tx_queue_size")
        self.assert_rejected(
            "name: n\ntcp_endpoints: [{name: a, address: 10.0.0.1, port: 1, retry_timeout: -1}]\n",
            "retry_timeout")

    def test_a_value_cannot_open_another_ini_line(self):
        """`address: "127.0.0.1\\nPort = 1\\n[UartEndpoint x]\\nDevice = /dev/sda"` used to render
        a whole extra endpoint section past the one-UART and device-path checks."""
        payload = r'"127.0.0.1\nPort = 1\n[UartEndpoint x]\nDevice = /dev/sda"'
        self.assert_rejected(
            f"name: n\nudp_endpoints: [{{name: a, mode: normal, address: {payload}}}]\n",
            "IP literal")
        self.assert_rejected(
            'name: n\nudp_endpoints: [{name: a, mode: normal, address: 127.0.0.1,'
            ' filters: {Group: "g\\nDevice = /dev/sda"}}]\n', "Group")
        self.assert_rejected(
            'name: n\nuart_endpoints: [{name: a, device: "/dev/ttyS0\\nBaud = 1"}]\n', "device")
        self.assert_rejected(
            'name: n\ngeneral: {debug_log_level: "info\\nLog = /"}\n', "debug_log_level")

    def test_structure_errors_are_messages_not_tracebacks(self):
        self.assert_rejected("name: [n\n", "not valid YAML")
        self.assert_rejected("- just\n- a list\n", "must be a YAML mapping")
        self.assert_rejected("name: n\ngeneral: [1, 2]\n", "`general` must be a mapping")
        self.assert_rejected("name: n\nlogging: yes please\n", "`logging` must be a mapping")
        self.assert_rejected("name: n\nudp_endpoints: {name: a}\n", "must be a list")
        self.assert_rejected("name: n\nudp_endpoints: [just-a-string]\n", "must be a mapping")
        self.assert_rejected("name: n\nudp_endpoints: [{name: a, mode: normal,"
                             " address: 127.0.0.1, filters: [1]}]\n", "filters")
        result = self.run_render(self.tmp / "missing.yaml")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)

    def test_identity_and_layout_rules(self):
        self.assert_rejected("name: Fcu-1\n", "name")
        self.assert_rejected("name: 7\n", "name")
        self.assert_rejected("service: 'mavlink router'\n", "service")
        self.assert_rejected(
            "name: n\nudp_endpoints:\n"
            "  - {name: a, mode: normal, address: 127.0.0.1}\n"
            "  - {name: a, mode: normal, address: 127.0.0.2}\n", "duplicate endpoint name")
        self.assert_rejected(
            "name: n\nuart_endpoints:\n" + "".join(
                f"  - {{name: u{n}, device: /dev/ttyUSB{n}}}\n" for n in range(5)),
            "at most 4 UART")
        self.assert_rejected(
            "name: n\nuart_endpoints:\n"
            "  - {name: a, device: /dev/ttyUSB0}\n  - {name: b, device: /dev/ttyUSB0}\n",
            "already used")
        self.assert_rejected(
            "name: n\nuart_endpoints: [{name: a, device: /dev/ttyUSB0}]\n"
            "udp_endpoints: [{name: a, mode: normal, address: 127.0.0.1}]\n",
            "duplicate endpoint name")
        self.assert_rejected(
            "name: n\nuart_endpoints:\n"
            "  - {name: a, device: '/dev/serial/by-path/pci-0000:00:14.0-usb-0:2:1.0'}\n",
            "by-id")
        self.assert_rejected("name: n\nuart_endpoints: [{name: a, device: ttyUSB0}]\n",
                             "absolute")
        self.assert_rejected("name: n\ngeneral: {tcp_server_port: 0}\n", "nothing to route")


class StageTests(RenderCase):
    def stage(self, text, directory=None):
        directory = directory or self.tmp / "var" / "run"
        result = self.run_render("--stage", directory, self.config(text))
        self.assertEqual(result.returncode, 0, result.stderr)
        tokens = {}
        for line in result.stdout.splitlines():
            key, value = line.split("=", 1)
            tokens[key] = shlex.split(value)[0] if value != "''" else ""
        return tokens

    def test_stage_writes_the_render_and_reports_it(self):
        text = "name: st\nudp_endpoints: [{name: a, mode: normal, address: 127.0.0.1}]\n"
        tokens = self.stage(text)
        staged = Path(tokens["CONF_HOST"])
        self.assertEqual(staged, self.tmp / "var" / "run" / "st.conf")
        self.assertEqual(staged.read_text(), self.render(text))
        self.assertEqual(tokens["CONF_SHA256"], hashlib.sha256(staged.read_bytes()).hexdigest())
        self.assertEqual(staged.stat().st_mode & 0o777, 0o644)
        self.assertEqual({k: tokens[k] for k in ("SERVICE", "NAME", "TYPE", "LOGGING")},
                         {"SERVICE": "mavlink-router", "NAME": "st", "TYPE": "net", "LOGGING": "0"})
        self.assertEqual(sorted(p.name for p in staged.parent.iterdir()), ["st.conf"])

    def test_unchanged_render_is_not_rewritten_and_a_changed_one_is_swapped(self):
        """The staged file is a running container's bind mount: same content must keep the very
        same file, and new content must arrive whole (rename), never by truncating in place."""
        text = "name: st\nudp_endpoints: [{name: a, mode: normal, address: 127.0.0.1}]\n"
        first = self.stage(text)
        staged = Path(first["CONF_HOST"])
        before = staged.stat()
        again = self.stage(text)
        self.assertEqual(again, first)
        after = staged.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

        held = open(staged)  # what a running container still has open
        self.addCleanup(held.close)
        changed = self.stage(text.replace("127.0.0.1", "127.0.0.2"))
        self.assertNotEqual(changed["CONF_SHA256"], first["CONF_SHA256"])
        self.assertIn("127.0.0.2", staged.read_text())
        self.assertIn("127.0.0.1", held.read(), "the old file must survive intact for its readers")
        self.assertNotEqual(staged.stat().st_ino, before.st_ino)

    def test_a_refused_config_leaves_the_staged_file_alone(self):
        good = "name: st\nudp_endpoints: [{name: a, mode: normal, address: 127.0.0.1}]\n"
        staged = Path(self.stage(good)["CONF_HOST"])
        content = staged.read_text()
        result = self.run_render("--stage", staged.parent,
                                 self.config(good.replace("127.0.0.1", "nowhere")))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(staged.read_text(), content)
        self.assertEqual(sorted(p.name for p in staged.parent.iterdir()), ["st.conf"])

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unwritable_stage_dir_is_a_message(self):
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        self.addCleanup(locked.chmod, 0o755)
        result = self.run_render("--stage", locked, self.config("name: st\n"))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("cannot stage", result.stderr)


if __name__ == "__main__":
    unittest.main()
