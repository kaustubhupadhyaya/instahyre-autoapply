"""Shared state writer for the Instahyre tools.

state/status.json  one record per account, rewritten atomically.
state/applications.jsonl  one JSON object per decision, appended and fsynced.
state/PAUSED  a global pause marker created by control.py.

All timestamps are IST (UTC+05:30). IST has no daylight saving, so a fixed offset is
exact and avoids a tzdata dependency on Windows.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BOARD = "instahyre"
VERSION = "1.0.0"
IST = timezone(timedelta(hours=5, minutes=30), name="IST")

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
STATUS_PATH = STATE_DIR / "status.json"
APPLICATIONS_PATH = STATE_DIR / "applications.jsonl"
PAUSED_PATH = STATE_DIR / "PAUSED"

STATUS_FIELDS = ("board", "version", "account", "state", "last_run", "last_success",
                 "applied_today", "daily_cap", "message", "updated_at")
APPLICATION_FIELDS = ("board", "job_id", "title", "company", "url", "applied_at", "status")


def now_ist() -> datetime:
    return datetime.now(IST)


def ist_iso(value: datetime | None = None) -> str:
    value = value or now_ist()
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
    return value.astimezone(IST).isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_state() -> dict:
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_applications() -> list[dict]:
    rows = []
    try:
        lines = APPLICATIONS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def write_state(account_key: str, **fields) -> dict:
    """Merge one account's status record and rewrite status.json atomically.

    The positional key is the account id. The ``account`` field is the display label.
    """
    account = str(account_key)
    data = read_state()
    data["board"] = BOARD
    data["version"] = VERSION
    records = data.get("accounts")
    if not isinstance(records, dict):
        records = {}
    record = records.get(account)
    if not isinstance(record, dict):
        record = {}
    record.update({k: v for k, v in fields.items() if k in STATUS_FIELDS})
    record["board"] = BOARD
    record["version"] = VERSION
    record.setdefault("account", account)
    record.setdefault("state", "unknown")
    record.setdefault("last_run", None)
    record.setdefault("last_success", None)
    record.setdefault("applied_today", 0)
    record.setdefault("daily_cap", 0)
    record.setdefault("message", "")
    record["updated_at"] = ist_iso()
    records[account] = {k: record.get(k) for k in STATUS_FIELDS}
    data["accounts"] = records
    data["updated_at"] = record["updated_at"]
    _atomic_write(STATUS_PATH, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return records[account]


def append_application(job_id: int, title: str, company: str, url: str, status: str,
                       applied_at: str | None = None) -> dict:
    """Append one application/decision record to state/applications.jsonl."""
    if status not in ("applied", "skipped", "failed"):
        raise ValueError(f"unknown application status: {status}")
    record = {
        "board": BOARD,
        "job_id": int(job_id),
        "title": title or "",
        "company": company or "",
        "url": url or "",
        "applied_at": applied_at or ist_iso(),
        "status": status,
    }
    APPLICATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({k: record[k] for k in APPLICATION_FIELDS}, ensure_ascii=False) + "\n"
    with open(APPLICATIONS_PATH, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    return record


def pause_info() -> dict | None:
    """Return the pause record, or None when not paused."""
    if not PAUSED_PATH.exists():
        return None
    try:
        data = json.loads(PAUSED_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"reason": "paused"}
    except (OSError, ValueError):
        return {"reason": "paused"}


def write_pause(reason: str = "") -> dict:
    record = {"paused_at": ist_iso(), "reason": reason or "paused by control.py"}
    _atomic_write(PAUSED_PATH, json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return record


def clear_pause() -> bool:
    if not PAUSED_PATH.exists():
        return False
    PAUSED_PATH.unlink(missing_ok=True)
    return True
