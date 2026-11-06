# Instahyre auto apply

A small Windows tool that applies to the jobs Instahyre itself matched to an account, and
refreshes that account's resume. It drives Edge over the DevTools protocol with a persistent,
already-logged-in profile. There is no WebDriver and no password typing.

Dry run is the default. Live mode needs two flags.

## Requirements

- Windows with Edge installed.
- Python 3.10 or newer.
- A persistent Edge profile per account that is already signed in to Instahyre.

## Quickstart

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy config.example.json config.json
# fill in the placeholders in config.json

python -m py_compile instahyre_apply.py refresh.py control.py publish.py edge_cdp.py
python instahyre_apply.py --help
python control.py status
```

## Account file (outside the repo)

Candidate ids and profile directories never live in this repo. Put them in
`%USERPROFILE%\.secrets\instahyre_accounts.json`:

```json
{
  "1": {
    "candidate_id": 100001,
    "profile_dir": "C:/Users/<you>/.secrets/profiles/instahyre-1"
  },
  "2": {
    "candidate_id": 100002,
    "profile_dir": "C:/Users/<you>/.secrets/profiles/instahyre-2"
  }
}
```

The account file is merged into `config.json` at runtime. If it is missing, every command stops
with a clear error. `INSTAHYRE_ACCOUNTS` can point somewhere else.

The label in status output comes from `config.json`. Candidate ids and profile directories are
never written to status or logs.

## Config and identity

Copy `config.example.json` to `config.json`. It contains the daily cap, pacing, the off-role
words, and one block per account. No candidate ids and no profile directories belong here.

Each account points at an identity file:

```json
{
  "job_search": {
    "relevance_keywords": ["data engineer", "data platform"],
    "avoid_companies": ["Example Corp"]
  }
}
```

The filter keeps a job when its title has an identity keyword, or when a generic build title
(engineer, developer, SDE) has an identity keyword in its skill tags. It drops inactive jobs,
location mismatches, avoided companies, senior titles (Lead, Staff, Principal, Head, Manager,
Architect, Director, VP, Chief), off-role titles, and off-stack titles. The daily cap is enforced
across runs by counting today's `applied` rows in `state/applications.jsonl`.

Path values accept `~` and environment variables. `INSTAHYRE_CONFIG` overrides the config path.
`INSTAHYRE_EDGE_EXE` overrides the Edge executable, and `edge_exe` in `config.json` does the same
for one install.

## Dry run

Dry run validates the account file, config, identity file, and budget. It does not open a browser
and makes no network calls.

```powershell
python instahyre_apply.py --dry-run --account 1
python refresh.py --dry-run --account 1
```

To exercise the relevance filter offline, save a feed response and pass it as a fixture:

```powershell
python instahyre_apply.py --dry-run --account 1 --fixture feed-account-1.json
```

A fixture is either a JSON list of opportunities or an object with `objects`, `feed`, or
`opportunities`. Dry run writes `state/status.json` with `state: "dry_run"`.

## Live gate

Live applies and refreshes require both `--live` and `--confirm-live`:

```powershell
python instahyre_apply.py --live --confirm-live --account 1
python instahyre_apply.py --live --confirm-live --account 1 --limit 5
python instahyre_apply.py --live --confirm-live --account 1 --visible

python refresh.py --live --confirm-live --account all
python refresh.py --live --confirm-live --account 2 --force
```

`--visible` shows the Edge window. Use it for the first login or when a session needs
inspection. The tools never answer a captcha or OTP. A logged-out profile stops with exit 42 and
a marker in `state/`.

## Pause and status

```powershell
python control.py pause --reason "checking something"
python control.py status
python control.py status --json
python control.py resume
```

Pause creates `state/PAUSED`. Both live tools check it before doing any work and exit 43. Status
reads `state/status.json`; each account record has `board`, `version`, `account` (label only),
`state`, `last_run`, `last_success`, `applied_today`, `daily_cap`, `message`, and `updated_at`.
All timestamps are IST (UTC+05:30).

Application rows live in `state/applications.jsonl` with `board`, `job_id`, `title`, `company`,
`url`, `applied_at` (IST), and `status` (`applied`, `skipped`, or `failed`).

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Finished, nothing left, or already done today |
| 1 | Other error |
| 42 | Needs the user (logged out, wrong account, missing resume) |
| 43 | Paused by `control.py` |
| 44 | Daily cap reached |
| 47 | Instahyre throttled the account (HTTP 429) |

## Disclaimer

This tool is not affiliated with Instahyre. Automating applications may violate Instahyre's
terms of service and can lead to rate limits, account restrictions, or a ban. It submits real
applications that cannot be withdrawn. You are responsible for the jobs it applies to and for
the account you point it at. Use it at your own risk. The software is provided as is, with no
warranty.

## License

MIT. See `LICENSE`.
