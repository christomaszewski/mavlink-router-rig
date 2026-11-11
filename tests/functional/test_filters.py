# Copyright 2026 Chris Tomaszewski
# SPDX-License-Identifier: Apache-2.0
"""Per-endpoint `filters:` — every Allow/Block key in both directions, and Group — checked on a
deployed router by what actually comes out of each endpoint.

Layout of the filter tests: two MAVLink systems (1/1 and 2/2) send the same mix of messages, and
one endpoint per filter key shows what that key lets through. Each sink is compared against the
EXACT list it should receive; the traffic ends with heartbeats from both systems, twice, so every
sink's last expected frame comes after anything that could have leaked.
"""
import unittest

import mav
from harness import RouterCase, ports

A, B = (1, 1), (2, 2)                      # (system, component) of the two senders
HEARTBEAT, SYSTEM_TIME, ATTITUDE = mav.HEARTBEAT, mav.SYSTEM_TIME, mav.ATTITUDE

# filter key -> (value in the rig config, predicate over (msgid, system, component) it must act as)
OUT_FILTERS = {
    "AllowMsgIdOut": ([HEARTBEAT, ATTITUDE], lambda msgid, sys, comp: msgid in (HEARTBEAT, ATTITUDE)),
    "BlockMsgIdOut": ([ATTITUDE], lambda msgid, sys, comp: msgid != ATTITUDE),
    "AllowSrcSysOut": (1, lambda msgid, sys, comp: sys == 1),
    "BlockSrcSysOut": (1, lambda msgid, sys, comp: sys != 1),
    "AllowSrcCompOut": ([2], lambda msgid, sys, comp: comp == 2),
    "BlockSrcCompOut": ([2], lambda msgid, sys, comp: comp != 2),
}
IN_FILTERS = {key.replace("Out", "In"): value for key, value in OUT_FILTERS.items()}


def udp(name, port, mode="server", **extra):
    return {"name": name, "mode": mode, "address": "127.0.0.1", "port": port, **extra}


class FilterTests(RouterCase):
    def test_outgoing_filters(self):
        names = [key.lower() for key in OUT_FILTERS]
        p = ports("server source plain combined " + " ".join(names))
        self.router({
            "general": {"tcp_server_port": p.server},
            "udp_endpoints": [
                udp("source", p.source),
                udp("plain", p.plain, "normal"),
                *(udp(key.lower(), getattr(p, key.lower()), "normal", filters={key: value})
                  for key, (value, _) in OUT_FILTERS.items()),
                # several keys on one endpoint: a message has to pass all of them
                udp("combined", p.combined, "normal",
                    filters={"AllowMsgIdOut": [HEARTBEAT, SYSTEM_TIME], "BlockSrcSysOut": 2}),
            ],
        })
        self.peers(outgoing_filters, p)

    def test_incoming_filters(self):
        names = [key.lower() for key in IN_FILTERS]
        p = ports("server sink " + " ".join(names))
        self.router({
            "general": {"tcp_server_port": p.server},
            "udp_endpoints": [
                udp("sink", p.sink, "normal"),
                *(udp(key.lower(), getattr(p, key.lower()), filters={key: value})
                  for key, (value, _) in IN_FILTERS.items()),
            ],
        })
        self.peers(incoming_filters, p)

    def test_filters_apply_to_tcp_endpoints(self):
        p = ports("server source relay")
        self.router({
            "general": {"tcp_server_port": p.server},
            "udp_endpoints": [udp("source", p.source)],
            "tcp_endpoints": [{"name": "relay", "address": "127.0.0.1", "port": p.relay,
                               "retry_timeout": 1,
                               "filters": {"AllowMsgIdOut": [HEARTBEAT], "BlockMsgIdIn": ATTITUDE}}],
        })
        self.peers(filters_apply_to_tcp_endpoints, p)

    def test_grouped_endpoints_do_not_feed_each_other(self):
        p = ports("server link_a link_b solo vehicle")
        self.router({
            "general": {"tcp_server_port": p.server},
            "udp_endpoints": [
                udp("link_a", p.link_a, filters={"Group": "gcs_links"}),
                udp("link_b", p.link_b, "normal", filters={"Group": "gcs_links"}),
                udp("solo", p.solo, "normal"),
                udp("vehicle", p.vehicle),
            ],
        })
        self.peers(grouped_endpoints_do_not_feed_each_other, p)


# ---- peer halves ------------------------------------------------------------------------------------

def _traffic(first, second):
    """The same message mix from both senders, then the closing heartbeats. Returns
    [(msgid, system, component, raw frame)] in send order; every frame is distinct."""
    sent, counter = [], iter(range(1, 1000))

    def send(peer, msg):
        sent.append((msg[0], peer.sysid, peer.compid, peer.send(msg)))

    for peer in (first, second):
        send(peer, mav.heartbeat(custom_mode=next(counter)))
        send(peer, mav.attitude(next(counter)))
        send(peer, mav.system_time(next(counter)))
    for peer in (first, second, first, second):
        send(peer, mav.heartbeat(custom_mode=next(counter)))
    return sent


def _expect_exactly(sink, sent, passes):
    expected = [raw for msgid, sysid, compid, raw in sent if passes(msgid, sysid, compid)]
    got = mav.raws(sink.until(expected[-1]))
    assert got == expected, (f"{sink.label}: expected {len(expected)} frames, received "
                             f"{mav.listing(sink.seen)}")


def outgoing_filters(p):
    sinks = {name: mav.UdpPeer(0, 0, name, bind=port) for name, port in vars(p).items()
             if name not in ("server", "source")}
    first = mav.UdpPeer(*A, "sender 1/1", router=p.source)
    second = mav.UdpPeer(*B, "sender 2/2", router=p.source)
    sent = _traffic(first, second)

    _expect_exactly(sinks["plain"], sent, lambda msgid, sys, comp: True)
    for key, (_, passes) in OUT_FILTERS.items():
        _expect_exactly(sinks[key.lower()], sent, passes)
    _expect_exactly(sinks["combined"], sent,
                    lambda msgid, sys, comp: msgid in (HEARTBEAT, SYSTEM_TIME) and sys != 2)


def incoming_filters(p):
    sink = mav.UdpPeer(0, 0, "sink", bind=p.sink)
    for key, (_, passes) in IN_FILTERS.items():
        port = getattr(p, key.lower())
        first = mav.UdpPeer(*A, f"sender 1/1 into {key}", router=port)
        second = mav.UdpPeer(*B, f"sender 2/2 into {key}", router=port)
        sink.label = f"sink behind {key}"
        _expect_exactly(sink, _traffic(first, second), passes)


def filters_apply_to_tcp_endpoints(p):
    listener = mav.Listener(p.relay)
    source = mav.UdpPeer(1, 1, "source", router=p.source)
    relay = listener.accept(30, 1, "relay")
    mav.introduce(source, relay)

    # out of the relay endpoint: heartbeats only
    blocked = source.send(mav.system_time(1))
    passed = source.send(mav.heartbeat(custom_mode=7))
    got = mav.raws(relay.until(passed))
    assert blocked not in got, "AllowMsgIdOut let SYSTEM_TIME out of the TCP endpoint"

    # into it: everything but ATTITUDE
    blocked = relay.send(mav.attitude(1))
    passed = relay.send(mav.system_time(2))
    got = mav.raws(source.until(passed))
    assert blocked not in got, "BlockMsgIdIn accepted ATTITUDE from the TCP endpoint"


def grouped_endpoints_do_not_feed_each_other(p):
    gcs_a = mav.UdpPeer(255, 190, "gcs on link a", router=p.link_a)
    gcs_b = mav.UdpPeer(255, 190, "gcs on link b", bind=p.link_b)     # the second link, idle
    solo = mav.UdpPeer(0, 0, "ungrouped endpoint", bind=p.solo)
    vehicle = mav.UdpPeer(1, 1, "vehicle", router=p.vehicle)
    mav.introduce(gcs_a, vehicle)

    # What arrives on link a goes to the vehicle and to an ungrouped endpoint...
    from_gcs = gcs_a.send(mav.system_time(1))
    vehicle.expect(from_gcs)
    solo.expect(from_gcs)
    # ...the vehicle's traffic goes out on BOTH links of the group...
    from_vehicle = vehicle.send(mav.system_time(2))
    gcs_a.expect(from_vehicle)
    gcs_b.until(from_vehicle)
    # ...but nothing the ground station sent on link a was looped back to it over link b.
    looped = [frame for frame in gcs_b.seen if frame.sysid == 255]
    assert not looped, f"link b carried the ground station's own traffic: {mav.listing(looped)}"

    # The same the other way round, once link b is in use too.
    from_gcs_b = gcs_b.send(mav.system_time(3))
    vehicle.expect(from_gcs_b)
    from_vehicle = vehicle.send(mav.system_time(4))
    assert from_gcs_b not in mav.raws(gcs_a.until(from_vehicle))


if __name__ == "__main__":
    unittest.main()
