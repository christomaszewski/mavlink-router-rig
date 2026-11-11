# Copyright 2026 Chris Tomaszewski
# SPDX-License-Identifier: Apache-2.0
"""`uart_endpoints:` against the real router binary, with a pseudo-terminal standing in for the
serial device.

These tests run router and peers in ONE container (harness.embedded): a pty lives in the devpts
of the container that opened it, so it cannot be handed to another container through compose's
`devices:`. What they cover is everything from the rig config to bytes on the wire — the
rendered [UartEndpoint] section, the image's entrypoint, the router opening and configuring the
device. The compose side of a serial launch (the device mapping itself) is checked on the
rendered model in tests/unit/test_contract.py, and by `up` refusing a missing device in
test_lifecycle.py.
"""
import unittest

import mav
from harness import REPO, RouterCase, ports


def udp(name, port, mode="server", **extra):
    return {"name": name, "mode": mode, "address": "127.0.0.1", "port": port, **extra}


class SerialTests(RouterCase):
    def test_serial_link_routes_both_ways(self):
        p = ports("server gcs")
        self.embedded(serial_link_routes_both_ways, {
            "general": {"tcp_server_port": p.server},
            "uart_endpoints": [{"name": "fcu", "device": "/dev/ttyFCU", "baud": 921600}],
            "udp_endpoints": [udp("gcs", p.gcs)],
        }, p)

    def test_shipped_example_config_routes_a_vehicle(self):
        """config/mavlink-router.example.yaml exactly as shipped — only its serial device swapped
        for a pseudo-terminal — connects the autopilot, a ground station and an onboard stack on
        the ports it documents. (One container, own network namespace: the real 14550/14540/5760
        are free here whatever runs on the host.)"""
        import yaml
        config = yaml.safe_load((REPO / "config" / "mavlink-router.example.yaml").read_text())
        self.assertEqual((config.pop("service"), config.pop("name")), ("mavlink-router", "fcu"))
        (uart,) = config["uart_endpoints"]
        uart["device"] = "/dev/ttyFCU"
        self.assertEqual([(e["name"], e["mode"], e["port"]) for e in config["udp_endpoints"]],
                         [("gcs", "server", 14550), ("autonomy", "normal", 14540)])
        self.assertEqual(config["general"]["tcp_server_port"], 5760)
        self.embedded(shipped_example_config_routes_a_vehicle, config)

    def test_two_serial_links_and_their_options(self):
        p = ports("server gcs")
        self.embedded(two_serial_links_and_their_options, {
            "general": {"tcp_server_port": p.server},
            "uart_endpoints": [
                {"name": "fcu", "device": "/dev/ttyFCU", "baud": 921600},
                {"name": "radio", "device": "/dev/ttyRADIO", "baud": 57600, "flow_control": True,
                 "filters": {"BlockMsgIdOut": [mav.ATTITUDE]}}],
            "udp_endpoints": [udp("gcs", p.gcs)],
        }, p)

    def test_baud_list_is_cycled_until_mavlink_arrives(self):
        p = ports("server gcs")
        self.embedded(baud_list_is_cycled_until_mavlink_arrives, {
            "general": {"tcp_server_port": p.server},
            "uart_endpoints": [{"name": "fcu", "device": "/dev/ttyFCU",
                                "baud": [57600, 921600]}],
            "udp_endpoints": [udp("gcs", p.gcs)],
        }, p)

    def test_router_refuses_to_start_without_its_device(self):
        """What a restart looks like after the adapter disappeared: a clear log line and a
        non-zero exit (which Docker's restart policy then retries)."""
        p = ports("server")
        result = self.embedded(router_refuses_to_start_without_its_device, {
            "general": {"tcp_server_port": p.server},
            "uart_endpoints": [{"name": "fcu", "device": "/dev/ttyGONE"}],
        }, p)
        self.assertNotEqual(result["exit"], 0)
        self.assertIn("Could not open /dev/ttyGONE", result["log"])


# ---- peer halves ------------------------------------------------------------------------------------

def serial_link_routes_both_ways(p):
    master, _slave = mav.open_pty("/dev/ttyFCU")
    with mav.LocalRouter(wait_port=p.server) as router:
        fcu = mav.SerialPeer(1, 1, "autopilot on the serial port", master)
        gcs = mav.UdpPeer(255, 190, "gcs", router=p.gcs)
        mav.introduce(fcu, gcs)
        assert "Opened UART" in router.output() and "speed = 921600" in router.output()

        gcs.expect(fcu.send(mav.attitude(1)))
        fcu.expect(gcs.send(mav.command_long(1, 1, 400)))
        # The line is raw: every byte value crosses it untouched (no CR/LF translation, no
        # XON/XOFF, no 8th-bit stripping), in both directions.
        every_byte = (mav.UNKNOWN_MSG, bytes(range(255)))
        gcs.expect(fcu.send(every_byte))
        fcu.expect(gcs.send(every_byte))
        # a burst arrives complete and in order
        burst = [gcs.send(mav.system_time(n)) for n in range(50)]
        assert mav.raws(fcu.until(burst[-1])) == burst
        burst = [fcu.send(mav.system_time(n)) for n in range(50)]
        assert mav.raws(gcs.until(burst[-1])) == burst


def shipped_example_config_routes_a_vehicle(p):
    master, _slave = mav.open_pty("/dev/ttyFCU")
    with mav.LocalRouter(wait_port=5760) as router:
        fcu = mav.SerialPeer(1, mav.COMP_AUTOPILOT, "autopilot on the serial port", master)
        gcs = mav.UdpPeer(255, 190, "ground station calling in on 14550", router=14550)
        autonomy = mav.UdpPeer(1, 195, "onboard stack listening on 14540", bind=14540)
        tablet = mav.connect(5760, 254, 190, "second ground station over tcp 5760")
        everyone = [fcu, gcs, autonomy, tablet]
        mav.introduce(*everyone)
        assert "speed = 500000" in router.output(), router.output()

        telemetry = fcu.send(mav.attitude(1))               # the autopilot's stream: to everyone
        for consumer in (gcs, autonomy, tablet):
            consumer.expect(telemetry)
        fcu.expect(gcs.send(mav.command_long(1, 1, 400)))       # ground station -> autopilot
        fcu.expect(autonomy.send(mav.command_long(1, 1, 176)))  # onboard stack -> autopilot
        fcu.expect(tablet.send(mav.command_long(1, 1, 511)))    # tcp client -> autopilot
        gcs.expect(autonomy.send(mav.statustext("from the onboard stack")))


def two_serial_links_and_their_options(p):
    fcu_master, _fcu_slave = mav.open_pty("/dev/ttyFCU")
    radio_master, _radio_slave = mav.open_pty("/dev/ttyRADIO")
    with mav.LocalRouter(wait_port=p.server) as router:
        fcu = mav.SerialPeer(1, 1, "autopilot", fcu_master)
        radio = mav.SerialPeer(255, 190, "gcs behind the serial radio", radio_master)
        gcs = mav.UdpPeer(254, 190, "gcs on udp", router=p.gcs)
        mav.introduce(fcu, radio, gcs)
        log = router.output()
        assert log.count("Opened UART") == 2, log
        assert "speed = 921600" in log and "speed = 57600" in log, log

        # serial to serial, both ways
        fcu.expect(radio.send(mav.command_long(1, 1, 400)))
        radio.expect(fcu.send(mav.system_time(1)))
        # the radio endpoint's filter: ATTITUDE stays off the slow link, the UDP link gets it
        blocked = fcu.send(mav.attitude(1))
        passed = fcu.send(mav.system_time(2))
        assert mav.raws(radio.until(passed)) == [passed]
        assert mav.raws(gcs.until(passed))[-2:] == [blocked, passed]


def baud_list_is_cycled_until_mavlink_arrives(p):
    import time
    master, _slave = mav.open_pty("/dev/ttyFCU")
    with mav.LocalRouter(wait_port=p.server) as router:
        router.wait_for("speed = 57600")           # the first rate in the list
        router.wait_for("speed = 921600")          # nothing valid arrived for 5 s: the next one
        fcu = mav.SerialPeer(1, 1, "autopilot", master)
        gcs = mav.UdpPeer(255, 190, "gcs", router=p.gcs)
        mav.introduce(fcu, gcs)                    # valid MAVLink: the search stops here
        settled = router.output().count("speed = ")
        time.sleep(6)
        gcs.expect(fcu.send(mav.system_time(1)))
        assert router.output().count("speed = ") == settled, "the rate kept changing on a live link"


def router_refuses_to_start_without_its_device(p):
    router = mav.LocalRouter()
    try:
        code = router.process.wait(timeout=10)
    finally:
        log = router.stop()
    return {"exit": code, "log": log}


if __name__ == "__main__":
    unittest.main()
