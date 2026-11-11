# Copyright 2026 Chris Tomaszewski
# SPDX-License-Identifier: Apache-2.0
"""`logging:` — the telemetry log, the flight-stack logs of both autopilot families, when a log
starts and stops, where it lands (the open rig run, or the flat layout), and retention.

The peer half plays the autopilot: it answers the router's log-streaming requests the way
ArduPilot (dataflash blocks) and PX4 (ULog over MAVLink) do, then reads back the files the router
wrote — the data dir is mounted into the peer container at /data, as it is in the router.
"""
import unittest

import mav
from harness import RouterCase, ports

LOG_NAME = r"^\d{5}-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.(bin|ulg|tlog)$"


class LoggingTests(RouterCase):
    def logging_router(self, open_run=None, general=None, **logging):
        p = ports("server fcu gcs")
        data = self.data_dir(open_run)
        router = self.router({
            "general": {"tcp_server_port": p.server, **(general or {})},
            "udp_endpoints": [
                {"name": "fcu", "mode": "server", "address": "127.0.0.1", "port": p.fcu},
                {"name": "gcs", "mode": "server", "address": "127.0.0.1", "port": p.gcs}],
            "logging": {"enabled": True, **logging},
        }, env={"RIG_DATA_DIR": str(data)})
        p.flat = f"/data/mavlink-router/{router.name}"
        p.name = router.name
        return router, p, data

    def by_extension(self, files):
        counts = {}
        for name in files:
            self.assertRegex(name, LOG_NAME)
            extension = name.rsplit(".", 1)[1]
            counts[extension] = counts.get(extension, 0) + 1
        return counts

    def test_telemetry_log_records_the_stream(self):
        router, p, _ = self.logging_router(telemetry=True)
        self.assertIn(f"Log = {p.flat}\n", router.conf())
        self.assertNotIn("--log", router.argv())           # no run open: the flat layout
        result = self.peers(telemetry_log_records_the_stream, p, data=self.data_dir())
        # a generic autopilot has no flight-stack log protocol: the telemetry log is all there is
        self.assertEqual(self.by_extension(result["files"]), {"tlog": 1})
        # The log is an endpoint like any other, so by default it is sent what an endpoint with
        # no known systems is sent: broadcasts, not messages addressed to one system.
        self.assertEqual(result["addressed_logged"], [False, False])

    def test_sniffing_as_the_log_system_records_addressed_messages_too(self):
        """The router's loggers are MAVLink system 2: naming that system the sniffer makes the
        telemetry log a complete record (commands and their replies included)."""
        router, p, _ = self.logging_router(telemetry=True, general={"sniffer_sysid": 2})
        result = self.peers(telemetry_log_records_the_stream, p, data=self.data_dir())
        self.assertEqual(result["addressed_logged"], [True, True])

    def test_ardupilot_dataflash_log(self):
        router, p, _ = self.logging_router()
        files = self.peers(ardupilot_dataflash_log, p, data=self.data_dir())
        self.assertEqual(self.by_extension(files), {"bin": 1})   # telemetry: false by default

    def test_px4_ulog(self):
        router, p, _ = self.logging_router()
        files = self.peers(px4_ulog, p, data=self.data_dir())
        self.assertEqual(self.by_extension(files), {"ulg": 1})

    def test_dialect_forces_the_log_protocol(self):
        """mavlink_dialect: ardupilotmega asks for a dataflash log whatever the heartbeat says."""
        p = ports("server fcu gcs")
        data = self.data_dir()
        router = self.router({
            "general": {"tcp_server_port": p.server, "mavlink_dialect": "ardupilotmega"},
            "udp_endpoints": [
                {"name": "fcu", "mode": "server", "address": "127.0.0.1", "port": p.fcu},
                {"name": "gcs", "mode": "server", "address": "127.0.0.1", "port": p.gcs}],
            "logging": {"enabled": True},
        }, env={"RIG_DATA_DIR": str(data)})
        p.flat = f"/data/mavlink-router/{router.name}"
        self.peers(dialect_forces_the_log_protocol, p, data=data)

    def test_while_armed_logs_one_file_per_flight(self):
        router, p, _ = self.logging_router(mode="while-armed", telemetry=True)
        files = self.peers(while_armed_logs_one_file_per_flight, p, data=self.data_dir())
        self.assertEqual(self.by_extension(files), {"tlog": 2})

    def test_log_system_id_selects_the_vehicle(self):
        router, p, _ = self.logging_router(log_system_id=7)
        self.peers(log_system_id_selects_the_vehicle, p, data=self.data_dir())

    def test_logs_land_in_the_open_run_and_stay_there(self):
        first, second = "20260101T000000Z_first", "20260102T000000Z_second"
        router, p, data = self.logging_router(open_run=first, telemetry=True)
        in_first = f"/data/runs/{first}/mavlink-router/{router.name}"
        in_second = f"/data/runs/{second}/mavlink-router/{router.name}"
        self.assertIn(f"--log {in_first}", router.argv())

        tree = self.peers(one_flight, p, data=data)
        self.assertEqual(self.by_extension(tree[in_first]), {"tlog": 1})
        self.assertEqual(list(tree), [in_first], "nothing may be written outside the open run")

        # rig rotates the run while the router keeps running: the recording stays where it began
        self.open_run(second)
        tree = self.peers(one_flight, p, data=data)
        self.assertEqual(list(tree), [in_first])

        # the next start of the container picks up the run that is open then
        router.down()
        router.up()
        self.assertIn(f"--log {in_second}", router.argv())
        tree = self.peers(one_flight, p, data=data)
        self.assertEqual(self.by_extension(tree[in_second]), {"tlog": 1})
        self.assertEqual(self.by_extension(tree[in_first]), {"tlog": 1})

    def test_without_a_usable_run_the_flat_layout_is_used(self):
        router, p, data = self.logging_router(telemetry=True)
        (data / "current").symlink_to("runs/gone")                 # a dangling link
        router.down()
        router.up()
        self.assertNotIn("--log", router.argv())
        self.assertIn("dangling", router.logs())
        tree = self.peers(one_flight, p, data=data)
        self.assertEqual(list(tree), [p.flat])

    def test_max_log_files_prunes_the_oldest(self):
        router, p, data = self.logging_router(telemetry=True, max_log_files=2)
        names = []
        for _ in range(5):
            tree = self.peers(one_flight, p, data=data)
            names.append(sorted(tree[p.flat]))
            router.down()
            router.up()
        # At each start the router keeps the 2 newest FINISHED logs and opens a new one.
        self.assertEqual([len(found) for found in names], [1, 2, 3, 3, 3])
        self.assertEqual(names[4][:2], names[3][1:], "the oldest log must be the one removed")
        self.assertNotIn(names[0][0], names[4])

    def test_min_free_space_prunes_finished_logs(self):
        # More free space demanded than any disk has: every finished log goes at the next start.
        router, p, data = self.logging_router(telemetry=True, min_free_space_mb=1 << 40)
        self.assertIn(f"MinFreeSpace = {(1 << 40) * 1024 * 1024}\n", router.conf())
        counts = []
        for _ in range(3):
            tree = self.peers(one_flight, p, data=data)
            counts.append(len(tree[p.flat]))
            router.down()
            router.up()
        self.assertEqual(counts, [1, 1, 1])

    def test_retention_with_flight_stack_and_telemetry_logs(self):
        """Both logs on: every start writes a .bin AND a .tlog, and retention has to keep up
        with both."""
        router, p, data = self.logging_router(telemetry=True, max_log_files=2)
        counts = []
        for _ in range(5):
            self.peers(ardupilot_dataflash_log, p, data=data)
            router.down()
            router.up()
            counts.append(len(self.peers(list_logs, p, data=data)))
        self.assertLessEqual(max(counts), 4, f"log files pile up despite max_log_files: {counts}")


# ---- peer halves ------------------------------------------------------------------------------------

def _files(directory):
    import os
    return sorted(os.listdir(directory)) if os.path.isdir(directory) else []


def _tree(root="/data"):
    """{directory: [file names]} of every directory under the data dir that holds log files."""
    import os
    found = {}
    for directory, _, files in os.walk(root):
        logs = sorted(name for name in files if name.rsplit(".", 1)[-1] in ("bin", "ulg", "tlog"))
        if logs:
            found[directory] = logs
    return dict(sorted(found.items()))


def _wait(what, check, timeout=10.0):
    """Poll `check()` until it returns something truthy (log IO may trail the routing)."""
    import time
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}; data dir holds {_tree()}")
        time.sleep(0.1)


def _newest(directory, extension):
    """Path of the latest log of that kind (the names start with a running counter), or None."""
    names = [name for name in _files(directory) if name.endswith("." + extension)]
    return f"{directory}/{names[-1]}" if names else None


def _beat_until(fcu, match, timeout=10.0, **heartbeat):
    """Heartbeat a few times a second, like a real autopilot, until a frame matching `match`
    arrives. (The router's loggers advance on traffic: one heartbeat makes it pick the log
    protocol, the following ones start the log.)"""
    import time
    deadline = time.monotonic() + timeout
    while True:
        fcu.send(mav.heartbeat(**heartbeat))
        try:
            return fcu.expect(match, timeout=0.3)
        except AssertionError:
            if time.monotonic() >= deadline:
                raise


def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


def _tlog(path):
    """[(timestamp in microseconds, raw frame)] of a telemetry log."""
    data, entries = _read(path), []
    while len(data) >= 8:
        frames = mav.Parser().feed(data[8:])
        if not frames or data[8] not in (mav.V1_STX, mav.V2_STX):
            break
        entries.append((int.from_bytes(data[:8], "big"), frames[0].raw))
        data = data[8 + len(frames[0].raw):]
    return entries


def list_logs(p):
    return _files(p.flat)


def one_flight(p):
    """An autopilot shows up and some telemetry flows; returns the log tree afterwards."""
    fcu = mav.UdpPeer(1, mav.COMP_AUTOPILOT, "fcu", router=p.fcu)
    gcs = mav.UdpPeer(255, 190, "gcs", router=p.gcs)
    mav.introduce(fcu, gcs)
    last = None
    for counter in range(5):
        last = fcu.send(mav.system_time(counter))
        gcs.expect(last)
    _wait("the telemetry frames on disk", lambda: any(
        last in [raw for _, raw in _tlog(f"{directory}/{name}")]
        for directory, names in _tree().items() for name in names if name.endswith(".tlog")))
    return _tree()


def telemetry_log_records_the_stream(p):
    import time
    gcs = mav.UdpPeer(255, 190, "gcs", router=p.gcs)
    companion = mav.connect(p.server, 10, 191, "companion")
    mav.introduce(gcs, companion)
    companion.expect(gcs.send(mav.system_time(1)))
    assert not _files(p.flat), "a log was started before any autopilot was heard"

    fcu = mav.UdpPeer(1, mav.COMP_AUTOPILOT, "fcu", router=p.fcu)   # component 1: the autopilot
    mav.introduce(fcu, gcs, companion)
    started = time.time()
    # One at a time, each seen to arrive before the next is sent: traffic on different
    # endpoints has no order of its own, and the log is compared in order below.
    sent = []
    for sender, receiver, message, options in (
            (fcu, companion, mav.attitude(1), {"version": 1}),
            (gcs, companion, mav.system_time(2), {}),
            (fcu, companion, mav.statustext("hello tlog"), {}),
            (gcs, fcu, mav.command_long(1, 1, 400), {}),       # addressed to the autopilot
            (companion, gcs, mav.ping(255, 190, 3), {}),       # addressed to the ground station
            (fcu, gcs, mav.system_time(3), {})):
        sent.append(sender.send(message, **options))
        receiver.expect(sent[-1])

    path = _wait("the telemetry log", lambda: _newest(p.flat, "tlog"))
    entries = _wait("the frames in the telemetry log",
                    lambda: (log := _tlog(path)) and sent[-1] in [raw for _, raw in log] and log)
    logged = [raw for _, raw in entries]
    broadcast = [sent[0], sent[1], sent[2], sent[5]]
    assert [raw for raw in logged if raw in broadcast] == broadcast, "broadcast frames, in order"
    stamps = [stamp for stamp, _ in entries]
    assert stamps == sorted(stamps), "timestamps go backwards"
    assert all(abs(stamp / 1e6 - started) < 120 for stamp in stamps), "timestamps are not UTC now"
    return {"files": _files(p.flat),
            "addressed_logged": [raw in logged for raw in (sent[3], sent[4])]}


def ardupilot_dataflash_log(p):
    fcu = mav.UdpPeer(1, mav.COMP_AUTOPILOT, "ardupilot", router=p.fcu)

    # The router (as system 2) asks the autopilot to start streaming its dataflash log...
    request = _beat_until(fcu, lambda frame: frame.msgid == mav.REMOTE_LOG_BLOCK_STATUS,
                          autopilot=mav.AUTOPILOT_ARDUPILOT)
    status = mav.parse_remote_log_block_status(request.payload)
    assert request.sysid == mav.LOG_SYSTEM, f"request came from system {request.sysid}"
    assert status == {"seqno": mav.REMOTE_LOG_START, "target_system": 1, "status": 1}, status

    # ...and acknowledges every 200-byte block it stores.
    blocks = [bytes((seqno * 7 + offset) % 251 for offset in range(200)) for seqno in range(12)]
    for seqno, block in enumerate(blocks):
        fcu.send(mav.remote_log_data_block(seqno, block))
        fcu.expect(lambda frame, seqno=seqno: frame.msgid == mav.REMOTE_LOG_BLOCK_STATUS
                   and mav.parse_remote_log_block_status(frame.payload)
                   == {"seqno": seqno, "target_system": 1, "status": 1})

    _wait("all blocks in the dataflash log",
          lambda: (path := _newest(p.flat, "bin")) and _read(path) == b"".join(blocks))
    return _files(p.flat)


def px4_ulog(p):
    import struct
    fcu = mav.UdpPeer(1, mav.COMP_AUTOPILOT, "px4", router=p.fcu)

    # The router asks PX4 to start ULog streaming (MAV_CMD_LOGGING_START) until it is accepted.
    request = _beat_until(fcu, lambda frame: frame.msgid == mav.COMMAND_LONG,
                          autopilot=mav.AUTOPILOT_PX4)
    command = mav.parse_command_long(request.payload)
    assert request.sysid == mav.LOG_SYSTEM
    assert command == {"command": mav.CMD_LOGGING_START, "target_system": 1,
                       "target_component": 0}, command
    fcu.send(mav.command_ack(mav.CMD_LOGGING_START))

    # A ULog stream: the 16-byte file header, then messages of (size, type, body).
    stream = b"ULog\x01\x12\x35" + b"\x01" + struct.pack("<Q", 123456)
    for index in range(30):
        body = bytes((index + offset) % 256 for offset in range(40 + index))
        stream += struct.pack("<HB", len(body), ord("I")) + body
    chunks = [stream[start:start + 200] for start in range(0, len(stream), 200)]
    for sequence, chunk in enumerate(chunks):       # messages straddle the chunk boundaries
        fcu.send(mav.logging_data_acked(sequence, chunk))
        fcu.expect(lambda frame, sequence=sequence: frame.msgid == mav.LOGGING_ACK
                   and mav.parse_logging_ack(frame.payload)
                   == {"sequence": sequence, "target_system": 1})

    _wait("the whole stream in the ULog file",
          lambda: (path := _newest(p.flat, "ulg")) and _read(path) == stream)
    return _files(p.flat)


def dialect_forces_the_log_protocol(p):
    fcu = mav.UdpPeer(1, mav.COMP_AUTOPILOT, "fcu", router=p.fcu)
    # the heartbeat says PX4; the config says ArduPilot
    request = _beat_until(fcu, lambda frame: frame.sysid == mav.LOG_SYSTEM,
                          autopilot=mav.AUTOPILOT_PX4)
    assert request.msgid == mav.REMOTE_LOG_BLOCK_STATUS, mav.describe(request)


def while_armed_logs_one_file_per_flight(p):
    fcu = mav.UdpPeer(1, mav.COMP_AUTOPILOT, "fcu", router=p.fcu)
    gcs = mav.UdpPeer(255, 190, "gcs", router=p.gcs)
    mav.introduce(fcu, gcs)

    def tlogs():
        return [name for name in _files(p.flat) if name.endswith(".tlog")]

    def fly(counter):
        gcs.expect(fcu.send(mav.heartbeat(base_mode=mav.ARMED)))          # arm
        _wait("a log for this flight", lambda: len(tlogs()) == counter)
        in_flight = fcu.send(mav.system_time(counter))
        gcs.expect(in_flight)
        gcs.expect(fcu.send(mav.heartbeat(base_mode=0, custom_mode=counter)))   # disarm
        return in_flight

    on_ground = fcu.send(mav.system_time(100))
    gcs.expect(on_ground)
    assert not tlogs(), "a log was started while disarmed"
    first = fly(1)
    between = fcu.send(mav.system_time(101))
    gcs.expect(between)
    second = fly(2)

    names = tlogs()
    logged = [[raw for _, raw in _tlog(f"{p.flat}/{name}")] for name in names]
    _wait("both flights on disk", lambda: first in [raw for _, raw in _tlog(f"{p.flat}/{names[0]}")]
          and second in [raw for _, raw in _tlog(f"{p.flat}/{names[1]}")])
    logged = [[raw for _, raw in _tlog(f"{p.flat}/{name}")] for name in names]
    assert second not in logged[0] and first not in logged[1], "the flights are mixed up"
    for raw in (on_ground, between):
        assert raw not in logged[0] + logged[1], "telemetry was logged while disarmed"
    return names


def log_system_id_selects_the_vehicle(p):
    other = mav.UdpPeer(1, mav.COMP_AUTOPILOT, "vehicle 1", router=p.gcs)
    chosen = mav.UdpPeer(7, mav.COMP_AUTOPILOT, "vehicle 7", router=p.fcu)
    for _ in range(3):      # vehicle 1 is heard first, and repeatedly
        other.send(mav.heartbeat(autopilot=mav.AUTOPILOT_ARDUPILOT))
    request = _beat_until(chosen, lambda frame: frame.msgid == mav.REMOTE_LOG_BLOCK_STATUS,
                          autopilot=mav.AUTOPILOT_ARDUPILOT)
    assert mav.parse_remote_log_block_status(request.payload)["target_system"] == 7
    # by the time vehicle 7's next heartbeat is relayed, a request to vehicle 1 would have arrived
    marker = chosen.send(mav.heartbeat(autopilot=mav.AUTOPILOT_ARDUPILOT, custom_mode=9))
    asked = [frame for frame in other.until(marker) if frame.sysid == mav.LOG_SYSTEM]
    assert not asked, f"vehicle 1 was asked for its log: {mav.listing(asked)}"


if __name__ == "__main__":
    unittest.main()
