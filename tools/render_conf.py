#!/usr/bin/env python3
# Copyright 2026 Chris Tomaszewski
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Render a generic *rig* instance config into upstream mavlink-router's main.conf INI.

The rig config is the single source of truth (uniform shape across rig services). Known keys are
DERIVED onto upstream's INI schema ([General] + one section per endpoint); an endpoint's optional
`filters:` map is passed through (validated against upstream's filter/group key set). Used by
`mavlink-router-up`:

  render_conf.py <config.yaml>                # -> main.conf INI on stdout
  render_conf.py --env <config.yaml>          # -> SERVICE/NAME/TYPE/DEVICE*/... env lines (eval'd)
  render_conf.py --stage <dir> <config.yaml>  # -> write <dir>/<name>.conf, then the env lines plus
                                              #    CONF_HOST (that file) and CONF_SHA256 (its content)

Absent keys are OMITTED from the INI so upstream's own defaults apply instead of this script
fabricating divergent ones. Unknown keys are a HARD error — a typo must not pass as "configured" —
and so is every value upstream would refuse at startup (a port of 0, a hostname where it wants an
IP literal, an unknown log level): the router runs under `restart: unless-stopped`, so a config it
rejects is a crash loop on the vehicle instead of an error at `up`. Every mode runs the SAME
validation, so `--env` can never accept a config the INI render would refuse.
Needs PyYAML on the host (apt: python3-yaml).
"""
import hashlib
import os
import re
import shlex
import sys
import tempfile

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("render_conf: PyYAML required on the host (apt install python3-yaml)\n")
    sys.exit(2)


# Instance identity tokens end up in compose project names, staging file paths, and an eval'd env
# block in the launcher — keep them to a safe, unambiguous alphabet. The service token also allows
# '-' (it is 'mavlink-router', and only ever lands in the compose project name, where '-' is legal).
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SERVICE_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# Upstream's own address checks (src/endpoint.cpp validate_ipv4 / validate_ipv6), verbatim: it
# takes IP literals only — no hostnames — and IPv6 must be bracketed, lowercase hex.
_IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_IPV6_RE = re.compile(r"^\[(([a-f\d]{0,4}:)+[a-f\d]{0,4})\]$")

_LOG_LEVELS = ("error", "warning", "info", "debug", "trace")
_DIALECTS = ("auto", "common", "ardupilotmega")
_LOG_MODES = ("always", "while-armed")
_UDP_MODES = ("normal", "server")

# Upstream's per-endpoint message/system/component filters + endpoint grouping, passed through
# from an endpoint's `filters:` map. Matched case-insensitively (upstream's parser is too), emitted
# with the canonical casing. The value is the largest id upstream's parser accepts for that key
# (message ids are uint32, system/component ids uint8); None marks the one string key.
_UINT32, _UINT8 = 2**32 - 1, 255
_FILTER_KEYS = {
    "AllowMsgIdOut": _UINT32, "BlockMsgIdOut": _UINT32,
    "AllowSrcCompOut": _UINT8, "BlockSrcCompOut": _UINT8,
    "AllowSrcSysOut": _UINT8, "BlockSrcSysOut": _UINT8,
    "AllowMsgIdIn": _UINT32, "BlockMsgIdIn": _UINT32,
    "AllowSrcCompIn": _UINT8, "BlockSrcCompIn": _UINT8,
    "AllowSrcSysIn": _UINT8, "BlockSrcSysIn": _UINT8,
    "Group": None,
}
_FILTER_CANON = {k.lower(): k for k in _FILTER_KEYS}

# Every key the rig config accepts, per block. tests/unit holds the example config to this list:
# an option added here must be documented there.
_TOP_KEYS = {"service", "name", "general", "uart_endpoints", "udp_endpoints", "tcp_endpoints",
             "logging"}
_GENERAL_KEYS = {"tcp_server_port", "report_stats", "debug_log_level", "dedup_period_ms",
                 "mavlink_dialect", "sniffer_sysid"}
_LOGGING_KEYS = {"enabled", "mode", "telemetry", "log_system_id", "max_log_files",
                 "min_free_space_mb"}
_UART_KEYS = {"name", "device", "baud", "flow_control", "tx_queue_size", "filters"}
_UDP_KEYS = {"name", "mode", "address", "port", "tx_queue_size", "filters"}
_TCP_KEYS = {"name", "address", "port", "retry_timeout", "tx_queue_size", "filters"}

# The serial compose overlays carry one device each — compose.deploy.serial.yaml for the first
# UART, compose.deploy.serial<N>.yaml for the Nth — so the number of UART endpoints is bounded by
# the overlay files that exist. Raising it means adding an overlay (and its launch_surface row).
_MAX_UARTS = 4

# The fork's per-endpoint transmit queue (TxQueueSize), in bytes: its own accepted range. The
# upstream build has no such key and ignores it; the image's entrypoint says so at start.
_TX_QUEUE_MIN, _TX_QUEUE_MAX = 280, 1048576

# Container-side root of the logging overlay's RIG_DATA_DIR bind (compose.deploy.logging.yaml).
# The rendered `Log =` is the FLAT layout under it; the image's entrypoint re-points the router at
# the open rig run (<data>/current/mavlink-router/<name>) when there is one — see entrypoint.sh.
_DATA_ROOT = "/data"
_DATA_KIND = "mavlink-router"


def die(msg: str) -> None:
    sys.stderr.write(f"render_conf: {msg}\n")
    sys.exit(2)


def require_ident(what: str, value, pattern=_IDENT_RE) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        die(f"{what} {value!r} must match {pattern.pattern} "
            "(it becomes a compose project / file name / env token)")
    return value


def mapping(what: str, value) -> dict:
    """A config block that must be a mapping (absent/null = empty)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        die(f"`{what}` must be a mapping")
    return value


def reject_unknown(what: str, block: dict, known) -> None:
    unknown = sorted(str(k) for k in block if k not in known)
    if unknown:
        die(f"unknown {what} key(s): {', '.join(unknown)} (supported: {', '.join(sorted(known))})")


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def want_bool(what: str, value) -> bool:
    if not isinstance(value, bool):
        die(f"{what} must be true or false")
    return value


def want_int(what: str, value, low: int, high=None) -> int:
    if not is_int(value) or value < low or (high is not None and value > high):
        bound = f"{low}..{high}" if high is not None else f">= {low}"
        die(f"{what} must be an integer {bound}, not {value!r}")
    return value


def want_choice(what: str, value, choices) -> str:
    if value not in choices:
        die(f"{what} must be one of {' | '.join(choices)}, not {value!r}")
    return value


def want_address(what: str, value) -> str:
    if not isinstance(value, str) or not (_IPV4_RE.match(value) or _IPV6_RE.match(value)):
        die(f"{what} {value!r} must be an IP literal — dotted IPv4, or bracketed lowercase IPv6 "
            "like [::1]; upstream does not resolve hostnames")
    return value


def want_port(what: str, value) -> int:
    return want_int(what, value, 1, 65535)


def want_ids(what: str, value, high: int) -> list:
    """An id, a list of ids, or upstream's own "1,2,3" spelling -> list of ints (upstream parses
    a comma-separated unsigned list)."""
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        if not all(part.isascii() and part.isdigit() for part in parts):
            die(f"{what} must be an integer or a list of integers, not {value!r}")
        value = [int(part) for part in parts]
    values = value if isinstance(value, list) else [value]
    if not values:
        die(f"{what} must not be an empty list")
    return [want_int(what, v, 0, high) for v in values]


def parse_filters(where: str, endpoint: dict) -> list:
    """An endpoint's optional `filters:` map -> [(CanonicalKey, ini_value)]."""
    out, seen = [], set()
    for key, value in mapping(f"{where}: filters", endpoint.get("filters")).items():
        canonical = _FILTER_CANON.get(str(key).lower())
        if canonical is None:
            die(f"{where}: unknown filter key {key!r} "
                f"(supported: {', '.join(sorted(_FILTER_KEYS))})")
        if canonical in seen:
            die(f"{where}: filter key {canonical} is given twice (keys are case-insensitive)")
        seen.add(canonical)
        high = _FILTER_KEYS[canonical]
        if high is None:  # Group: endpoints sharing a group name do not forward to each other
            if is_int(value):
                value = str(value)  # `Group: 1` is a perfectly good group name
            if not isinstance(value, str) or not re.match(r"^[A-Za-z0-9_.-]+$", value):
                die(f"{where}: filter {canonical} must be a name of letters, digits, '_', '.', '-'")
            out.append((canonical, value))
        else:
            ids = want_ids(f"{where}: filter {canonical}", value, high)
            out.append((canonical, ",".join(str(i) for i in ids)))
    return out


def tx_queue(where: str, endpoint: dict) -> list:
    """An endpoint's optional `tx_queue_size:` -> [("TxQueueSize", text)] or []."""
    if "tx_queue_size" not in endpoint:
        return []
    size = want_int(f"{where}: tx_queue_size", endpoint["tx_queue_size"],
                    _TX_QUEUE_MIN, _TX_QUEUE_MAX)
    return [("TxQueueSize", str(size))]


def parse(cfg: dict) -> dict:
    """Validate the whole config -> a normalized model every output mode renders from. Each
    section is an ordered list of (IniKey, text) pairs holding ONLY the keys the user set."""
    reject_unknown("top-level", cfg, _TOP_KEYS)
    service = require_ident("service", cfg.get("service") or "mavlink-router", _SERVICE_RE)
    name = require_ident("name", cfg.get("name") or "main")

    general = mapping("general", cfg.get("general"))
    reject_unknown("general", general, _GENERAL_KEYS)
    lines = []
    tcp_server_port = 5760  # upstream's default when omitted
    if "tcp_server_port" in general:
        tcp_server_port = want_int("general.tcp_server_port", general["tcp_server_port"], 0, 65535)
        lines.append(("TcpServerPort", str(tcp_server_port)))  # 0 disables the TCP server
    if "report_stats" in general:
        lines.append(("ReportStats",
                      _bool(want_bool("general.report_stats", general["report_stats"]))))
    if "debug_log_level" in general:
        lines.append(("DebugLogLevel", want_choice("general.debug_log_level",
                                                   general["debug_log_level"], _LOG_LEVELS)))
    if "dedup_period_ms" in general:
        lines.append(("DeduplicationPeriod",
                      str(want_int("general.dedup_period_ms", general["dedup_period_ms"], 0))))
    if "mavlink_dialect" in general:
        lines.append(("MavlinkDialect", want_choice("general.mavlink_dialect",
                                                    general["mavlink_dialect"], _DIALECTS)))
    if "sniffer_sysid" in general:
        lines.append(("SnifferSysid",
                      str(want_int("general.sniffer_sysid", general["sniffer_sysid"], 0, 255))))

    logging_blk = mapping("logging", cfg.get("logging"))
    reject_unknown("logging", logging_blk, _LOGGING_KEYS)
    log_enabled = want_bool("logging.enabled", logging_blk.get("enabled", False))
    # Validated even when disabled: a bad value must not lie dormant until someone flips `enabled`.
    log_lines = [("Log", f"{_DATA_ROOT}/{_DATA_KIND}/{name}")]
    if "mode" in logging_blk:
        log_lines.append(("LogMode", want_choice("logging.mode", logging_blk["mode"], _LOG_MODES)))
    if "telemetry" in logging_blk:
        log_lines.append(("LogTelemetry",
                          _bool(want_bool("logging.telemetry", logging_blk["telemetry"]))))
    if "log_system_id" in logging_blk:
        log_lines.append(("LogSystemId", str(want_int("logging.log_system_id",
                                                      logging_blk["log_system_id"], 1, 255))))
    if "max_log_files" in logging_blk:
        log_lines.append(("MaxLogFiles", str(want_int("logging.max_log_files",
                                                      logging_blk["max_log_files"], 0))))
    if "min_free_space_mb" in logging_blk:  # upstream's MinFreeSpace is bytes
        mb = want_int("logging.min_free_space_mb", logging_blk["min_free_space_mb"], 0)
        log_lines.append(("MinFreeSpace", str(mb * 1024 * 1024)))
    if log_enabled:
        lines += log_lines

    raw = {}
    for kind in ("uart_endpoints", "udp_endpoints", "tcp_endpoints"):
        value = cfg.get(kind)
        if value is not None and not isinstance(value, list):
            die(f"`{kind}:` must be a list")
        raw[kind] = value or []
    # One fixed compose overlay per serial device — never silently drop one.
    if len(raw["uart_endpoints"]) > _MAX_UARTS:
        die(f"at most {_MAX_UARTS} UART endpoints are supported (one serial compose overlay per "
            "device; run a second instance for more)")

    sections, seen_names, devices = [], set(), []

    def begin(kind: str, endpoint, known) -> str:
        if not isinstance(endpoint, dict):
            die(f"each {kind} endpoint must be a mapping")
        ep_name = require_ident(f"{kind} endpoint name", endpoint.get("name") or "")
        # upstream names sections case-insensitively, and so far names are lowercase anyway
        if ep_name in seen_names:
            die(f"duplicate endpoint name {ep_name!r}")
        seen_names.add(ep_name)
        reject_unknown(f"{kind} endpoint {ep_name!r}", endpoint, known)
        return ep_name

    for endpoint in raw["uart_endpoints"]:
        ep = begin("uart", endpoint, _UART_KEYS)
        where = f"uart endpoint {ep!r}"
        device = endpoint.get("device")
        if not isinstance(device, str) or not device.startswith("/"):
            die(f"{where} needs an absolute `device:` path (use the stable "
                "/dev/serial/by-id/... form)")
        if ":" in device or any(c.isspace() or not c.isprintable() for c in device):
            die(f"{where}: device path {device!r} contains ':', whitespace or a control "
                "character — it breaks the compose device mapping; use the "
                "/dev/serial/by-id/... form (not by-path)")
        if device in devices:
            die(f"{where}: device {device} is already used by another UART endpoint")
        devices.append(device)
        body = [("Device", device)]
        if "baud" in endpoint:  # a list = upstream cycles through the rates until one syncs
            bauds = want_ids(f"{where}: baud", endpoint["baud"], _UINT32)
            if 0 in bauds:
                die(f"{where}: baud must be positive")
            body.append(("Baud", ",".join(str(b) for b in bauds)))
        if "flow_control" in endpoint:
            body.append(("FlowControl",
                         _bool(want_bool(f"{where}: flow_control", endpoint["flow_control"]))))
        sections.append((f"UartEndpoint {ep}",
                         body + tx_queue(where, endpoint) + parse_filters(where, endpoint)))

    for endpoint in raw["udp_endpoints"]:
        ep = begin("udp", endpoint, _UDP_KEYS)
        where = f"udp endpoint {ep!r}"
        mode = endpoint.get("mode")
        if mode not in _UDP_MODES:
            die(f"{where}: mode must be 'normal' (send-to) or 'server' (bind + wait)")
        body = [("Mode", mode.capitalize()),
                ("Address", want_address(f"{where}: address", endpoint.get("address")))]
        if "port" in endpoint:
            body.append(("Port", str(want_port(f"{where}: port", endpoint["port"]))))
        elif mode == "server":  # normal mode without a port: upstream picks the next free 14550+
            die(f"{where}: server mode requires `port:`")
        sections.append((f"UdpEndpoint {ep}",
                         body + tx_queue(where, endpoint) + parse_filters(where, endpoint)))

    for endpoint in raw["tcp_endpoints"]:
        ep = begin("tcp", endpoint, _TCP_KEYS)
        where = f"tcp endpoint {ep!r}"
        if "port" not in endpoint:
            die(f"{where}: `port:` is required")
        body = [("Address", want_address(f"{where}: address", endpoint.get("address"))),
                ("Port", str(want_port(f"{where}: port", endpoint["port"])))]
        if "retry_timeout" in endpoint:  # seconds; 0 = never reconnect
            body.append(("RetryTimeout", str(want_int(f"{where}: retry_timeout",
                                                      endpoint["retry_timeout"], 0))))
        sections.append((f"TcpEndpoint {ep}",
                         body + tx_queue(where, endpoint) + parse_filters(where, endpoint)))

    if not sections and tcp_server_port == 0:
        die("nothing to route: no endpoints and the TCP server is disabled")

    return {
        "service": service, "name": name, "general": lines, "sections": sections,
        "tcp_server_port": tcp_server_port, "log_enabled": log_enabled,
        "devices": devices,
    }


def _bool(value: bool) -> str:
    return "true" if value else "false"


def render_ini(model: dict) -> str:
    out = [f"# rendered by render_conf.py for instance '{model['name']}' — edit the rig config, "
           "not this file", "", "[General]"]
    out += [f"{key} = {value}" for key, value in model["general"]]
    for title, body in model["sections"]:
        out += ["", f"[{title}]"] + [f"{key} = {value}" for key, value in body]
    text = "\n".join(out) + "\n"
    # Belt and braces behind the per-field checks above: one config value must never be able to
    # open a second INI line (and with it another key, or a whole endpoint section).
    if len(text.splitlines()) != len(out) or any(not line.isprintable() for line in out):
        die("a config value contains a line break or control character")
    return text


def render_env(model: dict, extra=()) -> str:
    """The launcher's identity/derived tokens, shell-quoted for a safe `eval` (device paths may
    contain characters an unquoted eval would mangle)."""
    devices = model["devices"] + [""] * (_MAX_UARTS - len(model["devices"]))
    tokens = [
        ("SERVICE", model["service"]),
        ("NAME", model["name"]),
        ("TYPE", "serial" if model["devices"] else "net"),
        # one token per serial overlay slot, in uart_endpoints order: DEVICE, DEVICE_2, ...
        *((f"DEVICE_{slot}" if slot > 1 else "DEVICE", device)
          for slot, device in enumerate(devices, start=1)),
        ("HEALTH_PORT", str(model["tcp_server_port"])),
        ("LOGGING", "1" if model["log_enabled"] else "0"),
        *extra,
    ]
    return "\n".join(f"{key}={shlex.quote(value)}" for key, value in tokens) + "\n"


def stage(directory: str, model: dict, text: str) -> str:
    """Write the rendered INI to <directory>/<name>.conf and return that path. The file is the
    single-file bind mount of a possibly RUNNING router, and every launcher verb lands here
    (`status` included), so: an unchanged render is not rewritten at all, and a changed one is
    swapped in by rename — never truncated in place, where a container restarting at that
    instant would start on half a config."""
    path = os.path.join(directory, f"{model['name']}.conf")
    try:
        with open(path, encoding="utf-8") as handle:
            if handle.read() == text:
                return path
    except (OSError, UnicodeDecodeError):
        pass
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{model['name']}.conf.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mode, stage_dir = "ini", None
    if args and args[0] == "--env":
        mode, args = "env", args[1:]
    elif len(args) >= 2 and args[0] == "--stage":
        mode, stage_dir, args = "stage", args[1], args[2:]
    if len(args) != 1 or args[0].startswith("--"):
        sys.stderr.write("usage: render_conf.py [--env | --stage <dir>] <config.yaml>\n")
        return 2

    try:
        with open(args[0], encoding="utf-8") as handle:
            cfg = yaml.safe_load(handle)
    except OSError as exc:
        die(f"cannot read config {args[0]}: {exc.strerror or exc}")
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        die(f"config {args[0]} is not valid YAML: {' '.join(str(exc).split())}")
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        die("config must be a YAML mapping")

    model = parse(cfg)
    text = render_ini(model)
    if mode == "ini":
        sys.stdout.write(text)
    elif mode == "env":
        sys.stdout.write(render_env(model))
    else:
        try:
            path = stage(stage_dir, model, text)
        except OSError as exc:
            die(f"cannot stage the rendered config under {stage_dir}: {exc.strerror or exc}")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        sys.stdout.write(render_env(model, (("CONF_HOST", path), ("CONF_SHA256", digest))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
