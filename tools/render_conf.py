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
`filters:` map is passed VERBATIM (validated against upstream's filter/group key set). Used by
`mavlink-router-up`:

  render_conf.py <config.yaml>          # -> main.conf INI on stdout
  render_conf.py --env <config.yaml>    # -> SERVICE/NAME/TYPE/DEVICE/... env lines (eval'd)

Absent keys are OMITTED from the INI so upstream's own defaults apply instead of this script
fabricating divergent ones. Unknown keys are a HARD error — a typo must not pass as "configured".
Needs PyYAML on the host (apt: python3-yaml).
"""
import re
import shlex
import sys

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

# Upstream's per-endpoint message/system/component filters + endpoint grouping — passed verbatim
# from an endpoint's `filters:` map. Matched case-insensitively (upstream's parser is too), emitted
# with the canonical casing.
_FILTER_KEYS = {k.lower(): k for k in (
    "AllowMsgIdOut", "BlockMsgIdOut", "AllowSrcCompOut", "BlockSrcCompOut",
    "AllowSrcSysOut", "BlockSrcSysOut",
    "AllowMsgIdIn", "BlockMsgIdIn", "AllowSrcCompIn", "BlockSrcCompIn",
    "AllowSrcSysIn", "BlockSrcSysIn",
    "Group",
)}


def die(msg: str) -> None:
    sys.stderr.write(f"render_conf: {msg}\n")
    sys.exit(2)


def require_ident(what: str, value: str, pattern: re.Pattern = _IDENT_RE) -> str:
    if not pattern.match(value):
        die(f"{what} {value!r} must match {pattern.pattern} "
            "(it becomes a compose project / file name / env token)")
    return value


def reject_unknown(what: str, block: dict, known) -> None:
    unknown = sorted(set(block) - set(known))
    if unknown:
        die(f"unknown {what} key(s): {', '.join(unknown)} (supported: {', '.join(sorted(known))})")


def _ini_value(value):
    """A config scalar/list -> upstream INI text. Bools are true/false; lists comma-join."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return str(value)


def _take(block: dict, out: list, src_key: str, ini_key: str, want=None) -> None:
    """Append `ini_key = value` for block[src_key] only when the user provided it — absent keys
    are OMITTED so upstream's own defaults apply. `want` optionally type-checks the value."""
    if src_key not in block:
        return
    value = block[src_key]
    if want is bool and not isinstance(value, bool):
        die(f"{src_key} must be a bool")
    if want is int and (isinstance(value, bool) or not isinstance(value, int)):
        die(f"{src_key} must be an integer")
    out.append(f"{ini_key} = {_ini_value(value)}")


def _filters(endpoint: dict, out: list) -> None:
    """Validate + pass through an endpoint's optional `filters:` map verbatim."""
    filters = endpoint.get("filters") or {}
    if not isinstance(filters, dict):
        die("`filters:` must be a mapping")
    for key, value in filters.items():
        canonical = _FILTER_KEYS.get(str(key).lower())
        if canonical is None:
            die(f"unknown filter key {key!r} (supported: {', '.join(sorted(_FILTER_KEYS.values()))})")
        out.append(f"{canonical} = {_ini_value(value)}")


def parse(cfg: dict) -> dict:
    """Validate the whole config -> a normalized model both output modes render from."""
    reject_unknown("top-level", cfg, {
        "service", "name", "general", "uart_endpoints", "udp_endpoints", "tcp_endpoints", "logging",
    })
    service = require_ident("service", str(cfg.get("service") or "mavlink-router"), _SERVICE_RE)
    name = require_ident("name", str(cfg.get("name") or "main"))

    general = cfg.get("general") or {}
    reject_unknown("general", general, {
        "tcp_server_port", "report_stats", "debug_log_level", "dedup_period_ms",
        "mavlink_dialect", "sniffer_sysid",
    })
    tcp_server_port = general.get("tcp_server_port", 5760)  # upstream's default when omitted
    if isinstance(tcp_server_port, bool) or not isinstance(tcp_server_port, int) or tcp_server_port < 0:
        die("general.tcp_server_port must be a non-negative integer (0 disables the TCP server)")

    logging_blk = cfg.get("logging") or {}
    reject_unknown("logging", logging_blk, {
        "enabled", "mode", "telemetry", "log_system_id", "max_log_files", "min_free_space_mb",
    })
    log_enabled = logging_blk.get("enabled", False)
    if not isinstance(log_enabled, bool):
        die("logging.enabled must be a bool")
    log_mode = logging_blk.get("mode")
    if log_mode is not None and log_mode not in ("always", "while-armed"):
        die("logging.mode must be 'always' or 'while-armed'")

    uarts = cfg.get("uart_endpoints") or []
    udps = cfg.get("udp_endpoints") or []
    tcps = cfg.get("tcp_endpoints") or []
    for what, lst in (("uart_endpoints", uarts), ("udp_endpoints", udps), ("tcp_endpoints", tcps)):
        if not isinstance(lst, list):
            die(f"`{what}:` must be a list")
    # ONE serial slot: the compose serial overlay passes exactly one device through. A second UART
    # needs a second overlay slot — extend deliberately, do not silently drop a device.
    if len(uarts) > 1:
        die("at most one UART endpoint is supported (the serial overlay carries one device)")

    seen_names: set = set()
    for endpoint in uarts + udps + tcps:
        if not isinstance(endpoint, dict):
            die("each endpoint must be a mapping")
        ep_name = require_ident("endpoint name", str(endpoint.get("name") or ""))
        if ep_name in seen_names:
            die(f"duplicate endpoint name {ep_name!r}")
        seen_names.add(ep_name)

    device = ""
    for endpoint in uarts:
        reject_unknown("uart endpoint", endpoint, {"name", "device", "baud", "flow_control", "filters"})
        device = str(endpoint.get("device") or "")
        if not device.startswith("/"):
            die("uart endpoint needs an absolute `device:` path (use the stable /dev/serial/by-id/... form)")
        if ":" in device or any(c.isspace() for c in device):
            die(f"device path {device!r} contains ':' or whitespace — it breaks the compose device "
                "mapping; use the /dev/serial/by-id/... form (not by-path)")
    for endpoint in udps:
        reject_unknown("udp endpoint", endpoint, {"name", "mode", "address", "port", "filters"})
        mode = str(endpoint.get("mode") or "")
        if mode not in ("normal", "server"):
            die(f"udp endpoint {endpoint.get('name')!r}: mode must be 'normal' (send-to) or "
                "'server' (bind + wait)")
        if not endpoint.get("address"):
            die(f"udp endpoint {endpoint.get('name')!r}: `address:` is required")
        if mode == "server" and "port" not in endpoint:
            die(f"udp endpoint {endpoint.get('name')!r}: server mode requires `port:`")
    for endpoint in tcps:
        reject_unknown("tcp endpoint", endpoint, {"name", "address", "port", "retry_timeout", "filters"})
        if not endpoint.get("address") or "port" not in endpoint:
            die(f"tcp endpoint {endpoint.get('name')!r}: `address:` and `port:` are required")

    if not (uarts or udps or tcps) and tcp_server_port == 0:
        die("nothing to route: no endpoints and the TCP server is disabled")

    return {
        "service": service, "name": name, "general": general,
        "tcp_server_port": tcp_server_port, "logging": logging_blk, "log_enabled": log_enabled,
        "uarts": uarts, "udps": udps, "tcps": tcps, "device": device,
    }


def render_ini(model: dict) -> str:
    lines = [f"# rendered by render_conf.py for instance '{model['name']}' — edit the rig config, "
             "not this file", "", "[General]"]
    general, logging_blk = model["general"], model["logging"]
    _take(general, lines, "tcp_server_port", "TcpServerPort", int)
    _take(general, lines, "report_stats", "ReportStats", bool)
    _take(general, lines, "debug_log_level", "DebugLogLevel")
    _take(general, lines, "dedup_period_ms", "DeDuplicationPeriod", int)
    _take(general, lines, "mavlink_dialect", "MavlinkDialect")
    _take(general, lines, "sniffer_sysid", "SnifferSysid", int)
    if model["log_enabled"]:
        # container-side path of the logging overlay's RIG_DATA_DIR bind (compose.deploy.logging.yaml)
        lines.append("Log = /var/log/mavlink-router")
        _take(logging_blk, lines, "mode", "LogMode")
        _take(logging_blk, lines, "telemetry", "LogTelemetry", bool)
        _take(logging_blk, lines, "log_system_id", "LogSystemId", int)
        _take(logging_blk, lines, "max_log_files", "MaxLogFiles", int)
        if "min_free_space_mb" in logging_blk:  # upstream's MinFreeSpace is bytes
            mb = logging_blk["min_free_space_mb"]
            if isinstance(mb, bool) or not isinstance(mb, int) or mb < 0:
                die("logging.min_free_space_mb must be a non-negative integer (MB)")
            lines.append(f"MinFreeSpace = {mb * 1024 * 1024}")

    for endpoint in model["uarts"]:
        lines += ["", f"[UartEndpoint {endpoint['name']}]", f"Device = {endpoint['device']}"]
        _take(endpoint, lines, "baud", "Baud", int)
        _take(endpoint, lines, "flow_control", "FlowControl", bool)
        _filters(endpoint, lines)
    for endpoint in model["udps"]:
        lines += ["", f"[UdpEndpoint {endpoint['name']}]",
                  f"Mode = {endpoint['mode'].capitalize()}", f"Address = {endpoint['address']}"]
        _take(endpoint, lines, "port", "Port", int)
        _filters(endpoint, lines)
    for endpoint in model["tcps"]:
        lines += ["", f"[TcpEndpoint {endpoint['name']}]",
                  f"Address = {endpoint['address']}"]
        _take(endpoint, lines, "port", "Port", int)
        _take(endpoint, lines, "retry_timeout", "RetryTimeout", int)
        _filters(endpoint, lines)
    return "\n".join(lines) + "\n"


def render_env(model: dict) -> str:
    """The launcher's identity/derived tokens, shell-quoted for a safe `eval` (device paths may
    contain characters an unquoted eval would mangle)."""
    tokens = [
        ("SERVICE", model["service"]),
        ("NAME", model["name"]),
        ("TYPE", "serial" if model["uarts"] else "net"),
        ("DEVICE", model["device"]),
        ("HEALTH_PORT", str(model["tcp_server_port"])),
        ("LOGGING", "1" if model["log_enabled"] else "0"),
    ]
    return "\n".join(f"{key}={shlex.quote(value)}" for key, value in tokens) + "\n"


def main() -> int:
    args = sys.argv[1:]
    env_mode = bool(args and args[0] == "--env")
    if env_mode:
        args = args[1:]
    if len(args) != 1:
        sys.stderr.write("usage: render_conf.py [--env] <config.yaml>\n")
        return 2

    with open(args[0]) as handle:
        cfg = yaml.safe_load(handle) or {}
    if not isinstance(cfg, dict):
        die("config must be a YAML mapping")

    model = parse(cfg)
    sys.stdout.write(render_env(model) if env_mode else render_ini(model))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
