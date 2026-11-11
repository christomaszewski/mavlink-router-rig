# Copyright 2026 Chris Tomaszewski
# SPDX-License-Identifier: Apache-2.0
"""Just enough MAVLink for the functional tests: v1/v2 framing with the X.25 checksum, the few
messages the scenarios speak, and UDP/TCP/serial peers that send and collect whole frames.

Standard library only — this module runs inside the peer container (the image under test plus
python3), where nothing else is installed. The ids, payload layouts and CRC seeds below are the
ones in the mavlink C library the router is built against (modules/mavlink_c_library_v2 at the
pinned commit), so a frame built here is one the router's own parser accepts.
"""
import collections
import os
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time

V1_STX, V2_STX = 0xFE, 0xFD
SIGNATURE_LEN = 13

# msgid -> CRC seed (CRC_EXTRA) of every message the router must recognise in these tests.
HEARTBEAT, SYS_STATUS, SYSTEM_TIME, PING, ATTITUDE = 0, 1, 2, 4, 30
COMMAND_LONG, COMMAND_ACK, STATUSTEXT = 76, 77, 253
REMOTE_LOG_DATA_BLOCK, REMOTE_LOG_BLOCK_STATUS = 184, 185
LOGGING_DATA, LOGGING_DATA_ACKED, LOGGING_ACK = 266, 267, 268
CRC_EXTRA = {
    HEARTBEAT: 50, SYS_STATUS: 124, SYSTEM_TIME: 137, PING: 237, ATTITUDE: 39,
    COMMAND_LONG: 152, COMMAND_ACK: 143, STATUSTEXT: 83,
    REMOTE_LOG_DATA_BLOCK: 159, REMOTE_LOG_BLOCK_STATUS: 186,
    LOGGING_DATA: 193, LOGGING_DATA_ACKED: 35, LOGGING_ACK: 14,
}
# A message id no dialect defines: the router has no table entry for it and forwards it as is.
UNKNOWN_MSG = 45000

AUTOPILOT_GENERIC, AUTOPILOT_ARDUPILOT, AUTOPILOT_INVALID, AUTOPILOT_PX4 = 0, 3, 8, 12
ARMED = 0x80                        # MAV_MODE_FLAG_SAFETY_ARMED
COMP_AUTOPILOT = 1                  # MAV_COMP_ID_AUTOPILOT1
LOG_SYSTEM = 2                      # the router's own system id when it talks to a flight stack
REMOTE_LOG_START = 2147483646       # MAV_REMOTE_LOG_DATA_BLOCK_START
CMD_LOGGING_START = 2510            # MAV_CMD_LOGGING_START

Frame = collections.namedtuple("Frame", "version msgid sysid compid seq payload raw")


def x25(data, crc=0xFFFF):
    for byte in data:
        tmp = (byte ^ crc) & 0xFF
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def _crc_extra(msgid, crc_extra):
    return CRC_EXTRA.get(msgid, 0) if crc_extra is None else crc_extra


def v2(msg, sysid, compid, seq=0, crc_extra=None, signed=False, trim=False):
    """One MAVLink 2 frame. `trim` drops trailing zero bytes of the payload the way real senders
    do; `signed` appends a (dummy) 13-byte signature block and sets the incompat flag."""
    msgid, payload = msg
    if trim:
        payload = payload.rstrip(b"\0") or payload[:1]
    head = struct.pack("<BBBBBB", len(payload), 1 if signed else 0, 0, seq & 0xFF, sysid, compid)
    head += struct.pack("<I", msgid)[:3]
    crc = x25(head + payload + bytes([_crc_extra(msgid, crc_extra)]))
    frame = bytes([V2_STX]) + head + payload + struct.pack("<H", crc)
    return frame + bytes(range(1, SIGNATURE_LEN + 1)) if signed else frame


def v1(msg, sysid, compid, seq=0, crc_extra=None):
    msgid, payload = msg
    head = struct.pack("<BBBBB", len(payload), seq & 0xFF, sysid, compid, msgid)
    crc = x25(head + payload + bytes([_crc_extra(msgid, crc_extra)]))
    return bytes([V1_STX]) + head + payload + struct.pack("<H", crc)


def corrupt(frame):
    """The same frame with its checksum broken (signature blocks are not used with this)."""
    return frame[:-1] + bytes([frame[-1] ^ 0xFF])


class Parser:
    """Incremental frame splitter. It trusts the length byte and does not verify checksums: the
    tests compare whole frames byte for byte against what was sent."""

    def __init__(self):
        self.buffer = b""

    def feed(self, data):
        self.buffer += data
        frames = []
        while self.buffer:
            if self.buffer[0] not in (V1_STX, V2_STX):
                self.buffer = self.buffer[1:]
                continue
            is_v2 = self.buffer[0] == V2_STX
            header = 10 if is_v2 else 6
            if len(self.buffer) < header:
                break
            size = header + self.buffer[1] + 2
            if is_v2 and self.buffer[2] & 1:
                size += SIGNATURE_LEN
            if len(self.buffer) < size:
                break
            raw, self.buffer = self.buffer[:size], self.buffer[size:]
            if is_v2:
                msgid = int.from_bytes(raw[7:10], "little")
                frames.append(Frame(2, msgid, raw[5], raw[6], raw[4], raw[10:10 + raw[1]], raw))
            else:
                frames.append(Frame(1, raw[5], raw[3], raw[4], raw[2], raw[6:6 + raw[1]], raw))
        return frames


# ---- messages: each returns (msgid, full-length payload) -----------------------------------------

def heartbeat(autopilot=AUTOPILOT_INVALID, base_mode=0, custom_mode=0, mav_type=6):
    return HEARTBEAT, struct.pack("<IBBBBB", custom_mode, mav_type, autopilot, base_mode, 4, 3)


def system_time(counter):
    """A broadcast message with no target fields; `counter` makes each one distinct."""
    return SYSTEM_TIME, struct.pack("<QI", 1_700_000_000_000_000 + counter, counter)


def attitude(counter):
    return ATTITUDE, struct.pack("<I6f", counter, 0.1, 0.2, 0.3, 0.0, 0.0, 0.0)


def statustext(text):
    return STATUSTEXT, struct.pack("<B50s", 6, text.encode())


def ping(target_system, target_component, counter=0):
    return PING, struct.pack("<QIBB", 1_700_000_000_000_000, counter, target_system,
                             target_component)


def command_long(target_system, target_component, command, param1=0.0):
    return COMMAND_LONG, struct.pack("<7fHBBB", param1, 0, 0, 0, 0, 0, 0, command,
                                     target_system, target_component, 0)


def command_ack(command, result=0):
    return COMMAND_ACK, struct.pack("<HB", command, result)


def remote_log_data_block(seqno, data, target_system=LOG_SYSTEM, target_component=0):
    return REMOTE_LOG_DATA_BLOCK, struct.pack("<IBB200s", seqno, target_system, target_component,
                                              data)


def logging_data_acked(sequence, data, first_message_offset=0, target_system=LOG_SYSTEM,
                       target_component=0):
    return LOGGING_DATA_ACKED, struct.pack("<HBBBB249s", sequence, target_system,
                                           target_component, len(data), first_message_offset,
                                           data)


def parse_command_long(payload):
    payload = payload.ljust(33, b"\0")
    values = struct.unpack("<7fHBBB", payload)
    return {"command": values[7], "target_system": values[8], "target_component": values[9]}


def parse_remote_log_block_status(payload):
    seqno, target_system, target_component, status = struct.unpack("<IBBB", payload.ljust(7, b"\0"))
    return {"seqno": seqno, "target_system": target_system, "status": status}


def parse_logging_ack(payload):
    sequence, target_system, _ = struct.unpack("<HBB", payload.ljust(4, b"\0"))
    return {"sequence": sequence, "target_system": target_system}


# ---- peers --------------------------------------------------------------------------------------

class Peer:
    """One MAVLink system on the far side of a router endpoint. `send` frames a message as this
    system; `until`/`collect` return the frames the router delivered."""

    def __init__(self, sysid, compid, label):
        self.sysid, self.compid, self.label = sysid, compid, label
        self.seq = 0
        self.parser = Parser()
        self.seen = []          # every frame ever received, in order
        self.cursor = 0         # index into `seen` of the next frame until()/collect() hand out

    # transport hooks
    def _write(self, data):
        raise NotImplementedError

    def _read(self, timeout):
        """Bytes that arrived within `timeout`, b"" when nothing did."""
        raise NotImplementedError

    def can_send(self):
        return True

    def close(self):
        pass

    def send(self, msg, version=2, **kwargs):
        frame = (v2 if version == 2 else v1)(msg, self.sysid, self.compid, self.seq, **kwargs)
        self.seq += 1
        self._write(frame)
        return frame

    def send_raw(self, data):
        self._write(data)
        return data

    def hello(self):
        """Announce this system (and, on a UDP server endpoint, this address) to the router."""
        return self.send(heartbeat())

    def _pump(self, timeout):
        self.seen += self.parser.feed(self._read(timeout))

    def _next(self, deadline):
        """The next frame not yet handed out, or None once `deadline` passes."""
        while self.cursor >= len(self.seen):
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self._pump(left)
        self.cursor += 1
        return self.seen[self.cursor - 1]

    def collect(self, seconds):
        """Everything that arrives in the next `seconds`."""
        frames, deadline = [], time.monotonic() + seconds
        while (frame := self._next(deadline)) is not None:
            frames.append(frame)
        return frames

    def until(self, match, timeout=5.0):
        """The frames received up to and including the first one matching `match` (raw frame
        bytes, or a predicate over a Frame). The router forwards in arrival order, so by the time
        a later message of the same sender shows up, anything it was going to deliver from an
        earlier one has arrived — which is how the tests assert that something was NOT sent."""
        test = (lambda frame: frame.raw == match) if isinstance(match, bytes) else match
        frames, deadline = [], time.monotonic() + timeout
        while (frame := self._next(deadline)) is not None:
            frames.append(frame)
            if test(frame):
                return frames
        want = describe_raw(match) if isinstance(match, bytes) else "the expected frame"
        raise AssertionError(f"{self.label}: {want} did not arrive within {timeout}s "
                             f"(received: {listing(frames)})")

    def expect(self, match, timeout=5.0):
        return self.until(match, timeout)[-1]

    def expect_all(self, wanted, timeout=5.0):
        """Wait until every raw frame in `wanted` has arrived, in any order."""
        missing, frames, deadline = set(wanted), [], time.monotonic() + timeout
        while missing:
            frame = self._next(deadline)
            if frame is None:
                names = ", ".join(sorted(describe_raw(raw) for raw in missing))
                raise AssertionError(f"{self.label}: still waiting for {names} after {timeout}s "
                                     f"(received: {listing(frames)})")
            frames.append(frame)
            missing.discard(frame.raw)
        return frames


def introduce(*peers, rounds=6):
    """Heartbeats all round, repeated until every peer has heard every other one in the SAME
    round. After this the router knows which system sits behind which endpoint, and has PROCESSED
    it — messages arriving on different endpoints are otherwise not ordered against each other.

    One round is not enough, by design of the router: a UDP server endpoint has nowhere to send
    until its peer has spoken once (so whatever was broadcast before that is gone), and a peer
    behind a Normal-mode endpoint cannot speak until the router has called it. Real systems
    paper over both with their 1 Hz heartbeat; so does this."""
    wait = 0.3
    for _ in range(rounds):
        hellos = {peer: peer.hello() for peer in peers if peer.can_send()}
        complete = len(hellos) == len(peers)
        for peer in peers:
            try:
                peer.expect_all([raw for other, raw in hellos.items() if other is not peer], wait)
            except AssertionError:
                complete = False
        if complete:
            return
        wait = min(wait * 2, 3.0)
    heard = "; ".join(f"{peer.label} heard {listing(peer.seen)}" for peer in peers)
    raise AssertionError(f"the peers never all heard each other through the router: {heard}")


class UdpPeer(Peer):
    """A UDP socket bound to `bind` on `host` (port 0 = ephemeral). Datagrams go to the router
    port `router` when given, otherwise to whoever sent the last datagram received (a router
    endpoint in Normal mode calls first)."""

    def __init__(self, sysid, compid, label, router=None, bind=0, host="127.0.0.1"):
        super().__init__(sysid, compid, label)
        self.sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET,
                                  socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, bind))
        self.dest = (host, router) if router else None

    def can_send(self):
        return self.dest is not None

    def _write(self, data):
        if self.dest is None:
            raise AssertionError(f"{self.label}: nowhere to send — the router has not called yet")
        self.sock.sendto(data, self.dest)

    def _read(self, timeout):
        if not select.select([self.sock], [], [], timeout)[0]:
            return b""
        try:
            data, sender = self.sock.recvfrom(65535)
        except ConnectionError:   # ICMP port-unreachable for an earlier datagram of ours
            return b""
        if self.dest is None:
            self.dest = sender
        return data

    def close(self):
        self.sock.close()


class StreamPeer(Peer):
    """A connected TCP socket: a client of the router's TCP server, or the far end of one of the
    router's own TcpEndpoint connections (see `listen`)."""

    def __init__(self, sysid, compid, label, sock):
        super().__init__(sysid, compid, label)
        self.sock = sock
        self.closed = False

    def _write(self, data):
        self.sock.sendall(data)

    def _read(self, timeout):
        if self.closed or not select.select([self.sock], [], [], timeout)[0]:
            return b""
        try:
            data = self.sock.recv(65535)
        except ConnectionError:
            data = b""
        if not data:
            self.closed = True
        return data

    def close(self):
        self.sock.close()


class SerialPeer(Peer):
    """The device side of a serial link: the master of a pseudo-terminal whose slave the router
    opens as its UART (see `open_pty`)."""

    def __init__(self, sysid, compid, label, master):
        super().__init__(sysid, compid, label)
        self.master = master

    def _write(self, data):
        os.write(self.master, data)

    def _read(self, timeout):
        if not select.select([self.master], [], [], timeout)[0]:
            return b""
        try:
            return os.read(self.master, 65535)
        except OSError:
            return b""


def connect(port, sysid, compid, label, timeout=10.0):
    """A TCP client of the router's server port, retried while the router is still starting."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=2)
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return StreamPeer(sysid, compid, label, sock)
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise AssertionError(f"{label}: no TCP server on port {port}: {exc}") from exc
            time.sleep(0.1)


class Listener:
    """A TCP server the router's TcpEndpoint connects out to."""

    def __init__(self, port, host="127.0.0.1"):
        self.sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET,
                                  socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(4)

    def accept(self, sysid, compid, label, timeout=15.0):
        if not select.select([self.sock], [], [], timeout)[0]:
            raise AssertionError(f"{label}: the router did not connect within {timeout}s")
        sock, _ = self.sock.accept()
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return StreamPeer(sysid, compid, label, sock)

    def close(self):
        self.sock.close()


def open_pty(link):
    """A raw pseudo-terminal with its slave published at `link` (e.g. /dev/ttyFCU). Returns
    (master_fd, slave_fd); keep the slave open so the master never reads EIO between opens."""
    import tty
    master, slave = os.openpty()
    tty.setraw(slave)
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(os.ttyname(slave), link)
    return master, slave


class LocalRouter:
    """The router started INSIDE the peer container — the image's own entrypoint, on the main.conf
    the harness mounted — instead of by the launcher next to it. For what needs the router in
    this container's own namespaces: a pseudo-terminal as its UART, or a loopback that Docker
    Desktop's host network cannot offer (it drops IPv6 loopback datagrams).

        with mav.LocalRouter(wait_port=p.server) as router:
            ...                     # router.output() is its log so far
    """

    def __init__(self, *args, wait_port=None, timeout=10.0):
        self.log = tempfile.TemporaryFile()
        self.process = subprocess.Popen(["/usr/local/bin/entrypoint.sh", *args],
                                        stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + timeout
        while wait_port:
            try:
                socket.create_connection(("127.0.0.1", wait_port), timeout=1).close()
                break
            except OSError:
                if self.process.poll() is not None or time.monotonic() >= deadline:
                    raise AssertionError(f"the router did not come up:\n{self.stop()}") from None
                time.sleep(0.1)

    def output(self):
        self.log.seek(0)
        return self.log.read().decode(errors="replace")

    def wait_for(self, text, timeout=10.0):
        """Block until `text` shows up in the router's log."""
        deadline = time.monotonic() + timeout
        while text not in self.output():
            if time.monotonic() >= deadline:
                raise AssertionError(f"the router never logged {text!r}:\n{self.output()}")
            time.sleep(0.1)

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        return self.output()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        output = self.stop()
        if kind is not None:
            sys.stderr.write(f"--- router (in the peer container) ---\n{output}")
        return False


# ---- reporting ------------------------------------------------------------------------------------

def describe(frame):
    return f"v{frame.version} msg {frame.msgid} from {frame.sysid}/{frame.compid} seq {frame.seq}"


def describe_raw(raw):
    frames = Parser().feed(raw)
    return describe(frames[0]) if frames else f"{len(raw)} raw bytes"


def listing(frames):
    return ", ".join(describe(frame) for frame in frames) or "nothing"


def raws(frames):
    return [frame.raw for frame in frames]
