"""Pause, resume, or inspect the Instahyre worker.

  python control.py pause [--reason "text"]
  python control.py resume
  python control.py status [--json]

Pause writes state/PAUSED, which instahyre_apply.py and refresh.py check before doing any
work. Resume deletes it. Status reads state/status.json and prints one line per account.
"""
from __future__ import annotations

import argparse
import json
import sys

import publish


def cmd_pause(reason: str) -> int:
    record = publish.write_pause(reason)
    print(f"paused at {record['paused_at']}: {record['reason']}")
    return 0


def cmd_resume() -> int:
    if publish.clear_pause():
        print("resumed")
    else:
        print("not paused")
    return 0


def cmd_status(as_json: bool) -> int:
    data = publish.read_state()
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    if not data:
        print("no status yet")
        return 0
    print(f"{data.get('board') or publish.BOARD} {data.get('version') or publish.VERSION}"
          f" (updated {data.get('updated_at') or '-'})")
    pause = publish.pause_info()
    print(f"paused: {'yes - ' + str((pause or {}).get('reason') or '') if pause else 'no'}")
    records = data.get("accounts") or {}
    if not records:
        print("no accounts yet")
    for key in sorted(records, key=lambda k: str(k)):
        r = records[key] or {}
        print(f"account {key} [{r.get('account')}]: state={r.get('state')} "
              f"applied_today={r.get('applied_today')}/{r.get('daily_cap')} "
              f"last_run={r.get('last_run')} last_success={r.get('last_success')} "
              f"message={r.get('message')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Pause, resume, or inspect the Instahyre worker.")
    sub = ap.add_subparsers(dest="command", required=True)
    pause = sub.add_parser("pause", help="stop both tools before their next action")
    pause.add_argument("--reason", default="", help="why the worker is paused")
    sub.add_parser("resume", help="clear state/PAUSED")
    status = sub.add_parser("status", help="print state/status.json")
    status.add_argument("--json", action="store_true", help="print the raw status JSON")
    args = ap.parse_args()
    if args.command == "pause":
        return cmd_pause(args.reason)
    if args.command == "resume":
        return cmd_resume()
    return cmd_status(args.json)


if __name__ == "__main__":
    sys.exit(main())
