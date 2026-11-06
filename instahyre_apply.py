"""Instahyre auto-applier: applies to the jobs Instahyre itself matched to each account.

Dry run is the default. It validates the config and account file, prints the budget, and,
with ``--fixture``, runs the relevance filter over saved feed JSON. It never opens a browser
and never makes a network call.

Live mode needs both ``--live`` and ``--confirm-live``:

  1. Start headless Edge on that account's persistent, already-logged-in profile (edge_cdp.py,
     no WebDriver) and confirm the page belongs to the expected candidate id.
  2. Read the "Recommended jobs" feed (undecided matches) from Instahyre's own API.
  3. Keep jobs whose title/skill tags match the account's identity. Drop avoided companies,
     location mismatches, senior titles, off-role titles, and off-stack titles.
  4. Apply exactly the way Instahyre's page does, one job at a time, with jittered pacing:
     POST /api/v1/candidate_opportunities/candidate_matching/apply
          {"job_id": <job.id>, "is_interested": true, "is_activity_page_job": false}
     A job counts as applied only when the server returns opp_id + applied_on; each one is
     then confirmed in Instahyre's own "Applied jobs" list (interview_status 1 = ACCEPTED).

The daily cap and the relevance filter are the only guard rails. Instahyre applications have
no form (screening questions arrive later by email) and cannot be withdrawn. Nothing here
types a password: a logged-out profile stops the run with exit 42 and a marker file.

Exit codes: 0 finished, 43 paused by control.py, 44 daily cap reached, 42 needs the user
(logged out / wrong account), 47 Instahyre throttled us (429), 1 other error.
"""
from __future__ import annotations

import argparse
import json
import msvcrt
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import publish
from edge_cdp import CDPError, EdgeSession, wait_for_ram

HERE = Path(__file__).resolve().parent
STATE_DIR = publish.STATE_DIR
LOG_DIR = HERE / "logs"
LOG_PATH = LOG_DIR / "instahyre_apply.log"
CONFIG_PATH: Path | None = None
CONFIG: dict = {}

SITE = "https://www.instahyre.com"
FEED = ("/api/v1/candidate_opportunities/candidate_matching?companies=&company_size=&industry_type="
        "&interest_facet=0&job_categories=&job_functions=&job_type=&limit=30&location=&offset={offset}")
SEARCH_FEED = "/api/v1/job_search?job_functions={fn_id}&limit=30&offset={offset}"
APPLY = "/api/v1/candidate_opportunities/candidate_matching/apply"
APPLIED_FEED = FEED.replace("interest_facet=0", "interest_facet=1")
ACCEPTED = 1   # OFFER_STATUSES in Instahyre's bundle: PENDING 0, ACCEPTED 1, DECLINED 2

EXIT_OK, EXIT_ERROR, EXIT_NEEDS_USER, EXIT_PAUSED, EXIT_CAP, EXIT_THROTTLED = 0, 1, 42, 43, 44, 47

OFF_STACK_TITLE = (r"(?<![a-z0-9])(java|j2ee|spring boot|\.net|dotnet|c#|php|golang|ruby|rails|salesforce|sap|"
                   r"abap|mainframe|cobol|pl ?/ ?sql|plsql|oracle developer|oracle apps|power ?bi|tableau|qlik|"
                   r"informatica|iics|ab ?initio|datastage|talend|pentaho|ssis|sharepoint|servicenow|react|angular|"
                   r"node\.?js|full ?stack|front ?end|embedded|firmware|palantir|mulesoft|guidewire|pega)(?![a-z0-9])")


class SetupError(Exception):
    """Configuration or account file problem; print plainly and stop."""


class Stop(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def log(account: str, msg: str, level: str = "INFO") -> None:
    line = f"[{publish.now_ist():%Y-%m-%d %H:%M:%S}] [{level}] [acct {account}] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ------------------------------------------------------------------ config
def accounts_path() -> Path:
    raw = os.environ.get("INSTAHYRE_ACCOUNTS")
    return Path(os.path.expanduser(raw)) if raw else Path.home() / ".secrets" / "instahyre_accounts.json"


def resolve_path(value: str | Path, base: Path | None = None) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if not path.is_absolute():
        path = (base or HERE) / path
    return path


def load_accounts() -> dict:
    path = accounts_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise SetupError(f"cannot read {path} ({e}); it holds each account's candidate_id and "
                         f"profile_dir. See README.md") from e
    except ValueError as e:
        raise SetupError(f"{path} is not valid JSON ({e})") from e
    if not isinstance(data, dict) or not data:
        raise SetupError(f"{path} must be a non-empty JSON object keyed by account (1, 2)")
    return data


def load_config() -> dict:
    global CONFIG, CONFIG_PATH
    raw = os.environ.get("INSTAHYRE_CONFIG")
    path = resolve_path(raw) if raw else HERE / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise SetupError(f"cannot read {path} ({e}); copy config.example.json to config.json "
                         f"and fill in the placeholders") from e
    except ValueError as e:
        raise SetupError(f"{path} is not valid JSON ({e})") from e
    if not isinstance(data, dict):
        raise SetupError(f"{path} must contain a JSON object")
    CONFIG_PATH = path.resolve()
    CONFIG = data
    return CONFIG


def bootstrap() -> dict:
    """Load accounts first so a fresh clone fails on the missing account file, not config."""
    accounts = load_accounts()
    load_config()
    merged = CONFIG.setdefault("accounts", {})
    if not isinstance(merged, dict):
        raise SetupError("config.json: accounts must be an object")
    for key, private in accounts.items():
        if not isinstance(private, dict):
            raise SetupError(f"{accounts_path()}: account {key} must be an object")
        entry = merged.setdefault(str(key), {})
        if not isinstance(entry, dict):
            raise SetupError(f"config.json: account {key} must be an object")
        entry.update(private)
    return CONFIG


def account_cfg(account: str) -> dict:
    cfg = ((CONFIG.get("accounts") or {}).get(str(account)))
    if not isinstance(cfg, dict):
        raise SetupError(f"account {account} is not configured in config.json")
    missing = [k for k in ("candidate_id", "profile_dir") if not cfg.get(k)]
    if missing:
        raise SetupError(f"account {account} is missing {', '.join(missing)} in {accounts_path()}")
    return cfg


def label_of(account: str, cfg: dict) -> str:
    return str(cfg.get("label") or f"Instahyre {account}")


def check_paused() -> None:
    info = publish.pause_info()
    if info is not None:
        raise Stop(EXIT_PAUSED, f"paused via control.py ({info.get('reason') or 'no reason'}); "
                                f"run `python control.py resume`")


# ------------------------------------------------------------------ state
def parse_ist(raw) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=publish.IST)
    return value.astimezone(publish.IST)


def applied_today() -> int:
    today = publish.now_ist().date()
    total = 0
    for row in publish.read_applications():
        if row.get("status") != "applied":
            continue
        when = parse_ist(row.get("applied_at"))
        if when is not None and when.date() == today:
            total += 1
    return total


def known_applied() -> set[int]:
    ids = set()
    for row in publish.read_applications():
        if row.get("status") == "applied":
            try:
                ids.add(int(row.get("job_id")))
            except (TypeError, ValueError):
                pass
    return ids


def marker(account: str, kind: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"INSTAHYRE_{kind}_{account}"


def check_throttle_markers(account: str) -> None:
    m = marker(account, "THROTTLED")
    if m.exists():
        try:
            until = datetime.fromisoformat(json.loads(m.read_text(encoding="utf-8"))["until"])
        except Exception:
            until = datetime.fromtimestamp(m.stat().st_mtime) + timedelta(minutes=30)
        if publish.now_ist().replace(tzinfo=None) < until.replace(tzinfo=None):
            raise Stop(EXIT_THROTTLED, f"throttled by Instahyre until {until:%H:%M} (marker {m.name})")
        m.unlink(missing_ok=True)
    p = marker(account, "PROTECTION")
    if p.exists():
        if p.read_text(encoding="utf-8").strip()[:10] == publish.now_ist().strftime("%Y-%m-%d"):
            raise Stop(EXIT_THROTTLED, f"Instahyre's abuse protection fired earlier today (marker {p.name})")
        p.unlink(missing_ok=True)


# ------------------------------------------------------------------ relevance
def identity_filter(account_cfg: dict):
    if account_cfg.get("identity_config"):
        base = CONFIG_PATH.parent if CONFIG_PATH else HERE
        path = resolve_path(account_cfg["identity_config"], base)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise Stop(EXIT_ERROR, f"cannot read identity config {path} ({e})")
        except ValueError as e:
            raise Stop(EXIT_ERROR, f"identity config {path} is not valid JSON ({e})")
        js = data.get("job_search") if isinstance(data, dict) else None
        if not isinstance(js, dict):
            js = data if isinstance(data, dict) else {}
    else:
        js = account_cfg
    tokens = [str(t).lower().strip() for t in (js.get("relevance_keywords") or js.get("keywords") or [])
              if str(t).strip()]
    if not tokens:
        raise Stop(EXIT_ERROR, "no relevance_keywords in the identity config; refusing to apply blindly")
    avoid = [str(c).lower().strip() for c in (js.get("avoid_companies") or []) if str(c).strip()]
    generic = [str(w).lower().strip() for w in (account_cfg.get("generic_role_words") or []) if str(w).strip()]
    if not generic:
        raise Stop(EXIT_ERROR, "no generic_role_words in config.json for this account")
    tok_res = [re.compile(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])") for t in tokens]
    avoid_res = [re.compile(r"(?<![a-z0-9])" + re.escape(c) + r"(?![a-z0-9])") for c in avoid]
    role_re = re.compile(r"(?<![a-z0-9])(" + "|".join(map(re.escape, generic)) + r")(?![a-z0-9])")
    off_role_words = [str(w).lower().strip() for w in (CONFIG.get("off_role_words") or []) if str(w).strip()]
    off_role_re = (re.compile(r"(?<![a-z0-9])(" + "|".join(map(re.escape, off_role_words)) + r")(?![a-z0-9])")
                   if off_role_words else None)
    # Never Lead / Staff / Principal / Head / Manager / Architect / Director / VP / Chief.
    # "Senior" is fine.
    senior_re = re.compile(r"(?<![a-z0-9])(lead|staff|principal|head|manager|architect|director|vp|"
                           r"vice president|chief)(?![a-z0-9])")
    # Never open a job built on a stack that is not on the resume.
    off_stack_re = re.compile(OFF_STACK_TITLE)

    def decide(opp: dict) -> str | None:
        """None = apply; otherwise the reason to skip."""
        job, emp = opp.get("job") or {}, opp.get("employer") or {}
        if not (opp.get("is_active") and job.get("is_active")):
            return "inactive"
        if not opp.get("is_location_match"):
            return "location mismatch"
        company = f"{emp.get('company_name') or ''} {job.get('hiring_company_name') or ''}".lower()
        if any(r.search(company) for r in avoid_res):
            return "avoided company"
        title = f"{job.get('title') or ''} | {job.get('candidate_title') or ''}".lower()
        tags = " | ".join(job.get("keywords") or []).lower()
        if off_role_re is not None and off_role_re.search(title):
            return "off-role title"
        if senior_re.search((job.get("title") or "").lower()):
            return "senior title (lead/staff/principal/head/manager/architect)"
        if off_stack_re.search((job.get("title") or "").lower()):
            return "off-stack title (a technology not on the resume)"
        if any(r.search(title) for r in tok_res):
            return None
        if role_re.search(title) and any(r.search(tags) for r in tok_res):
            return None
        return "not this identity's role"

    return decide


# ------------------------------------------------------------------ Instahyre calls
def check_throttle(account: str, res: dict, what: str) -> None:
    if res["status"] != 429:
        return
    data = res.get("data") or {}
    if data.get("protection_activated"):
        marker(account, "PROTECTION").write_text(publish.ist_iso(), encoding="utf-8")
        raise Stop(EXIT_THROTTLED, f"{what}: 429 protection_activated - Instahyre's abuse limit; stopping for today")
    try:
        wait = int(res.get("retryAfter") or 0)
    except ValueError:
        wait = 0
    until = publish.now_ist() + timedelta(seconds=max(wait, 30 * 60))
    marker(account, "THROTTLED").write_text(json.dumps({"until": until.isoformat(timespec="seconds"),
                                                        "data": data}), encoding="utf-8")
    raise Stop(EXIT_THROTTLED, f"{what}: 429 (retry-after {res.get('retryAfter')}) - pausing until {until:%H:%M}")


def check_logged_out(account: str, res: dict, what: str) -> None:
    if res["status"] in (401, 403) or (res.get("data") or {}).get("logged_out"):
        marker(account, "LOGIN_REQUIRED").write_text(publish.ist_iso(), encoding="utf-8")
        raise Stop(EXIT_NEEDS_USER, f"{what}: HTTP {res['status']} logged out - the profile needs a sign-in")


def open_session(account: str, cfg: dict, visible: bool) -> EdgeSession:
    waited = wait_for_ram()
    if waited:
        log(account, f"before starting Edge: {waited}", "WARN")
    exe = CONFIG.get("edge_exe") or None
    profile = resolve_path(cfg["profile_dir"])
    try:
        s = EdgeSession(profile, headless=not visible, exe=exe)
        s.start()
    except CDPError as e:
        # A previous run's Edge still holding the profile makes the new one hand off and exit.
        log(account, f"Edge start failed ({e}); closing stale Edge on this profile and retrying in 45 s", "WARN")
        kill_profile_edges(profile)
        time.sleep(45)
        try:
            s = EdgeSession(profile, headless=not visible, exe=exe).start()
        except CDPError as e2:
            raise Stop(EXIT_ERROR, f"Edge did not start twice ({e2}); a start failure, not a sign-out")
    try:
        s.navigate(f"{SITE}/candidate/opportunities/?matching=true", timeout=90)
    except CDPError as e:
        s.close()
        raise Stop(EXIT_ERROR, f"Instahyre did not load ({e}); a load failure, not a sign-out")
    time.sleep(3)
    # Signed in or not is decided by Instahyre's own API, not by the page path.
    try:
        probe = s.fetch_json(FEED.format(offset=0))
    except CDPError as e:
        s.close()
        raise Stop(EXIT_ERROR, f"sign-in check could not run ({e}); not a sign-out")
    try:
        check_throttle(account, probe, "sign-in check")
        check_logged_out(account, probe, "sign-in check")
    except Stop:
        s.close()
        raise
    if probe["status"] != 200:
        s.close()
        raise Stop(EXIT_ERROR, f"sign-in check: HTTP {probe['status']} {probe.get('text') or ''}"[:300])
    who = json.loads(s.eval("""JSON.stringify({path: location.pathname,
        cid: (typeof candidateId !== 'undefined') ? candidateId : null,
        anon: (typeof isAnonymous !== 'undefined') ? isAnonymous : null})"""))
    try:
        cid = int(who["cid"]) if who["cid"] not in (None, "", "None") else None
    except (TypeError, ValueError):
        cid = None
    if cid is None or who["anon"] in (True, "True", "true") or not who["path"].startswith("/candidate"):
        s.close()
        raise Stop(EXIT_ERROR, f"feed answered 200 but the page shows no candidate (landed on {who['path']}); "
                               f"retrying later, not a sign-out")
    if cid != int(cfg["candidate_id"]):
        s.close()
        raise Stop(EXIT_NEEDS_USER,
                   f"profile is signed in as candidate {cid}, expected the configured account; refusing to apply")
    marker(account, "LOGIN_REQUIRED").unlink(missing_ok=True)
    return s


def kill_profile_edges(profile_dir: str | Path) -> None:
    needle = str(profile_dir).replace("'", "''")
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | Where-Object { $_.CommandLine -like "
          f"'*{needle}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}")
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=90,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        pass
    time.sleep(2)


def fetch_feed(account: str, s: EdgeSession, max_pages: int) -> tuple[list[dict], int]:
    opps, total, offset = [], 0, 0
    for _ in range(max_pages):
        res = s.fetch_json(FEED.format(offset=offset))
        check_throttle(account, res, "feed")
        check_logged_out(account, res, "feed")
        if res["status"] != 200:
            raise Stop(EXIT_ERROR, f"feed: HTTP {res['status']} {res.get('text')}")
        data = res["data"] or {}
        meta = data.get("meta") or {}
        total = meta.get("total_count") or total
        page = data.get("objects") or []
        opps.extend(page)
        if not meta.get("next") or not page:
            break
        offset += len(page)
    return opps, total


def fetch_search_feed(account: str, s: EdgeSession, search_fns: list[int], max_pages: int = 2) -> list[dict]:
    """Fallback search when the matching feed has no undecided jobs left."""
    opps = []
    seen_ids = set()
    for fn_id in search_fns:
        offset = 0
        for p in range(max_pages):
            if p > 0:
                time.sleep(random.uniform(2.0, 3.5))
            res = s.fetch_json(SEARCH_FEED.format(fn_id=fn_id, offset=offset))
            check_throttle(account, res, f"search {fn_id}")
            check_logged_out(account, res, f"search {fn_id}")
            if res.get("status") != 200:
                break
            data = res.get("data") or {}
            meta = data.get("meta") or {}
            raw_jobs = data.get("objects") or []
            if not raw_jobs:
                break
            for j in raw_jobs:
                jid = int(j.get("id") or 0)
                if not jid or jid in seen_ids:
                    continue
                seen_ids.add(jid)
                # Any Indian city or remote is fine. Drop clearly foreign listings.
                locs = str(j.get("locations") or "").lower()
                abroad = re.search(r"(?<![a-z])(usa|united states|u\.s\.|uk|united kingdom|london|singapore|dubai|"
                                   r"uae|canada|germany|australia|europe|netherlands|ireland|poland)(?![a-z])", locs)
                opps.append({
                    "employer": j.get("employer") or {},
                    "job": j,
                    "is_location_match": not abroad,
                    "interview_status": j.get("interview_status"),
                    "is_active": j.get("is_active", True),
                    "score": j.get("score", 0),
                })
            if not meta.get("next") or not raw_jobs:
                break
            offset += len(raw_jobs)
    return opps


# ------------------------------------------------------------------ output
def job_url(job: dict) -> str:
    return job.get("public_url") or (SITE + (job.get("opportunity_url") or ""))


def record_decision(opp: dict, status: str) -> None:
    job, emp = opp.get("job") or {}, opp.get("employer") or {}
    try:
        publish.append_application(
            job_id=int(job.get("id") or 0),
            title=str(job.get("title") or ""),
            company=str(emp.get("company_name") or job.get("hiring_company_name") or ""),
            url=str(job_url(job)),
            status=status,
        )
    except (TypeError, ValueError):
        pass


# ------------------------------------------------------------------ dry run
def run_dry(account: str, limit: int | None, fixture: str | None) -> int:
    cfg = account_cfg(account)
    label = label_of(account, cfg)
    cap = int(CONFIG.get("daily_cap", 30))
    done = applied_today()
    budget = max(0, cap - done)
    if limit is not None:
        budget = min(budget, limit)
    started = publish.now_ist()
    try:
        decide = identity_filter(cfg)
        known = known_applied()
        would_apply = skipped = 0
        if fixture:
            path = resolve_path(fixture)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except OSError as e:
                raise SetupError(f"cannot read fixture {path} ({e})") from e
            except ValueError as e:
                raise SetupError(f"fixture {path} is not valid JSON ({e})") from e
            opps = data if isinstance(data, list) else None
            if opps is None and isinstance(data, dict):
                opps = data.get("objects") or data.get("feed") or data.get("opportunities")
            if not isinstance(opps, list):
                raise SetupError(f"fixture {path} must be a list or an object with objects/feed/opportunities")
            for opp in opps:
                if not isinstance(opp, dict):
                    continue
                job = opp.get("job") or {}
                try:
                    jid = int(job.get("id") or 0)
                except (TypeError, ValueError):
                    jid = 0
                title = f"{job.get('title')} @ {(opp.get('employer') or {}).get('company_name')} (job {jid})"
                if not jid:
                    continue
                if jid in known:
                    log(account, f"DRY RUN already applied: {title}")
                    continue
                reason = decide(opp)
                if reason:
                    skipped += 1
                    log(account, f"DRY RUN skip ({reason}): {title}")
                elif would_apply < budget:
                    would_apply += 1
                    log(account, f"DRY RUN would apply: {title}")
                else:
                    log(account, f"DRY RUN over budget: {title}")
            note = f"dry run; would apply {would_apply}, skipped {skipped} (no network)"
        else:
            log(account, f"DRY RUN {label}: config valid, budget {budget}/{cap} "
                         f"({done} applied today); no network calls made")
            note = "dry run; no network"
        publish.write_state(account, account=label, state="dry_run",
                            last_run=publish.ist_iso(started), applied_today=done,
                            daily_cap=cap, message=note)
        return EXIT_OK
    except Stop as e:
        log(account, e.reason, "ERROR" if e.code == EXIT_ERROR else "INFO")
        publish.write_state(account, account=label, state="error",
                            last_run=publish.ist_iso(started), applied_today=done,
                            daily_cap=cap, message=e.reason)
        return e.code


# ------------------------------------------------------------------ live run
def run_account(account: str, limit: int | None, visible: bool) -> int:
    cfg = account_cfg(account)
    label = label_of(account, cfg)
    cap = int(CONFIG.get("daily_cap", 30))
    started = publish.now_ist()
    applied = skipped = 0
    applied_ids: list[int] = []
    feed_total = None
    code, note = EXIT_OK, ""
    s = None
    publish.write_state(account, account=label, state="running", last_run=publish.ist_iso(started),
                        applied_today=applied_today(), daily_cap=cap, message="run started")
    try:
        check_paused()
        done_today = applied_today()
        budget = cap - done_today
        if limit is not None:
            budget = min(budget, limit)
        if budget <= 0:
            raise Stop(EXIT_CAP, f"daily cap reached ({done_today}/{cap} applied today)")
        decide = identity_filter(cfg)
        log(account, f"{label}: starting ({done_today}/{cap} applied today, budget {budget})")

        s = open_session(account, cfg, visible)
        opps, feed_total = fetch_feed(account, s, int(CONFIG.get("max_feed_pages", 10)))
        known = known_applied()
        log(account, f"feed: {len(opps)} undecided matches (server total {feed_total})")

        queue = []
        for opp in opps:
            job = opp.get("job") or {}
            try:
                job_id = int(job.get("id") or 0)
            except (TypeError, ValueError):
                job_id = 0
            if not job_id or job_id in known:
                continue
            reason = decide(opp)
            if reason:
                skipped += 1
                record_decision(opp, "skipped")
                continue
            queue.append(opp)
        # Fallback to job_search when the matching feed has fewer relevant jobs than budget.
        search_fns = cfg.get("search_job_functions") or []
        if len(queue) < budget and search_fns:
            log(account, f"matching feed has only {len(queue)} relevant job(s); "
                         f"checking job_search for functions {search_fns}")
            queued_ids = {int((o.get("job") or {}).get("id") or 0) for o in queue}
            search_opps = fetch_search_feed(account, s, search_fns,
                                            max_pages=int(CONFIG.get("max_feed_pages", 5)))
            for opp in search_opps:
                if len(queue) >= budget:
                    break
                job = opp.get("job") or {}
                try:
                    job_id = int(job.get("id") or 0)
                except (TypeError, ValueError):
                    job_id = 0
                if not job_id or job_id in known or job_id in queued_ids:
                    continue
                reason = decide(opp)
                if reason:
                    skipped += 1
                    record_decision(opp, "skipped")
                    continue
                queue.append(opp)
                queued_ids.add(job_id)

        queue.sort(key=lambda o: float(o.get("score") or 0), reverse=True)
        log(account, f"{len(queue)} relevant (feed + search), {skipped} skipped by filter")

        pace = CONFIG.get("pace_seconds", [35, 90])
        failures_in_row = 0
        for n, opp in enumerate(queue[:budget]):
            job, emp = opp["job"], opp.get("employer") or {}
            label_text = (f"{job.get('title')} @ {emp.get('company_name')} "
                          f"(job {job['id']}, score {opp.get('score')})")
            if n:
                time.sleep(random.uniform(*pace))
            res = s.fetch_json(APPLY, method="POST",
                               body={"job_id": int(job["id"]), "is_interested": True,
                                     "is_activity_page_job": False})
            check_throttle(account, res, f"apply {job['id']}")
            check_logged_out(account, res, f"apply {job['id']}")
            data = res.get("data") or {}
            if res["status"] in (200, 201) and data.get("opp_id") and data.get("applied_on"):
                record_decision(opp, "applied")
                applied += 1
                applied_ids.append(int(job["id"]))
                failures_in_row = 0
                log(account, f"APPLIED {label_text} -> opp {data['opp_id']} applied_on {data['applied_on']}")
            else:
                failures_in_row += 1
                err = (data.get("error") if isinstance(data, dict) else None) or res.get("text") or data
                record_decision(opp, "failed")
                log(account, f"apply failed for {label_text}: HTTP {res['status']} {str(err)[:200]}", "WARN")
                if failures_in_row >= 3:
                    raise Stop(EXIT_ERROR, "3 apply failures in a row; stopping this run")
        confirmed = verify_applied(account, s, applied_ids) if applied_ids else 0
        if applied_today() >= cap:
            code, note = EXIT_CAP, f"daily cap reached ({cap})"
        elif len(queue) <= budget:
            note = "nothing left to apply"
        else:
            note = "run limit reached"
        if applied:
            note += f"; {applied} applied, {confirmed} confirmed in the Applied list"
    except Stop as e:
        code, note = e.code, e.reason
        log(account, e.reason, "ERROR" if e.code in (EXIT_ERROR, EXIT_NEEDS_USER, EXIT_THROTTLED) else "INFO")
    except Exception as e:
        code, note = EXIT_ERROR, f"{type(e).__name__}: {e}"
        log(account, f"run crashed: {note}", "ERROR")
    finally:
        if s is not None:
            s.close()
        now = publish.ist_iso()
        previous = (publish.read_state().get("accounts") or {}).get(account) or {}
        state = {EXIT_OK: "ok", EXIT_CAP: "capped", EXIT_NEEDS_USER: "needs_login",
                 EXIT_THROTTLED: "throttled", EXIT_PAUSED: "paused", EXIT_ERROR: "error"}.get(code, "error")
        publish.write_state(account, account=label, state=state,
                            last_run=publish.ist_iso(started),
                            last_success=now if code in (EXIT_OK, EXIT_CAP)
                            else previous.get("last_success"),
                            applied_today=applied_today(), daily_cap=cap, message=note)
    log(account, f"run finished: exit {code}, applied {applied}, skipped {skipped} ({note})")
    return code


def verify_applied(account: str, s: EdgeSession, job_ids: list[int], max_pages: int = 10) -> int:
    """Confirm this run's applications against the "Applied jobs" list (interest_facet=1)."""
    pending = set(job_ids)
    if not pending:
        return 0
    confirmed, offset = set(), 0
    for _ in range(max_pages):
        res = s.fetch_json(APPLIED_FEED.format(offset=offset))
        if res["status"] != 200:
            log(account, f"read-back: applied list HTTP {res['status']}", "WARN")
            break
        data = res.get("data") or {}
        page = data.get("objects") or []
        for o in page:
            try:
                jid = int((o.get("job") or {}).get("id") or 0)
            except (TypeError, ValueError):
                continue
            if jid in pending and o.get("interview_status") == ACCEPTED:
                confirmed.add(jid)
        if confirmed == pending or not (data.get("meta") or {}).get("next") or not page:
            break
        offset += len(page)
    missing = pending - confirmed
    log(account, f"read-back: {len(confirmed)} confirmed in Instahyre's Applied list"
                 + (f", {len(missing)} not found yet ({sorted(missing)[:5]})" if missing else ""),
        "WARN" if missing else "INFO")
    return len(confirmed)


# ------------------------------------------------------------------ CLI
def print_status() -> int:
    state = publish.read_state()
    pause = publish.pause_info()
    if not state:
        print("no status yet")
        return EXIT_OK
    print(f"{state.get('board') or publish.BOARD} {state.get('version') or publish.VERSION}"
          f" (updated {state.get('updated_at') or '-'})")
    print(f"paused: {'yes - ' + str((pause or {}).get('reason') or '') if pause else 'no'}")
    records = state.get("accounts") or {}
    for key in sorted(records, key=lambda k: str(k)):
        r = records[key] or {}
        print(f"account {key} [{r.get('account')}]: state={r.get('state')} "
              f"applied_today={r.get('applied_today')}/{r.get('daily_cap')} "
              f"last_run={r.get('last_run')} last_success={r.get('last_success')} "
              f"message={r.get('message')}")
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Apply to Instahyre's matched jobs. Dry run is the default.",
        epilog="Live: --live --confirm-live --account 1")
    ap.add_argument("--account", help="account key from config.json and the accounts file (1 or 2)")
    ap.add_argument("--limit", type=int, help="apply to at most N jobs this run (still bounded by the daily cap)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="validate config and print the plan; no browser, no network (default)")
    mode.add_argument("--live", action="store_true", help="apply for real; also requires --confirm-live")
    ap.add_argument("--confirm-live", action="store_true", help="second confirmation for --live")
    ap.add_argument("--fixture", help="dry run only: saved feed JSON to run the relevance filter over")
    ap.add_argument("--visible", action="store_true", help="show the browser window")
    ap.add_argument("--status", action="store_true", help="print state/status.json, then exit")
    args = ap.parse_args()

    if args.status:
        return print_status()
    if args.live and not args.confirm_live:
        ap.error("--live requires --confirm-live")
    if args.confirm_live and not args.live:
        ap.error("--confirm-live is only valid with --live")
    if args.fixture and args.live:
        ap.error("--fixture is only valid in dry-run mode")
    if not args.account:
        ap.error("--account is required")
    if args.limit is not None and args.limit < 0:
        ap.error("--limit must be zero or greater")

    try:
        bootstrap()
        account_cfg(args.account)
    except SetupError as e:
        print(f"instahyre_apply: {e}", file=sys.stderr)
        return EXIT_ERROR

    if not args.live:
        return run_dry(args.account, args.limit, args.fixture)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / f"instahyre_{args.account}.lock", "a+")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log(args.account, "another run for this account is active; exiting")
        return EXIT_OK
    return run_account(args.account, args.limit, args.visible)


if __name__ == "__main__":
    sys.exit(main())
