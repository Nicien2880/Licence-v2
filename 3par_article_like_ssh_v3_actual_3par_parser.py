#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HPE 3PAR / StoreServ SSH aggregate collector for Zabbix.

Compatible with:
  Template HPE 3PAR Article-like SSH Summary TOTAL ONLY

This version parses the actual grouped 3PAR stat* format:

  I/O per second: Cur Avg Max
  KBytes per sec: Cur Avg Max
  Svt ms: Cur Avg
  IOSz KB: Cur Avg
  Qlen
  Idle %: Cur Avg   # mostly statpd

Default commands intentionally do NOT use -rw:
  statvv -d 1 -iter 1
  statvlun -d 1 -iter 1
  statport -d 1 -iter 1
  statpd -d 1 -iter 1
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


def is_num_token(s: Any) -> bool:
    return re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", str(s).strip()) is not None


def fnum(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip().replace(",", ".")
    if not s or s in ("-", "--", "N/A", "n/a", "None", "null"):
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def split_line(line: str) -> List[str]:
    return re.split(r"\s+", line.strip())


def classify_rw(v: Any) -> str:
    s = str(v or "").strip().lower()
    if s in ("r", "rd", "read", "reads") or "read" in s:
        return "read"
    if s in ("w", "wr", "write", "writes") or "write" in s:
        return "write"
    return "total"


def zero_metrics() -> Dict[str, float]:
    return {
        "io_cur": 0.0, "io_avg": 0.0, "io_max": 0.0,
        "kb_cur": 0.0, "kb_avg": 0.0, "kb_max": 0.0,
        "mb_cur": 0.0, "mb_avg": 0.0, "mb_max": 0.0,
        "rw_cur": 0.0, "rw_avg": 0.0, "rw_max": 0.0,
        "svt_cur": 0.0, "svt_avg": 0.0, "svt_max": 0.0,
        "qlen_cur": 0.0, "qlen_avg": 0.0, "qlen_max": 0.0,
        "busy_cur": 0.0, "busy_avg": 0.0, "busy_max": 0.0,
    }


def numeric_tail(tokens: List[str]) -> Tuple[List[str], List[str]]:
    tail: List[str] = []
    i = len(tokens) - 1
    while i >= 0 and is_num_token(tokens[i]):
        tail.insert(0, tokens[i])
        i -= 1
    return tokens[:i + 1], tail


def skip_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if set(s) <= {"-", "=", "#", " "}:
        return True
    low = s.lower()
    if any(x in low for x in [
        "date", "time", "cur", "avg", "max", "kbytes", "second",
        "svt", "iosz", "idle", "qlen", "port", "vvname", "host", "node"
    ]):
        return True
    return False


def parse_stat_summary(text: str, label: str) -> Dict[str, Any]:
    records: List[Dict[str, Any]] = []
    rows_detected = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if skip_line(line):
            continue

        tokens = split_line(line)
        prefix, tail = numeric_tail(tokens)

        # Minimum actual layout:
        # IO Cur/Avg/Max + KB Cur/Avg/Max + Svt Cur/Avg + IOSz Cur/Avg + Qlen = 11 numeric values
        if len(tail) < 11:
            continue

        nums = [fnum(x) or 0.0 for x in tail]
        rows_detected += 1

        direction = "total"
        for token in reversed(prefix):
            tl = str(token).strip().lower()
            if tl in ("t", "total", "r", "w", "read", "write"):
                direction = classify_rw(tl)
                break

        prefix_low = [str(x).strip().lower() for x in prefix]
        has_colon_object = any(":" in x for x in prefix_low)

        # Summary examples:
        # statport: "32 Data t ..."
        # statpd:   "264 t ..."
        is_summary = False
        if len(prefix_low) <= 3 and not has_colon_object and any(x in ("t", "total", "r", "w", "read", "write") for x in prefix_low):
            is_summary = True
        if prefix_low and prefix_low[0] == "total":
            is_summary = True

        rec = {
            "direction": direction,
            "is_summary": is_summary,
            "io_cur": nums[0],
            "io_avg": nums[1],
            "io_max": nums[2],
            "kb_cur": nums[3],
            "kb_avg": nums[4],
            "kb_max": nums[5],
            "svt_cur": nums[6],
            "svt_avg": nums[7],
            # No Svt Max column in the observed output; use max(Cur, Avg).
            "svt_max": max(nums[6], nums[7]),
            "qlen_cur": nums[10],
            "qlen_avg": nums[10],
            "qlen_max": nums[10],
            "busy_cur": 0.0,
            "busy_avg": 0.0,
            "busy_max": 0.0,
        }

        # statpd usually has Idle % Cur Avg after Qlen.
        # Convert Idle to Busy.
        if len(nums) >= 13:
            idle_cur = nums[11]
            idle_avg = nums[12]
            rec["busy_cur"] = max(0.0, min(100.0, 100.0 - idle_cur))
            rec["busy_avg"] = max(0.0, min(100.0, 100.0 - idle_avg))
            rec["busy_max"] = max(rec["busy_cur"], rec["busy_avg"])

        records.append(rec)

    summary_records = [r for r in records if r["is_summary"]]
    selected = summary_records if summary_records else records

    out = {"read": zero_metrics(), "write": zero_metrics(), "total": zero_metrics()}
    svt_weight = {k: {"cur_num": 0.0, "cur_den": 0.0, "avg_num": 0.0, "avg_den": 0.0, "max": 0.0} for k in out}
    busy_weight = {k: {"cur_num": 0.0, "cur_den": 0.0, "avg_num": 0.0, "avg_den": 0.0, "max": 0.0} for k in out}

    rows_used = 0
    for r in selected:
        d = r["direction"]
        rows_used += 1

        for k in ("io_cur", "io_avg", "io_max", "kb_cur", "kb_avg", "kb_max", "qlen_cur", "qlen_avg", "qlen_max"):
            out[d][k] += float(r[k])

        out[d]["rw_cur"] += float(r["io_cur"])
        out[d]["rw_avg"] += float(r["io_avg"])
        out[d]["rw_max"] += float(r["io_max"])

        den_cur = r["io_cur"] if r["io_cur"] else 1.0
        den_avg = r["io_avg"] if r["io_avg"] else 1.0

        svt_weight[d]["cur_num"] += r["svt_cur"] * den_cur
        svt_weight[d]["cur_den"] += den_cur
        svt_weight[d]["avg_num"] += r["svt_avg"] * den_avg
        svt_weight[d]["avg_den"] += den_avg
        svt_weight[d]["max"] = max(svt_weight[d]["max"], r["svt_max"])

        busy_weight[d]["cur_num"] += r["busy_cur"] * den_cur
        busy_weight[d]["cur_den"] += den_cur
        busy_weight[d]["avg_num"] += r["busy_avg"] * den_avg
        busy_weight[d]["avg_den"] += den_avg
        busy_weight[d]["max"] = max(busy_weight[d]["max"], r["busy_max"])

    for metric in zero_metrics().keys():
        if out["total"].get(metric, 0.0) == 0.0:
            out["total"][metric] = out["read"].get(metric, 0.0) + out["write"].get(metric, 0.0)

    for direction in ("read", "write", "total"):
        d = out[direction]
        d["mb_cur"] = d["kb_cur"] / 1024.0
        d["mb_avg"] = d["kb_avg"] / 1024.0
        d["mb_max"] = d["kb_max"] / 1024.0

        sw = svt_weight[direction]
        if sw["cur_den"]:
            d["svt_cur"] = sw["cur_num"] / sw["cur_den"]
        if sw["avg_den"]:
            d["svt_avg"] = sw["avg_num"] / sw["avg_den"]
        d["svt_max"] = sw["max"]

        bw = busy_weight[direction]
        if bw["cur_den"]:
            d["busy_cur"] = bw["cur_num"] / bw["cur_den"]
        if bw["avg_den"]:
            d["busy_avg"] = bw["avg_num"] / bw["avg_den"]
        d["busy_max"] = bw["max"]

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
        "rows_detected": rows_detected,
        "rows_used": rows_used,
        "summary_rows_used": len(summary_records),
        "label": label,
        "parser_mode": "3par_grouped_position_v3",
    }


def parse_cpu(text: str) -> Dict[str, Any]:
    groups: Dict[str, List[float]] = {}
    rows_detected = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if skip_line(line):
            continue

        tokens = split_line(line)
        prefix, tail = numeric_tail(tokens)
        if not tail:
            continue

        nums = [fnum(x) for x in tail if fnum(x) is not None]
        if not nums:
            continue

        rows_detected += 1
        node = None
        for token in prefix + tokens:
            if str(token).isdigit():
                node = str(token)
                break
        if node is None:
            continue

        busy = nums[0]
        groups.setdefault(node, []).append(float(busy))

    by_node: Dict[str, Dict[str, Any]] = {}
    for node, vals in groups.items():
        by_node[node] = {"busy_pct": round(sum(vals) / len(vals), 3), "samples": len(vals), "present": 1}

    vals = [v["busy_pct"] for v in by_node.values()]
    result: Dict[str, Any] = {
        "by_node": by_node,
        "total_busy_pct": round(sum(vals) / len(vals), 3) if vals else 0.0,
        "rows_detected": rows_detected,
        "nodes_detected": len(by_node),
    }

    for i in range(4):
        n = by_node.get(str(i))
        result[f"node_{i}_busy_pct"] = n["busy_pct"] if n else 0.0
        result[f"node_{i}_present"] = n["present"] if n else 0

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
        password = open(password_file, encoding="utf-8").read().strip()
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
        "vv_command": s.get("vv_command", fallback="statvv -d 1 -iter 1"),
        "vlun_command": s.get("vlun_command", fallback="statvlun -d 1 -iter 1"),
        "port_command": s.get("port_command", fallback="statport -d 1 -iter 1"),
        "pd_command": s.get("pd_command", fallback="statpd -d 1 -iter 1"),

        "enable_vlun": s.getboolean("enable_vlun", fallback=True),
        "enable_port": s.getboolean("enable_port", fallback=True),
        "enable_pd": s.getboolean("enable_pd", fallback=True),
    }


def ssh_cmd(cfg: Dict[str, Any], command: str) -> Tuple[int, str, str, float]:
    start = time.monotonic()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
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
        stdin, stdout, stderr = c.exec_command(command, timeout=int(cfg["timeout"]), get_pty=False)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
    finally:
        c.close()

    return rc, out, err, round(time.monotonic() - start, 3)


def disabled(label: str) -> Dict[str, Any]:
    r = parse_stat_summary("", label)
    r["disabled"] = 1
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description="HPE 3PAR SSH collector for Zabbix, grouped parser v3")
    ap.add_argument("profile", nargs="?")
    ap.add_argument("--config-dir", default=CONFIG_DIR)
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args()

    started = time.time()
    result: Dict[str, Any] = {
        "status": 0,
        "collector": "3par_article_like_v3.py",
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
        result["vv"] = parse_stat_summary(raw.get("statvv", ""), "vv") if result["command_status"].get("statvv", {}).get("status") == 1 else disabled("vv")
        result["vlun"] = parse_stat_summary(raw.get("statvlun", ""), "vlun") if cfg["enable_vlun"] and result["command_status"].get("statvlun", {}).get("status") == 1 else disabled("vlun")
        result["port"] = parse_stat_summary(raw.get("statport", ""), "port") if cfg["enable_port"] and result["command_status"].get("statport", {}).get("status") == 1 else disabled("port")
        result["pd"] = parse_stat_summary(raw.get("statpd", ""), "pd") if cfg["enable_pd"] and result["command_status"].get("statpd", {}).get("status") == 1 else disabled("pd")

        failed_all = [k for k, v in result["command_status"].items() if v.get("status") != 1]
        failed_required = [k for k, v in result["command_status"].items() if v.get("status") != 1 and v.get("required")]

        result["commands_total"] = len(result["command_status"])
        result["commands_failed"] = len(failed_all)
        result["commands_ok"] = result["commands_total"] - result["commands_failed"]

        result["status"] = 0 if failed_required else 1
        if failed_required:
            result["error"] = "Required commands failed: " + ", ".join(failed_required)
        elif failed_all:
            result["error"] = "Optional commands failed: " + ", ".join(failed_all)
        else:
            result["error"] = ""

        result["elapsed_sec"] = round(time.time() - started, 3)

        if args.raw or cfg["raw_output"]:
            result["raw"] = raw

    except Exception as e:
        result["status"] = 0
        result["error"] = str(e)
        result["elapsed_sec"] = round(time.time() - started, 3)

    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if result.get("status") == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
