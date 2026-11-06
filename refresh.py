"""Instahyre-only resume refresh for account 1 and account 2.

Instahyre sorts candidates by "profile updated" date, so re-uploading the same resume once a
day keeps the profile current. A refresh counts as done only when Instahyre's own profile API
reports a new ``resume.uploaded_on``.

Dry run is the default: it validates the account file, config, and resume path, writes status,
and makes no network calls. Live mode needs ``--live`` and ``--confirm-live``.

The browser uses the account's persistent Edge profile and must already be signed in.
Nothing here types a password: a logged-out profile stops with exit 42.

Exit codes: 0 refreshed or already done today, 1 retryable failure, 42 needs the user,
43 paused by control.py.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import instahyre_apply as ih
import publish
from edge_cdp import CDPError

PROFILE_URL = "https://www.instahyre.com/candidate/profile/"
CANDIDATE_API = "/api/v1/candidate_misc/profile/candidate/{candidate_id}"

EXIT_OK = ih.EXIT_OK
EXIT_ERROR = ih.EXIT_ERROR
EXIT_NEEDS_USER = ih.EXIT_NEEDS_USER


class RefreshError(Exception):
    pass


def resume_path(cfg: dict) -> Path:
    raw = str(cfg.get("resume") or "").strip()
    if not raw:
        raise ih.SetupError("this account has no resume path in config.json")
    path = ih.resolve_path(raw, ih.CONFIG_PATH.parent if ih.CONFIG_PATH else ih.HERE)
    if not path.exists():
        raise ih.SetupError(f"resume file not found: {path}")
    return path


def parse_server_time(raw) -> datetime | None:
    """Parse Instahyre's timestamp and normalise it to IST. Naive values are treated as IST."""
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=publish.IST)
    return value.astimezone(publish.IST)


def resume_info(data: dict) -> dict:
    resume = data.get("resume") or {}
    return {"title": resume.get("title") or "",
            "uploaded_on": resume.get("uploaded_on"),
            "when": parse_server_time(resume.get("uploaded_on"))}


def fetch_profile(session, candidate_id) -> dict:
    res = session.fetch_json(CANDIDATE_API.format(candidate_id=int(candidate_id)))
    if res["status"] in (401, 403):
        raise ih.Stop(EXIT_NEEDS_USER, "profile API says logged out; the profile needs a sign-in")
    if res["status"] != 200:
        raise ih.Stop(EXIT_ERROR, f"profile API: HTTP {res['status']} {res.get('text') or ''}"[:300])
    data = res.get("data")
    if not isinstance(data, dict):
        raise ih.Stop(EXIT_ERROR, "profile API returned no candidate object")
    return data


def wait_for_input(session, selector: str, timeout: float = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if session.eval(f"!!document.querySelector({json.dumps(selector)})"):
                return True
        except CDPError:
            pass
        time.sleep(1)
    return False


def wait_for_upload(session, candidate_id, started: datetime, timeout: float = 90) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = resume_info(fetch_profile(session, candidate_id))
        except ih.Stop as e:
            if e.code == EXIT_NEEDS_USER:
                raise
            info = None
        if info and info["when"] and info["when"].timestamp() >= started.timestamp() - 60:
            return info
        time.sleep(5)
    return None


def write_status(account: str, cfg: dict, state: str, started: datetime, cap: int,
                 message: str, success: bool) -> None:
    previous = (publish.read_state().get("accounts") or {}).get(account) or {}
    publish.write_state(
        account,
        account=ih.label_of(account, cfg),
        state=state,
        last_run=publish.ist_iso(started),
        last_success=publish.ist_iso() if success else previous.get("last_success"),
        applied_today=0,
        daily_cap=cap,
        message=message,
    )


def dry_run(account: str, force: bool) -> int:
    cfg = ih.account_cfg(account)
    label = ih.label_of(account, cfg)
    cap = int(ih.CONFIG.get("daily_cap", 30))
    started = publish.now_ist()
    try:
        resume = resume_path(cfg)
    except ih.SetupError as e:
        write_status(account, cfg, "error", started, cap, str(e), False)
        print(f"refresh: account {account} [{label}]: {e}", file=sys.stderr)
        return EXIT_NEEDS_USER
    message = f"dry run; would upload {resume.name}{' (forced)' if force else ''}"
    write_status(account, cfg, "dry_run", started, cap, message, False)
    ih.log(account, f"DRY RUN {label}: {message}; no network calls made")
    return EXIT_OK


def refresh_account(account: str, visible: bool, force: bool) -> int:
    cfg = ih.account_cfg(account)
    label = ih.label_of(account, cfg)
    cap = int(ih.CONFIG.get("daily_cap", 30))
    started = publish.now_ist()
    code, note, state = EXIT_OK, "", "ok"
    session = None
    try:
        resume = resume_path(cfg)
        ih.check_paused()
        write_status(account, cfg, "running", started, cap, "resume refresh started", False)
        ih.log(account, f"{label}: refreshing {resume.name}")
        session = ih.open_session(account, cfg, visible)
        session.navigate(PROFILE_URL, timeout=90)
        data = fetch_profile(session, cfg["candidate_id"])
        before = resume_info(data)
        ih.log(account, f"before upload: title={before['title']!r} uploaded_on={before['uploaded_on']}")
        if before["when"] and before["when"].date() == publish.now_ist().date() and not force:
            state, note = "already_done", f"already refreshed today at {before['when']:%H:%M}"
        else:
            if not wait_for_input(session, "#resume-input"):
                raise ih.Stop(EXIT_ERROR, "#resume-input not found on the profile page")
            session.set_file_input("#resume-input", resume)
            after = wait_for_upload(session, cfg["candidate_id"], started)
            if not after:
                current = resume_info(fetch_profile(session, cfg["candidate_id"]))
                raise ih.Stop(EXIT_ERROR,
                              f"resume.uploaded_on still {current['uploaded_on']!r} after upload")
            session.navigate(PROFILE_URL, timeout=90)
            state = "refreshed"
            note = f"uploaded {after['title']!r} at {after['uploaded_on']}"
            ih.log(account, f"VERIFIED - {note}")
    except ih.Stop as e:
        code, note = e.code, e.reason
        state = {EXIT_NEEDS_USER: "needs_login", ih.EXIT_PAUSED: "paused",
                 EXIT_ERROR: "error"}.get(code, "error")
        ih.log(account, note, "ERROR" if code in (EXIT_ERROR, EXIT_NEEDS_USER) else "INFO")
    except (CDPError, RefreshError, OSError) as e:
        code, note, state = EXIT_ERROR, f"{type(e).__name__}: {e}", "error"
        ih.log(account, f"refresh crashed: {note}", "ERROR")
    except Exception as e:
        code, note, state = EXIT_ERROR, f"{type(e).__name__}: {e}", "error"
        ih.log(account, f"refresh crashed: {note}", "ERROR")
    finally:
        if session is not None:
            session.close()
        write_status(account, cfg, state, started, cap, note, code == EXIT_OK)
    ih.log(account, f"refresh finished: exit {code} ({note})")
    return code


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Refresh the Instahyre resume for account 1 and 2. Dry run is the default.")
    ap.add_argument("--account", choices=("1", "2", "all"), default="all",
                    help="1, 2, or all (default)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="validate config and print the plan; no browser, no network (default)")
    mode.add_argument("--live", action="store_true", help="upload for real; also requires --confirm-live")
    ap.add_argument("--confirm-live", action="store_true", help="second confirmation for --live")
    ap.add_argument("--visible", action="store_true", help="show the browser window")
    ap.add_argument("--force", action="store_true", help="upload even if already done today")
    args = ap.parse_args()

    if args.live and not args.confirm_live:
        ap.error("--live requires --confirm-live")
    if args.confirm_live and not args.live:
        ap.error("--confirm-live is only valid with --live")

    try:
        ih.bootstrap()
        accounts = ["1", "2"] if args.account == "all" else [args.account]
        for account in accounts:
            ih.account_cfg(account)
    except ih.SetupError as e:
        print(f"refresh: {e}", file=sys.stderr)
        return EXIT_ERROR

    rc = EXIT_OK
    for account in accounts:
        code = (refresh_account(account, args.visible, args.force) if args.live
                else dry_run(account, args.force))
        if code == EXIT_NEEDS_USER:
            rc = EXIT_NEEDS_USER
        elif code != EXIT_OK and rc == EXIT_OK:
            rc = code
    return rc


if __name__ == "__main__":
    sys.exit(main())
