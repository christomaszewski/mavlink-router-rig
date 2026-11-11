# Copyright 2026 Chris Tomaszewski
# SPDX-License-Identifier: Apache-2.0
"""Routing through a deployed router: which message reaches which endpoint, over every transport
the rig config can declare (UDP server, UDP normal, the TCP server, TCP client endpoints).

Each test starts the service through the launcher (host half, the test method) and then plays
the MAVLink systems around it (peer half, the module-level function of the same name, which runs
in the peer container — see harness.py).
"""
import unittest

import mav
from harness import RouterCase, ports


def udp(name, port, mode="server", address="127.0.0.1", **extra):
    return {"name": name, "mode": mode, "address": address, "port": port, **extra}


def tcp(name, port, address="127.0.0.1", **extra):
    return {"name": name, "address": address, "port": port, **extra}


class RoutingTests(RouterCase):
    def test_broadcast_reaches_every_other_endpoint(self):
        p = ports("server fcu autonomy cloud")
        self.router({
            "general": {"tcp_server_port": p.server},
            "udp_endpoints": [udp("fcu", p.fcu), udp("autonomy", p.autonomy, "normal")],
            "tcp_endpoints": [tcp("cloud", p.cloud, retry_timeout=1)],
        })
        self.peers(broadcast_reaches_every_other_endpoint, p)

    def test_targeted_message_reaches_only_its_system(self):
        p = ports("server fcu companion")
        self.router({
            "general": {"tcp_server_port": p.server},
            "udp_endpoints": [udp("fcu", p.fcu), udp("companion", p.companion)],
        })
        self.peers(targeted_message_reaches_only_its_system, p)

    def test_frames_are_forwarded_unchanged(self):
        p = ports("server a b")
        self.router({
            "general": {"tcp_server_port": p.server},
            "udp_endpoints": [udp("a", p.a), udp("b", p.b)],
        })
        self.peers(frames_are_forwarded_unchanged, p)

    def test_tcp_stream_is_reassembled(self):
        p = ports("server sink")
        self.router({
            "general": {"tcp_server_port": p.server},
            "udp_endpoints": [udp("sink", p.sink)],
        })
        self.peers(tcp_stream_is_reassembled, p)

    def test_tcp_server_clients_come_and_go(self):
        p = ports("server fcu")
        self.router({
            "general": {"tcp_server_port": p.server},
            "udp_endpoints": [udp("fcu", p.fcu)],
        })
        self.peers(tcp_server_clients_come_and_go, p)

    def test_tcp_client_endpoint_redials(self):
        p = ports("server fcu cloud")
        self.router({
            "general": {"tcp_server_port": p.server},
            "udp_endpoints": [udp("fcu", p.fcu)],
            "tcp_endpoints": [tcp("cloud", p.cloud, retry_timeout=1)],
        })
        self.peers(tcp_client_endpoint_redials, p)

    def test_udp_server_endpoint_follows_its_peer(self):
        p = ports("server fcu gcs")
        self.router({
            "general": {"tcp_server_port": p.server},
            "udp_endpoints": [udp("fcu", p.fcu), udp("gcs", p.gcs, address="0.0.0.0")],
        })
        self.peers(udp_server_endpoint_follows_its_peer, p)

    def test_ipv6_endpoints(self):
        # Router and peers share ONE container here (harness.embedded): Docker Desktop's host
        # network does not deliver IPv6 loopback datagrams, a container's own loopback does.
        p = ports("server fcu gcs autonomy relay")
        outcome = self.embedded(ipv6_endpoints, {
            "general": {"tcp_server_port": p.server},
            "udp_endpoints": [udp("fcu", p.fcu),
                              udp("gcs", p.gcs, address="[::1]"),
                              udp("autonomy", p.autonomy, "normal", address="[::1]")],
            "tcp_endpoints": [tcp("relay", p.relay, address="[::1]", retry_timeout=1)],
        }, p)
        if outcome == "no-ipv6":
            self.skipTest("containers on this Docker host have no IPv6 loopback")

    def test_sustained_stream_arrives_complete_and_in_order(self):
        p = ports("server fcu gcs")
        router = self.router({
            "general": {"tcp_server_port": p.server},
            "udp_endpoints": [udp("fcu", p.fcu), udp("gcs", p.gcs)],
        })
        container = router.container()
        self.peers(sustained_stream_arrives_complete_and_in_order, p)
        self.assertEqual(router.inspect("{{.RestartCount}}", container), "0")

    def test_garbage_does_not_disturb_routing(self):
        p = ports("server fcu gcs")
        router = self.router({
            "general": {"tcp_server_port": p.server},
            "udp_endpoints": [udp("fcu", p.fcu), udp("gcs", p.gcs)],
        })
        container = router.container()
        self.peers(garbage_does_not_disturb_routing, p)
        self.assertEqual(router.container(), container)
        self.assertEqual(router.inspect("{{.RestartCount}}", container), "0",
                         "the router died on malformed input")

    def test_router_without_endpoints_serves_tcp_clients(self):
        # The smallest valid config: no endpoint sections at all, only the TCP server.
        p = ports("server")
        self.router({"general": {"tcp_server_port": p.server}})
        self.peers(router_without_endpoints_serves_tcp_clients, p)


# ---- peer halves (run in the peer container) -------------------------------------------------------

def broadcast_reaches_every_other_endpoint(p):
    listener = mav.Listener(p.cloud)
    fcu = mav.UdpPeer(1, 1, "fcu", router=p.fcu)
    autonomy = mav.UdpPeer(20, 195, "autonomy", bind=p.autonomy)   # Normal mode: the router calls
    gcs = mav.connect(p.server, 255, 190, "gcs")
    cloud = listener.accept(30, 1, "cloud")                        # the router dials out to us
    everyone = [fcu, autonomy, gcs, cloud]
    mav.introduce(*everyone)

    sent = {}
    for index, sender in enumerate(everyone):
        sent[sender] = sender.send(mav.system_time(index))
        for receiver in everyone:
            if receiver is not sender:
                receiver.expect(sent[sender])      # byte for byte what was sent

    # ...and never back to the endpoint it came from: by the time a later broadcast of someone
    # else arrives, an echo of the sender's own would have.
    last = {peer: other.send(mav.system_time(100 + index))
            for index, (peer, other) in enumerate(zip(everyone, everyone[1:] + everyone[:1]))}
    for peer in everyone:
        peer.until(last[peer])
        own = [frame for frame in peer.seen if frame.sysid == peer.sysid]
        assert not own, f"{peer.label} got its own traffic back: {mav.listing(own)}"


def targeted_message_reaches_only_its_system(p):
    fcu = mav.UdpPeer(1, 1, "fcu", router=p.fcu)
    companion = mav.UdpPeer(10, 191, "companion", router=p.companion)
    gcs = mav.connect(p.server, 255, 190, "gcs")
    mav.introduce(fcu, companion, gcs)

    to_autopilot = gcs.send(mav.command_long(1, 1, 400))    # system 1, component 1
    to_system = gcs.send(mav.command_long(1, 0, 401))       # component 0: all of system 1
    to_stranger = gcs.send(mav.command_long(99, 0, 402))    # a system on no endpoint
    end = gcs.send(mav.system_time(1))                      # broadcast: everyone gets it

    got = mav.raws(fcu.until(end))
    assert got == [to_autopilot, to_system, end], mav.listing(fcu.seen)
    got = mav.raws(companion.until(end))
    assert got == [end], f"companion received traffic addressed elsewhere: {mav.listing(companion.seen)}"

    # the answer is routed by ITS target: back to the GCS, past the companion
    reply = fcu.send(mav.ping(255, 190, 7))
    end = fcu.send(mav.system_time(2))
    assert mav.raws(gcs.until(end)) == [reply, end]
    assert mav.raws(companion.until(end)) == [end]


def frames_are_forwarded_unchanged(p):
    a = mav.UdpPeer(1, 1, "a", router=p.a)
    b = mav.UdpPeer(255, 190, "b", router=p.b)
    mav.introduce(a, b)

    sent = [
        a.send(mav.attitude(1), version=1),                 # MAVLink 1
        a.send(mav.attitude(2)),                            # MAVLink 2
        a.send(mav.command_long(0, 0, 300), trim=True),     # zero-trimmed payload
        a.send(mav.statustext("signed"), signed=True),      # with a signature block
        a.send((mav.UNKNOWN_MSG, b"\x01\x02\x03")),         # an id the router has no entry for
    ]
    a.send_raw(mav.corrupt(mav.v2(mav.attitude(3), 1, 1)))  # known message, bad checksum: dropped
    sent.append(a.send(mav.system_time(1)))
    got = mav.raws(b.until(sent[-1]))
    assert got == sent, f"b received {mav.listing(b.seen)}"


def tcp_stream_is_reassembled(p):
    import time
    sink = mav.UdpPeer(1, 1, "sink", router=p.sink)
    gcs = mav.connect(p.server, 255, 190, "gcs")
    mav.introduce(sink, gcs)

    first, second, third = (mav.v2(mav.system_time(n), 255, 190, seq=n) for n in (1, 2, 3))
    gcs.send_raw(b"\x00\x11 line noise \x7f" + first + second)   # junk, then two frames at once
    gcs.send_raw(third[:9])                                      # one frame in two segments
    time.sleep(0.2)
    gcs.send_raw(third[9:])
    assert mav.raws(sink.until(third)) == [first, second, third], mav.listing(sink.seen)


def tcp_server_clients_come_and_go(p):
    fcu = mav.UdpPeer(1, 1, "fcu", router=p.fcu)
    one = mav.connect(p.server, 255, 190, "first client")
    two = mav.connect(p.server, 254, 190, "second client")
    mav.introduce(fcu, one, two)
    frame = fcu.send(mav.system_time(1))
    one.expect(frame)
    two.expect(frame)

    one.close()                                   # a client disappearing must not disturb the rest
    frame = fcu.send(mav.system_time(2))
    two.expect(frame)

    three = mav.connect(p.server, 253, 190, "third client")
    hello = three.hello()
    fcu.expect(hello)
    two.expect(hello)
    frame = three.send(mav.system_time(3))
    fcu.expect(frame)
    two.expect(frame)
    frame = fcu.send(mav.system_time(4))
    two.expect(frame)
    three.expect(frame)


def tcp_client_endpoint_redials(p):
    fcu = mav.UdpPeer(1, 1, "fcu", router=p.fcu)
    # Nothing was listening when the router started: it has been retrying since.
    listener = mav.Listener(p.cloud)
    cloud = listener.accept(30, 1, "cloud")
    mav.introduce(fcu, cloud)
    frame = fcu.send(mav.system_time(1))
    cloud.expect(frame)

    cloud.close()                                 # the far end drops the connection...
    cloud = listener.accept(30, 1, "cloud, second connection")   # ...and the router dials again
    hello = cloud.hello()
    fcu.expect(hello)
    frame = fcu.send(mav.system_time(2))
    cloud.expect(frame)
    frame = cloud.send(mav.system_time(3))
    fcu.expect(frame)


def udp_server_endpoint_follows_its_peer(p):
    fcu = mav.UdpPeer(1, 1, "fcu", router=p.fcu)
    gcs = mav.UdpPeer(255, 190, "gcs", router=p.gcs)
    mav.introduce(fcu, gcs)
    gcs.expect(fcu.send(mav.system_time(1)))

    # The ground station restarts on another source port: the endpoint answers the new address.
    restarted = mav.UdpPeer(255, 190, "gcs after a restart", router=p.gcs)
    fcu.expect(restarted.hello())
    frame = fcu.send(mav.system_time(2))
    restarted.expect(frame)
    assert frame not in mav.raws(gcs.collect(0.3)), "the old address still receives traffic"


def ipv6_endpoints(p):
    try:
        probe = mav.UdpPeer(0, 0, "probe", bind=p.gcs, host="::1")
        probe.close()
    except OSError:
        return "no-ipv6"
    relay_listener = mav.Listener(p.relay, host="::1")
    with mav.LocalRouter(wait_port=p.server):
        fcu = mav.UdpPeer(1, 1, "fcu", router=p.fcu)
        gcs = mav.UdpPeer(255, 190, "gcs", router=p.gcs, host="::1")              # server, [::1]
        autonomy = mav.UdpPeer(20, 195, "autonomy", bind=p.autonomy, host="::1")  # normal, [::1]
        relay = relay_listener.accept(30, 1, "relay")                             # TCP to [::1]
        everyone = [fcu, gcs, autonomy, relay]
        mav.introduce(*everyone)
        for index, sender in enumerate(everyone):
            frame = sender.send(mav.system_time(index))
            for receiver in everyone:
                if receiver is not sender:
                    receiver.expect(frame)
    return "ok"


def sustained_stream_arrives_complete_and_in_order(p):
    """4000 frames from the autopilot — a few seconds of a fast telemetry stream — to a UDP and
    a TCP consumer at once, and 2000 back. Sent in bursts that are read before the next one, as
    a consumer that keeps up would."""
    fcu = mav.UdpPeer(1, 1, "fcu", router=p.fcu)
    gcs = mav.UdpPeer(255, 190, "gcs on udp", router=p.gcs)
    tablet = mav.connect(p.server, 254, 190, "gcs on tcp")
    mav.introduce(fcu, gcs, tablet)
    counter = 0
    for _ in range(80):
        burst = [fcu.send(mav.attitude(counter + n)) for n in range(50)]
        counter += 50
        for consumer in (gcs, tablet):
            got = mav.raws(consumer.until(burst[-1]))
            assert got == burst, f"{consumer.label}: lost or reordered frames near #{counter}"
    for _ in range(40):
        burst = [tablet.send(mav.system_time(counter + n)) for n in range(50)]
        counter += 50
        assert mav.raws(fcu.until(burst[-1])) == burst, f"fcu: lost frames near #{counter}"


def garbage_does_not_disturb_routing(p):
    import random
    noise = random.Random(20261110)
    fcu = mav.UdpPeer(1, 1, "fcu", router=p.fcu)
    gcs = mav.UdpPeer(255, 190, "gcs", router=p.gcs)
    tablet = mav.connect(p.server, 254, 190, "tcp client")
    mav.introduce(fcu, gcs, tablet)
    for _ in range(40):
        # random bytes — frame markers, absurd lengths and truncated frames among them
        fcu.send_raw(bytes(noise.getrandbits(8) for _ in range(noise.randint(1, 1400))))
        tablet.send_raw(bytes(noise.getrandbits(8) for _ in range(noise.randint(1, 4096))))
    fcu.send_raw(b"")                                           # an empty datagram
    fcu.send_raw(mav.v2(mav.system_time(1), 1, 1)[:7])          # half a header, then silence
    # A fresh client proves the server still accepts; the old peers prove routing still works.
    late = mav.connect(p.server, 253, 190, "late tcp client")
    mav.introduce(fcu, gcs, late)
    for counter in range(5):
        frame = fcu.send(mav.system_time(100 + counter))
        gcs.expect(frame)
        late.expect(frame)
    fcu.expect(gcs.send(mav.command_long(1, 1, 400)))


def router_without_endpoints_serves_tcp_clients(p):
    one = mav.connect(p.server, 1, 1, "one")
    two = mav.connect(p.server, 255, 190, "two")
    mav.introduce(one, two)
    two.expect(one.send(mav.system_time(1)))
    one.expect(two.send(mav.system_time(2)))


if __name__ == "__main__":
    unittest.main()
