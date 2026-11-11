# Copyright 2026 Chris Tomaszewski
# SPDX-License-Identifier: Apache-2.0
"""The `general:` block and the per-endpoint options that are not filters: de-duplication, the
sniffer system id, statistics, log level, a disabled TCP server, and tx_queue_size."""
import time
import unittest

import mav
from harness import RouterCase, image_is_fork, ports


def udp(name, port, mode="server", **extra):
    return {"name": name, "mode": mode, "address": "127.0.0.1", "port": port, **extra}


class GeneralTests(RouterCase):
    def redundant_links(self, **general):
        p = ports("server link_a link_b sink")
        self.router({
            "general": {"tcp_server_port": p.server, **general},
            "udp_endpoints": [udp("link_a", p.link_a), udp("link_b", p.link_b),
                              udp("sink", p.sink, "normal")],
        })
        return p

    def test_dedup_drops_the_copy_from_the_second_link(self):
        p = self.redundant_links(dedup_period_ms=400)
        self.assertEqual(self.peers(duplicate_over_two_links, p, period=0.4), 1)

    def test_without_dedup_both_copies_are_forwarded(self):
        p = self.redundant_links()
        self.assertEqual(self.peers(duplicate_over_two_links, p, period=0), 2)

    def test_sniffer_system_sees_traffic_addressed_to_others(self):
        p = ports("server fcu sniffer bystander")
        self.router({
            "general": {"tcp_server_port": p.server, "sniffer_sysid": 254},
            "udp_endpoints": [udp("fcu", p.fcu), udp("sniffer", p.sniffer),
                              udp("bystander", p.bystander)],
        })
        self.peers(sniffer_system_sees_traffic_addressed_to_others, p)

    def test_report_stats_prints_endpoint_counters(self):
        p = ports("server a b")
        endpoints = [udp("a", p.a), udp("b", p.b)]
        router = self.router({"general": {"tcp_server_port": p.server, "report_stats": True},
                              "udp_endpoints": endpoints})
        self.peers(some_traffic, p)
        # one block per endpoint, once a second, on a block-buffered stdout
        self.assertIn("Received messages", self.wait_for_log(router, "Transmitted messages"))

        quiet = ports("server a b")
        router = self.router({"general": {"tcp_server_port": quiet.server},
                              "udp_endpoints": [udp("a", quiet.a), udp("b", quiet.b)]})
        self.peers(some_traffic, quiet)
        time.sleep(2.5)
        self.assertNotIn("Received messages", router.logs(), "statistics are off by default")

    def test_debug_log_level(self):
        logs = {}
        for level in ("error", "info", "trace"):
            p = ports("server a b")
            router = self.router({
                "general": {"tcp_server_port": p.server, "debug_log_level": level},
                "udp_endpoints": [udp("a", p.a), udp("b", p.b)]})
            self.peers(some_traffic, p)
            logs[level] = router.logs()
        self.assertNotIn("Opened UDP Server", logs["error"])
        self.assertIn("Opened UDP Server", logs["info"])
        self.assertNotIn("accepted message", logs["info"])
        self.assertIn("accepted message", logs["trace"])     # one line per routed message

    def test_tcp_server_can_be_disabled(self):
        p = ports("server a b")
        router = self.router({"general": {"tcp_server_port": 0},
                              "udp_endpoints": [udp("a", p.a), udp("b", p.b)]})
        self.assertNotIn("Opened TCP Server", router.logs())
        self.assertEqual(router.inspect("{{range .Config.Env}}{{println .}}{{end}}").count(
            "MAVROUTER_HEALTH_PORT=0"), 1)       # the probe falls back to process liveness
        self.peers(some_traffic, p)

    def test_tx_queue_size(self):
        """A fork-only key: the fork build takes it, the upstream build ignores it — and then the
        container must say so, because upstream's parser would not."""
        p = ports("server a b")
        router = self.router({
            "general": {"tcp_server_port": p.server},
            "udp_endpoints": [udp("a", p.a, tx_queue_size=280),
                              udp("b", p.b, tx_queue_size=1048576)]})
        self.assertEqual(router.conf().count("TxQueueSize = "), 2)
        self.peers(some_traffic, p)
        logs = router.logs()
        if image_is_fork():
            self.assertNotIn("TxQueueSize", logs)
        else:
            self.assertIn("does not support", logs)
            self.assertIn("TxQueueSize", logs)

    def wait_for_log(self, router, text, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            logs = router.logs()
            if text in logs:
                return logs
            time.sleep(0.5)
        self.fail(f"the router never logged {text!r}:\n{router.logs()}")


# ---- peer halves ------------------------------------------------------------------------------------

def duplicate_over_two_links(p):
    """One frame sent over both links: how many copies reach the sink?"""
    link_a = mav.UdpPeer(255, 190, "gcs on link a", router=p.link_a)
    link_b = mav.UdpPeer(255, 190, "gcs on link b", router=p.link_b)
    sink = mav.UdpPeer(0, 0, "sink", bind=p.sink)

    frame = mav.v2(mav.system_time(1), 255, 190, seq=1)
    link_a.send_raw(frame)
    sink.expect(frame)                       # the first copy is through...
    link_b.send_raw(frame)                   # ...when the same bytes arrive on the other link
    end = link_b.send_raw(mav.v2(mav.system_time(2), 255, 190, seq=2))
    copies = 1 + mav.raws(sink.until(end)).count(frame)

    if p.period:
        # A frame that only LOOKS like a duplicate, after the period: forwarded again.
        time.sleep(p.period + 0.3)
        link_a.send_raw(frame)
        sink.expect(frame)
        # ...and different content is never held back, however quickly it follows.
        burst = [link_a.send_raw(mav.v2(mav.system_time(10 + n), 255, 190, seq=n))
                 for n in range(20)]
        assert mav.raws(sink.until(burst[-1])) == burst
    return copies


def sniffer_system_sees_traffic_addressed_to_others(p):
    fcu = mav.UdpPeer(1, 1, "fcu", router=p.fcu)
    sniffer = mav.UdpPeer(254, 1, "sniffer", router=p.sniffer)
    bystander = mav.UdpPeer(10, 191, "bystander", router=p.bystander)
    gcs = mav.connect(p.server, 255, 190, "gcs")
    mav.introduce(fcu, sniffer, bystander, gcs)

    command = gcs.send(mav.command_long(1, 1, 400))          # for the autopilot alone
    end = gcs.send(mav.system_time(1))
    assert mav.raws(fcu.until(end)) == [command, end]
    assert mav.raws(sniffer.until(end)) == [command, end], "the sniffer system missed it"
    assert mav.raws(bystander.until(end)) == [end], "an ordinary system received it too"


def some_traffic(p):
    a = mav.UdpPeer(1, 1, "a", router=p.a)
    b = mav.UdpPeer(255, 190, "b", router=p.b)
    mav.introduce(a, b)
    for counter in range(10):
        b.expect(a.send(mav.system_time(counter)))
        a.expect(b.send(mav.system_time(100 + counter)))


if __name__ == "__main__":
    unittest.main()
