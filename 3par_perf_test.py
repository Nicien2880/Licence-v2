#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
3par_perf_test.py

Single-file test collector for HPE 3PAR / StoreServ performance monitoring in Zabbix.

What it does:
- connects to 3PAR over SSH using username/password;
- runs basic health/performance CLI commands;
- returns one JSON document suitable for a Zabbix external check master item;
- stores command output under raw.* and a best-effort parsed table under parsed.*.

Recommended Zabbix usage:
  Type: External check
  Key: 3par/3par_perf_test.py["{$3PAR.PROFILE}"]

Recommended profile config:
  /etc/zabbix/3par/3par-prod-01.conf

  [3par]
  host=10.10.10.50
  port=22
  user=zbx_monitor
  password=YourPasswordHere
  command_set=basic
  timeout=25

Manual test:
  sudo -u zabbix /usr/lib/zabbix/externalscripts/3par/3par_perf_test.py \
    --host 10.10.10.50 --user zbx_monitor --password 'YourPasswordHere' --pretty

Safer manual test with env var:
  export THREEPAR_PASSWORD='YourPasswordHere'
  sudo -u zabbix -E /usr/lib/zabbix/externalscripts/3par/3par_perf_test.py \
    --host 10.10.10.50 --user zbx_monitor --pretty

Dependencies:
  python3 -m pip install paramiko
"""

import argparse
import configparser
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any



@dataclass
class CommandSpec:
    name: str
    command: str
    required: bool = False


COMMAND_SETS: Dict[str, List[CommandSpec]] = {
    # Minimal first test. This is the best starting point.
    "minimal": [
        CommandSpec("showversion", "showversion", required=True),
        CommandSpec("statcpu", "statcpu -iter 1", required=False),
        CommandSpec("statport", "statport -iter 1", required=False),
        CommandSpec("showcpg", "showcpg", required=False),
    ],

    # Good default for Zabbix performance monitoring.
    "basic": [
        CommandSpec("showversion", "showversion", required=True),
        CommandSpec("statcpu", "statcpu -iter 1", required=False),
        CommandSpec("statport", "statport -iter 1", required=False),
        CommandSpec("statpd", "statpd -iter 1", required=False),
        CommandSpec("statvv", "statvv -iter 1", required=False),
        CommandSpec("showcpg", "showcpg", required=False),
        CommandSpec("showport", "showport", required=False),
    ],

    # Heavier set. Use after the basic one works reliably.
    "full": [
        CommandSpec("showversion", "showversion", required=True),
        CommandSpec("statcpu", "statcpu -iter 1", required=False),
        CommandSpec("statport", "statport -iter 1", required=False),
        CommandSpec("statpd", "statpd -iter 1", required=False),
        CommandSpec("statvv", "statvv -iter 1", required=False),
        CommandSpec("statvlun", "statvlun -iter 1", required=False),
        CommandSpec("statcmp", "statcmp -iter 1", required=False),
        CommandSpec("showcpg", "showcpg", required=False),
        CommandSpec("showvv", "showvv", required=False),
        CommandSpec("showpd", "showpd", required=False),
        CommandSpec("showport", "showport", required=False),
    ],
}


def now_ts() -> int:
    return int(time.time())


def json_print(data: Dict[str, Any], pretty: bool = False) -> None:
    if pretty:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def read_password_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def load_profile(profile: str, base_dir: str = "/etc/zabbix/3par") -> Dict[str, Any]:
    path = os.path.join(base_dir, f"{profile}.conf")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Profile config not found: {path}")

    cfg = configparser.ConfigParser()
    read_files = cfg.read(path)
    if not read_files or not cfg.has_section("3par"):
        raise RuntimeError(f"Invalid config file. Expected [3par] section in: {path}")

    result = {
        "profile": profile,
        "host": cfg.get("3par", "host"),
        "port": cfg.getint("3par", "port", fallback=22),
        "user": cfg.get("3par", "user"),
        "password": cfg.get("3par", "password", fallback=""),
        "command_set": cfg.get("3par", "command_set", fallback="basic"),
        "timeout": cfg.getint("3par", "timeout", fallback=25),
    }

    password_file = cfg.get("3par", "password_file", fallback="").strip()
    if password_file:
        result["password"] = read_password_file(password_file)

    return result


def sanitize_error(text: str, password: Optional[str] = None) -> str:
    if not text:
        return ""
    cleaned = text
    if password:
        cleaned = cleaned.replace(password, "********")
    return cleaned.strip()


def normalize_key(value: str) -> str:
    value = value.strip().lower()
    value = value.replace("/", "_").replace("%", "pct")
    value = re.sub(r"[^a-z0-9_]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "col"


def to_number(value: str) -> Any:
    v = value.strip().replace(",", "")
    if v == "" or v == "-":
        return value.strip()
    try:
        if re.fullmatch(r"[-+]?\d+", v):
            return int(v)
        if re.fullmatch(r"[-+]?(\d+\.\d*|\d*\.\d+)", v):
            return float(v)
    except Exception:
        pass
    return value.strip()


def split_columns(line: str) -> List[str]:
    """Best-effort split for 3PAR CLI tables.

    3PAR CLI output is not a stable JSON API. This parser is intentionally conservative:
    - prefers columns separated by 2+ spaces;
    - falls back to generic whitespace split;
    - does not try to make final operational decisions from parsed output.
    """
    line = line.strip()
    if not line:
        return []
    parts = re.split(r"\s{2,}", line)
    if len(parts) <= 1:
        parts = line.split()
    return [p.strip() for p in parts if p.strip()]


def is_separator(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and all(ch in "-=_+ " for ch in stripped)


def looks_like_header(line: str) -> bool:
    if not line.strip() or is_separator(line):
        return False
    parts = split_columns(line)
    if len(parts) < 2:
        return False
    alpha_count = sum(1 for p in parts if re.search(r"[A-Za-zА-Яа-я]", p))
    return alpha_count >= max(1, len(parts) // 2)


def looks_like_data(line: str) -> bool:
    if not line.strip() or is_separator(line):
        return False
    parts = split_columns(line)
    if len(parts) < 2:
        return False
    return True


def parse_table_best_effort(text: str) -> List[Dict[str, Any]]:
    """Try to convert a CLI table to a list of dicts.

    This is intentionally best-effort. It is useful for the first Zabbix dependent items,
    but production parsing should be adjusted after seeing the real 3PAR output.
    """
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []

    # Remove obvious prompt/banner noise.
    noise_patterns = [
        r"^Press\s+.*continue",
        r"^Use\s+of\s+this\s+system",
        r"^Last\s+login",
    ]
    clean_lines = []
    for ln in lines:
        if any(re.search(p, ln, flags=re.I) for p in noise_patterns):
            continue
        clean_lines.append(ln)

    # Find the last plausible header before rows.
    header_idx = None
    for idx, ln in enumerate(clean_lines):
        if looks_like_header(ln):
            # Prefer a header followed by a separator or data line.
            if idx + 1 < len(clean_lines) and (is_separator(clean_lines[idx + 1]) or looks_like_data(clean_lines[idx + 1])):
                header_idx = idx

    if header_idx is None:
        return []

    headers = [normalize_key(h) for h in split_columns(clean_lines[header_idx])]
    rows: List[Dict[str, Any]] = []

    for ln in clean_lines[header_idx + 1:]:
        if is_separator(ln):
            continue
        if not looks_like_data(ln):
            continue

        cols = split_columns(ln)
        if len(cols) < 2:
            continue

        row: Dict[str, Any] = {}
        for i, header in enumerate(headers):
            if i < len(cols):
                row[header] = to_number(cols[i])
            else:
                row[header] = None

        # Preserve overflow columns to avoid silent loss.
        if len(cols) > len(headers):
            row["_extra"] = cols[len(headers):]

        rows.append(row)

    return rows


def connect_ssh(host: str, port: int, user: str, password: str, timeout: int):
    try:
        import paramiko
    except Exception as exc:
        raise RuntimeError("Python module 'paramiko' is not installed. Install it with: python3 -m pip install paramiko") from exc

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=port,
        username=user,
        password=password,
        look_for_keys=False,
        allow_agent=False,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
    )
    return client


def run_command(client, command: str, timeout: int) -> Tuple[int, str, str, float]:
    started = time.monotonic()
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    elapsed = round(time.monotonic() - started, 3)
    return rc, out, err, elapsed


def collect(host: str, port: int, user: str, password: str, timeout: int, command_set: str) -> Dict[str, Any]:
    if command_set not in COMMAND_SETS:
        raise RuntimeError(f"Unknown command_set='{command_set}'. Available: {', '.join(COMMAND_SETS.keys())}")

    result: Dict[str, Any] = {
        "status": 0,
        "collector": "3par_perf_test.py",
        "timestamp": now_ts(),
        "host": host,
        "port": port,
        "user": user,
        "command_set": command_set,
        "commands_total": len(COMMAND_SETS[command_set]),
        "commands_ok": 0,
        "commands_failed": 0,
        "command_status": {},
        "raw": {},
        "parsed": {},
        "error": "",
    }

    client = None
    try:
        client = connect_ssh(host, port, user, password, timeout)
    except Exception as exc:
        result["status"] = 0
        result["error"] = sanitize_error(f"SSH connection failed: {exc}", password)
        return result

    try:
        for spec in COMMAND_SETS[command_set]:
            try:
                rc, out, err, elapsed = run_command(client, spec.command, timeout)
                ok = rc == 0
                result["command_status"][spec.name] = {
                    "status": 1 if ok else 0,
                    "required": spec.required,
                    "command": spec.command,
                    "rc": rc,
                    "elapsed_sec": elapsed,
                    "error": sanitize_error(err, password),
                }
                result["raw"][spec.name] = out
                result["parsed"][spec.name] = parse_table_best_effort(out)

                if ok:
                    result["commands_ok"] += 1
                else:
                    result["commands_failed"] += 1
                    if spec.required:
                        result["error"] += f"Required command failed: {spec.name}; "
            except Exception as exc:
                result["commands_failed"] += 1
                result["command_status"][spec.name] = {
                    "status": 0,
                    "required": spec.required,
                    "command": spec.command,
                    "rc": None,
                    "elapsed_sec": None,
                    "error": sanitize_error(str(exc), password),
                }
                result["raw"][spec.name] = ""
                result["parsed"][spec.name] = []
                if spec.required:
                    result["error"] += f"Required command exception: {spec.name}: {exc}; "
    finally:
        try:
            client.close()
        except Exception:
            pass

    # Overall status: SSH worked and at least one command worked.
    # Required command failure marks the collector failed.
    required_failed = any(
        item.get("required") and item.get("status") == 0
        for item in result["command_status"].values()
    )
    if result["commands_ok"] > 0 and not required_failed:
        result["status"] = 1
    else:
        result["status"] = 0

    if not result["error"] and result["commands_failed"] > 0:
        result["error"] = f"Some commands failed: {result['commands_failed']} of {result['commands_total']}"

    result["error"] = sanitize_error(result["error"].strip(), password)
    return result


def write_example_config(path: str) -> None:
    content = """[3par]
host=10.10.10.50
port=22
user=zbx_monitor
password=YourPasswordHere
command_set=basic
timeout=25

# Instead of password= you can use:
# password_file=/etc/zabbix/3par/3par-prod-01.password
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Single-file SSH/password test collector for HPE 3PAR / StoreServ Zabbix monitoring.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "profile",
        nargs="?",
        help="Profile name. Reads /etc/zabbix/3par/<profile>.conf if --host is not specified.",
    )
    parser.add_argument("--host", help="3PAR management IP/FQDN.")
    parser.add_argument("--port", type=int, default=22, help="SSH port. Default: 22.")
    parser.add_argument("--user", help="3PAR username.")
    parser.add_argument("--password", help="3PAR password. For tests only; profile/password-file/env is safer.")
    parser.add_argument("--password-file", help="File containing the 3PAR password.")
    parser.add_argument("--profile-dir", default="/etc/zabbix/3par", help="Profile config directory. Default: /etc/zabbix/3par")
    parser.add_argument(
        "--command-set",
        choices=sorted(COMMAND_SETS.keys()),
        default=None,
        help="Command set to run: minimal, basic, full. Default: profile value or basic.",
    )
    parser.add_argument("--timeout", type=int, default=None, help="SSH command timeout in seconds. Default: profile value or 25.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON for manual tests.")
    parser.add_argument("--list-command-sets", action="store_true", help="Print available command sets and exit.")
    parser.add_argument("--write-example-config", metavar="PATH", help="Write an example profile config and exit.")
    parser.add_argument("--debug", action="store_true", help="Include traceback on top-level failure.")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    if args.list_command_sets:
        data = {
            name: [{"name": c.name, "command": c.command, "required": c.required} for c in commands]
            for name, commands in COMMAND_SETS.items()
        }
        json_print({"status": 1, "command_sets": data, "timestamp": now_ts()}, pretty=True)
        return

    if args.write_example_config:
        write_example_config(args.write_example_config)
        json_print({"status": 1, "message": f"Example config written to {args.write_example_config}", "timestamp": now_ts()}, pretty=args.pretty)
        return

    try:
        cfg: Dict[str, Any] = {}

        if args.host:
            cfg = {
                "profile": args.profile or "direct",
                "host": args.host,
                "port": args.port,
                "user": args.user,
                "password": args.password or "",
                "command_set": args.command_set or "basic",
                "timeout": args.timeout or 25,
            }
            if args.password_file:
                cfg["password"] = read_password_file(args.password_file)
            if not cfg["password"]:
                cfg["password"] = os.environ.get("THREEPAR_PASSWORD", "")
        else:
            if not args.profile:
                raise RuntimeError("Specify either --host/--user/--password or a profile name, e.g. 3par_perf_test.py 3par-prod-01")
            cfg = load_profile(args.profile, args.profile_dir)
            if args.command_set:
                cfg["command_set"] = args.command_set
            if args.timeout:
                cfg["timeout"] = args.timeout

        missing = [k for k in ("host", "user", "password") if not cfg.get(k)]
        if missing:
            raise RuntimeError(f"Missing required parameter(s): {', '.join(missing)}")

        data = collect(
            host=str(cfg["host"]),
            port=int(cfg.get("port", 22)),
            user=str(cfg["user"]),
            password=str(cfg["password"]),
            timeout=int(cfg.get("timeout", 25)),
            command_set=str(cfg.get("command_set", "basic")),
        )
        data["profile"] = cfg.get("profile", args.profile or "direct")
        json_print(data, pretty=args.pretty)

    except Exception as exc:
        error_data: Dict[str, Any] = {
            "status": 0,
            "collector": "3par_perf_test.py",
            "timestamp": now_ts(),
            "error": str(exc),
        }
        if args.debug:
            error_data["traceback"] = traceback.format_exc()
        json_print(error_data, pretty=args.pretty)


if __name__ == "__main__":
    main()
