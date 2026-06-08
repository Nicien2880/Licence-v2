#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HPE 3PAR / StoreServ article-like SSH collector for Zabbix.

Python analogue of the common PowerShell/HPEStoragePowerShellToolkit approach:
- CPU summary by physical 3PAR node/controller
- VV/volume read/write/total summary
- VLUN read/write/total summary
- Port read/write/total summary
- Physical disk read/write/total summary

Designed for Zabbix:
  External check -> one master JSON item -> dependent items.

NO LLD. This avoids thousands of items.

Config file:
  /etc/zabbix/3par/<profile>.conf

Example:
  [3par]
  host=10.10.10.50
  port=22
  user=zbx_monitor
  password=YourPassword
  timeout=35

  cpu_command=statcpu -d 1 -iter 1
  vv_command=statvv -rw -d 1 -iter 1
  vlun_command=statvlun -rw -d 1 -iter 1
  port_command=statport -rw -d 1 -iter 1
  pd_command=statpd -rw -d 1 -iter 1

  # Optional:
  raw_output=false

Run:
  sudo -u zabbix /usr/lib/zabbix/externalscripts/3par/3par_article_like.py 3par-prod --pretty
  sudo -u zabbix /usr/lib/zabbix/externalscripts/3par/3par_article_like.py 3par-prod --pretty --raw

Output notes:
- cpu.node_N_busy_pct is averaged by physical node/controller.
- For KB/MB/IO/RW, values are summed across all rows.
- Svt/service time is weighted by IO for cur/avg and max() for max.
- All numeric output fields are always present and numeric, so Zabbix dependent items do not become unsupported because of JSON null.
"""

import argparse
import configparser
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import paramiko
except ImportError:
    print(json.dumps({"status": 0, "error": "Install paramiko: python3 -m pip install paramiko"}, ensure_ascii=False))
    sys.exit(1)

CONFIG_DIR = "/etc/zabbix/3par"


def norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s).strip().lower())


def fnum(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip().replace(",", ".")
    if not s or s in ("-", "--", "N/A", "n/a", "None", "null"):
        return None
    s = re.sub(r"(?<=\d)\s*(KB/s|MB/s|GB/s|IO/s|IOPS|ms|%)$", "", s, flags=re.I).strip()
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def pick(row: Dict[str, Any], names: List[str]) -> Optional[Any]:
    rr = {norm(k): v for k, v in row.items()}
    for name in names:
        key = norm(name)
        if key in rr and rr[key] not in ("", None):
            return rr[key]
    return None


def split_line(line: str) -> List[str]:
    return re.split(r"\s+", line.strip())


def looks_like_header(cols: List[str]) -> bool:
    if len(cols) < 2:
        return False
    joined = " ".join(cols).lower()
    markers = [
        "node", "cpu", "busy", "idle",
        "vv", "vvname", "vlun", "lun", "host",
        "port", "n:s:p", "nsp",
        "pd", "disk",
        "r/w", "rw", "read", "write",
        "kb", "kb/s", "i/o", "io", "iops", "svt",
        "cur", "avg", "max", "q", "qlen", "busy"
    ]
    return sum(1 for m in markers if m in joined) >= 2


def parse_table(text: str) -> List[Dict[str, str]]:
    """Best-effort parser for whitespace-separated 3PAR CLI tables."""
    rows: List[Dict[str, str]] = []
    header: Optional[List[str]] = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if set(line) <= {"-", "=", "#", " "}:
            continue

        cols = split_line(line)

        if looks_like_header(cols):
            header = [norm(c) for c in cols]
            continue

        if not header:
            continue

        vals = cols[:len(header)]
        if len(vals) < len(header):
            vals += [""] * (len(header) - len(vals))

        row = {header[i]: vals[i] for i in range(len(header))}
        if any(fnum(v) is not None for v in row.values()):
            rows.append(row)

    return rows


def zero_metrics() -> Dict[str, float]:
    return {
        "kb_cur": 0.0, "kb_avg": 0.0, "kb_max": 0.0,
        "mb_cur": 0.0, "mb_avg": 0.0, "mb_max": 0.0,
        "io_cur": 0.0, "io_avg": 0.0, "io_max": 0.0,
        "rw_cur": 0.0, "rw_avg": 0.0, "rw_max": 0.0,
        "svt_cur": 0.0, "svt_avg": 0.0, "svt_max": 0.0,
        "busy_cur": 0.0, "busy_avg": 0.0, "busy_max": 0.0,
        "qlen_cur": 0.0, "qlen_avg": 0.0, "qlen_max": 0.0,
    }


def classify_rw(v: Any) -> str:
    s = str(v or "").strip().lower()
    if s in ("r", "rd", "read", "reads") or "read" in s:
        return "read"
    if s in ("w", "wr", "write", "writes") or "write" in s:
        return "write"
    if s in ("t", "tot", "total", "r/w", "rw", "r+w", "sum") or "total" in s or "r/w" in s:
        return "total"
    return "total"


def add_metric(dst: Dict[str, float], key: str, value: Optional[float]) -> None:
    if value is not None:
        dst[key] = float(dst.get(key, 0.0)) + float(value)


def parse_rw_summary(text: str, label: str) -> Dict[str, Any]:
    """Parse and aggregate read/write/total performance tables."""
    rows = parse_table(text)
    out = {"read": zero_metrics(), "write": zero_metrics(), "total": zero_metrics()}
    svt_weight = {
        "read": {"cur_num": 0.0, "cur_den": 0.0, "avg_num": 0.0, "avg_den": 0.0, "max": 0.0},
        "write": {"cur_num": 0.0, "cur_den": 0.0, "avg_num": 0.0, "avg_den": 0.0, "max": 0.0},
        "total": {"cur_num": 0.0, "cur_den": 0.0, "avg_num": 0.0, "avg_den": 0.0, "max": 0.0},
    }
    used = 0

    for row in rows:
        direction = classify_rw(pick(row, ["r/w", "rw", "direction", "type", "mode", "readwrite"]))

        values = {
            "kb_cur": fnum(pick(row, ["kb_cur", "kbcur", "kb/s_cur", "kbps_cur", "kbpscur", "kb/s"])),
            "kb_avg": fnum(pick(row, ["kb_avg", "kbavg", "kb/s_avg", "kbps_avg", "kbpsavg"])),
            "kb_max": fnum(pick(row, ["kb_max", "kbmax", "kb/s_max", "kbps_max", "kbpsmax"])),

            "io_cur": fnum(pick(row, ["i/o_cur", "io_cur", "iocur", "io/s_cur", "iops_cur", "iopscur", "i/o"])),
            "io_avg": fnum(pick(row, ["i/o_avg", "io_avg", "ioavg", "io/s_avg", "iops_avg", "iopsavg"])),
            "io_max": fnum(pick(row, ["i/o_max", "io_max", "iomax", "io/s_max", "iops_max", "iopsmax"])),

            "rw_cur": fnum(pick(row, ["r/w_cur", "rw_cur", "rwcur"])),
            "rw_avg": fnum(pick(row, ["r/w_avg", "rw_avg", "rwavg"])),
            "rw_max": fnum(pick(row, ["r/w_max", "rw_max", "rwmax"])),

            "busy_cur": fnum(pick(row, ["busy_cur", "busycur", "busy", "busy%", "busypct"])),
            "busy_avg": fnum(pick(row, ["busy_avg", "busyavg"])),
            "busy_max": fnum(pick(row, ["busy_max", "busymax"])),

            "qlen_cur": fnum(pick(row, ["q_cur", "qcur", "qlen_cur", "qlencur", "queue_cur", "queuecur", "q", "qlen"])),
            "qlen_avg": fnum(pick(row, ["q_avg", "qavg", "qlen_avg", "qlenavg", "queue_avg", "queueavg"])),
            "qlen_max": fnum(pick(row, ["q_max", "qmax", "qlen_max", "qlenmax", "queue_max", "queuemax"])),
        }

        svt_cur = fnum(pick(row, ["svt_cur", "svtcur", "service_time_cur", "servicetimecur", "svt"]))
        svt_avg = fnum(pick(row, ["svt_avg", "svtavg", "service_time_avg", "servicetimeavg"]))
        svt_max = fnum(pick(row, ["svt_max", "svtmax", "service_time_max", "servicetimemax"]))

        if not any(v is not None for v in values.values()) and svt_cur is None and svt_avg is None and svt_max is None:
            continue

        used += 1
        for k, v in values.items():
            add_metric(out[direction], k, v)

        if svt_cur is not None:
            den = values.get("io_cur") if values.get("io_cur") not in (None, 0) else 1.0
            svt_weight[direction]["cur_num"] += svt_cur * den
            svt_weight[direction]["cur_den"] += den
        if svt_avg is not None:
            den = values.get("io_avg") if values.get("io_avg") not in (None, 0) else 1.0
            svt_weight[direction]["avg_num"] += svt_avg * den
            svt_weight[direction]["avg_den"] += den
        if svt_max is not None:
            svt_weight[direction]["max"] = max(svt_weight[direction]["max"], svt_max)

    # Calculate total from read+write if total rows were not present or are zero.
    for metric in zero_metrics().keys():
        if out["total"].get(metric, 0.0) == 0.0:
            out["total"][metric] = float(out["read"].get(metric, 0.0)) + float(out["write"].get(metric, 0.0))

    # Finalize MB and service time.
    for direction in ("read", "write", "total"):
        d = out[direction]
        d["mb_cur"] = float(d.get("kb_cur", 0.0)) / 1024.0
        d["mb_avg"] = float(d.get("kb_avg", 0.0)) / 1024.0
        d["mb_max"] = float(d.get("kb_max", 0.0)) / 1024.0

        w = svt_weight[direction]
        if w["cur_den"]:
            d["svt_cur"] = w["cur_num"] / w["cur_den"]
        if w["avg_den"]:
            d["svt_avg"] = w["avg_num"] / w["avg_den"]
        d["svt_max"] = w["max"]

        for k, v in list(d.items()):
            d[k] = round(float(v), 6)

    flat: Dict[str, float] = {}
    for direction in ("read", "write", "total"):
        for metric, value in out[direction].items():
            flat[f"{direction}_{metric}"] = value

    return {
        "read": out["read"],
        "write": out["write"],
        "total": out["total"],
        "flat": flat,
        "rows_detected": len(rows),
        "rows_used": used,
        "label": label
    }


def parse_cpu(text: str) -> Dict[str, Any]:
    rows = parse_table(text)
    groups: Dict[str, List[float]] = {}

    for i, row in enumerate(rows):
        node = pick(row, ["node", "n", "nodeid", "node_id"])
        if node is None:
            nums = [v for v in row.values() if fnum(v) is not None]
            node = nums[0] if nums else str(i)

        busy = pick(row, ["busy", "busy%", "busypct", "cpubusy", "cpubusy%", "total", "total%", "cur", "cur%", "user", "usr"])
        busy_f = fnum(busy)

        if busy_f is None:
            idle_f = fnum(pick(row, ["idle", "idle%", "idlepct"]))
            if idle_f is not None:
                busy_f = max(0.0, min(100.0, 100.0 - idle_f))

        if busy_f is not None:
            groups.setdefault(str(node), []).append(busy_f)

    by_node: Dict[str, Dict[str, Any]] = {}
    for node, values in groups.items():
        by_node[node] = {
            "busy_pct": round(sum(values) / len(values), 3),
            "samples": len(values),
            "present": 1
        }

    vals = [v["busy_pct"] for v in by_node.values()]
    result: Dict[str, Any] = {
        "by_node": by_node,
        "total_busy_pct": round(sum(vals) / len(vals), 3) if vals else 0.0,
        "rows_detected": len(rows),
        "nodes_detected": len(by_node),
    }

    for i in range(4):
        node = by_node.get(str(i))
        result[f"node_{i}_busy_pct"] = node["busy_pct"] if node else 0.0
        result[f"node_{i}_present"] = node["present"] if node else 0

    return result


def load_cfg(profile: str, config_dir: str) -> Dict[str, Any]:
    path = os.path.join(config_dir, profile + ".conf")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config profile not found: {path}")

    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    if "3par" not in cp:
        raise ValueError(f"Missing [3par] section in {path}")

    s = cp["3par"]
    password = s.get("password", fallback=None)
    password_file = s.get("password_file", fallback=None)
    if not password and password_file:
        with open(password_file, "r", encoding="utf-8") as f:
            password = f.read().strip()
    if not password:
        password = os.environ.get("THREEPAR_PASSWORD")
    if not password:
        raise RuntimeError("No password. Set password=, password_file=, or THREEPAR_PASSWORD.")

    return {
        "host": s.get("host"),
        "port": s.getint("port", fallback=22),
        "user": s.get("user"),
        "password": password,
        "timeout": s.getint("timeout", fallback=35),
        "raw_output": s.getboolean("raw_output", fallback=False),

        "cpu_command": s.get("cpu_command", fallback="statcpu -d 1 -iter 1"),
        "vv_command": s.get("vv_command", fallback="statvv -rw -d 1 -iter 1"),
        "vlun_command": s.get("vlun_command", fallback="statvlun -rw -d 1 -iter 1"),
        "port_command": s.get("port_command", fallback="statport -rw -d 1 -iter 1"),
        "pd_command": s.get("pd_command", fallback="statpd -rw -d 1 -iter 1"),

        "enable_vlun": s.getboolean("enable_vlun", fallback=True),
        "enable_port": s.getboolean("enable_port", fallback=True),
        "enable_pd": s.getboolean("enable_pd", fallback=True),
    }


def ssh_cmd(cfg: Dict[str, Any], command: str) -> Tuple[int, str, str, float]:
    started = time.monotonic()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    client.connect(
        hostname=cfg["host"],
        port=int(cfg["port"]),
        username=cfg["user"],
        password=cfg["password"],
        look_for_keys=False,
        allow_agent=False,
        timeout=int(cfg["timeout"]),
        banner_timeout=int(cfg["timeout"]),
        auth_timeout=int(cfg["timeout"]),
    )

    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=int(cfg["timeout"]), get_pty=False)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
    finally:
        client.close()

    return rc, out, err, round(time.monotonic() - started, 3)


def disabled_summary(label: str) -> Dict[str, Any]:
    res = parse_rw_summary("", label)
    res["disabled"] = 1
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="HPE 3PAR article-like SSH collector for Zabbix")
    ap.add_argument("profile", nargs="?")
    ap.add_argument("--config-dir", default=CONFIG_DIR)
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args()

    started = time.time()
    result: Dict[str, Any] = {
        "status": 0,
        "collector": "3par_article_like.py",
        "profile": args.profile or "",
        "timestamp": int(started),
        "error": "",
        "command_status": {},
        "commands_total": 0,
        "commands_ok": 0,
        "commands_failed": 0,
        "cpu": {},
        "vv": {},
        "vlun": {},
        "port": {},
        "pd": {},
    }

    if not args.profile:
        result["error"] = "Usage: 3par_article_like.py <profile> [--pretty] [--raw]"
        print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
        return 1

    raw: Dict[str, str] = {}

    try:
        cfg = load_cfg(args.profile, args.config_dir)

        commands: Dict[str, Tuple[str, str, bool]] = {
            "statcpu": ("cpu", cfg["cpu_command"], True),
            "statvv": ("vv", cfg["vv_command"], True),
        }

        if cfg["enable_vlun"]:
            commands["statvlun"] = ("vlun", cfg["vlun_command"], False)
        if cfg["enable_port"]:
            commands["statport"] = ("port", cfg["port_command"], False)
        if cfg["enable_pd"]:
            commands["statpd"] = ("pd", cfg["pd_command"], False)

        for name, (section, command, required) in commands.items():
            try:
                rc, out, err, elapsed = ssh_cmd(cfg, command)
                raw[name] = out
                result["command_status"][name] = {
                    "status": 1 if rc == 0 else 0,
                    "required": required,
                    "rc": rc,
                    "elapsed_sec": elapsed,
                    "command": command,
                    "stderr": err[:500],
                }
            except Exception as e:
                raw[name] = ""
                result["command_status"][name] = {
                    "status": 0,
                    "required": required,
                    "rc": None,
                    "elapsed_sec": 0.0,
                    "command": command,
                    "stderr": str(e)[:500],
                }

        result["cpu"] = parse_cpu(raw.get("statcpu", "")) if result["command_status"].get("statcpu", {}).get("status") == 1 else parse_cpu("")
        result["vv"] = parse_rw_summary(raw.get("statvv", ""), "vv") if result["command_status"].get("statvv", {}).get("status") == 1 else parse_rw_summary("", "vv")
        result["vlun"] = parse_rw_summary(raw.get("statvlun", ""), "vlun") if cfg["enable_vlun"] and result["command_status"].get("statvlun", {}).get("status") == 1 else disabled_summary("vlun")
        result["port"] = parse_rw_summary(raw.get("statport", ""), "port") if cfg["enable_port"] and result["command_status"].get("statport", {}).get("status") == 1 else disabled_summary("port")
        result["pd"] = parse_rw_summary(raw.get("statpd", ""), "pd") if cfg["enable_pd"] and result["command_status"].get("statpd", {}).get("status") == 1 else disabled_summary("pd")

        failed_required = [k for k, v in result["command_status"].items() if v.get("status") != 1 and v.get("required")]
        failed_all = [k for k, v in result["command_status"].items() if v.get("status") != 1]

        result["commands_total"] = len(result["command_status"])
        result["commands_failed"] = len(failed_all)
        result["commands_ok"] = result["commands_total"] - result["commands_failed"]

        if failed_required:
            result["status"] = 0
            result["error"] = "Required commands failed: " + ", ".join(failed_required)
        else:
            result["status"] = 1
            result["error"] = ("Optional commands failed: " + ", ".join(failed_all)) if failed_all else ""

        result["elapsed_sec"] = round(time.time() - started, 3)

        if args.raw or cfg["raw_output"]:
            result["raw"] = raw

    except Exception as e:
        result["status"] = 0
        result["error"] = str(e)
        result["elapsed_sec"] = round(time.time() - started, 3)

    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=False))
    return 0 if result.get("status") == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
