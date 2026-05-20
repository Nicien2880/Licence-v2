from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request, url_for

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_FILE = DATA_DIR / "licenses.json"

app = Flask(__name__)


def ensure_storage() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    if not DATA_FILE.exists():
        DATA_FILE.write_text("[]", encoding="utf-8")


def load_licenses() -> list[dict[str, Any]]:
    ensure_storage()
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = DATA_FILE.with_suffix(".json.bak")
        DATA_FILE.replace(backup)
        DATA_FILE.write_text("[]", encoding="utf-8")
        return []


def save_licenses(licenses: list[dict[str, Any]]) -> None:
    ensure_storage()
    DATA_FILE.write_text(
        json.dumps(licenses, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def days_left(expire_date: str | None) -> int | None:
    end = parse_date(expire_date)
    if end is None:
        return None
    return (end - date.today()).days


def license_status(days: int | None) -> str:
    if days is None:
        return "unknown"
    if days < 0:
        return "expired"
    if days < 7:
        return "critical"
    if days < 30:
        return "high"
    if days < 60:
        return "warning"
    return "ok"


def enrich_license(item: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(item)
    total = to_int(enriched.get("total"))
    used = to_int(enriched.get("used"))
    free = max(total - used, 0)
    utilization = round((used / total) * 100, 2) if total else 0
    days = days_left(enriched.get("expire_date"))

    enriched["total"] = total
    enriched["used"] = used
    enriched["free"] = free
    enriched["utilization"] = utilization
    enriched["days_left"] = days
    enriched["status"] = license_status(days)
    return enriched


def dashboard_summary(licenses: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(to_int(item.get("total")) for item in licenses)
    used = sum(to_int(item.get("used")) for item in licenses)
    free = max(total - used, 0)
    utilization = round((used / total) * 100, 2) if total else 0

    enriched = [enrich_license(item) for item in licenses]
    expiring_30 = sum(1 for item in enriched if item["days_left"] is not None and 0 <= item["days_left"] <= 30)
    expired = sum(1 for item in enriched if item["status"] == "expired")
    critical = sum(1 for item in enriched if item["status"] == "critical")
    warning = sum(1 for item in enriched if item["status"] == "warning")

    return {
        "records": len(licenses),
        "total": total,
        "used": used,
        "free": free,
        "utilization": utilization,
        "expiring_30": expiring_30,
        "expired": expired,
        "critical": critical,
        "warning": warning,
    }


def sort_key(item: dict[str, Any]) -> tuple[int, int]:
    status_weight = {
        "expired": 0,
        "critical": 1,
        "high": 2,
        "warning": 3,
        "ok": 4,
        "unknown": 5,
    }
    days = item.get("days_left")
    return (status_weight.get(item.get("status"), 9), 99999 if days is None else days)


@app.route("/")
def dashboard():
    query = request.args.get("q", "").strip().lower()
    raw_licenses = load_licenses()
    licenses = [enrich_license(item) for item in raw_licenses]

    if query:
        licenses = [
            item
            for item in licenses
            if query in " ".join(
                str(item.get(field, "")).lower()
                for field in ("name", "service", "license_type", "owner", "comment")
            )
        ]

    licenses.sort(key=sort_key)
    summary = dashboard_summary(raw_licenses)
    return render_template("dashboard.html", licenses=licenses, summary=summary, query=query)


@app.route("/licenses/add", methods=["GET", "POST"])
def add_license():
    if request.method == "POST":
        licenses = load_licenses()
        licenses.append(
            {
                "id": str(uuid.uuid4()),
                "name": request.form.get("name", "").strip(),
                "service": request.form.get("service", "").strip(),
                "license_type": request.form.get("license_type", "").strip(),
                "total": to_int(request.form.get("total")),
                "used": to_int(request.form.get("used")),
                "expire_date": request.form.get("expire_date", "").strip(),
                "owner": request.form.get("owner", "").strip(),
                "comment": request.form.get("comment", "").strip(),
            }
        )
        save_licenses(licenses)
        return redirect(url_for("dashboard"))

    return render_template("license_form.html", mode="add", license=None)


@app.route("/licenses/<license_id>/delete", methods=["POST"])
def delete_license(license_id: str):
    licenses = load_licenses()
    save_licenses([item for item in licenses if item.get("id") != license_id])
    return redirect(url_for("dashboard"))


@app.route("/api/licenses")
def api_licenses():
    licenses = [enrich_license(item) for item in load_licenses()]
    return jsonify({"summary": dashboard_summary(licenses), "licenses": licenses})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "license-dashboard"})


if __name__ == "__main__":
    port = int(os.getenv("APP_PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
