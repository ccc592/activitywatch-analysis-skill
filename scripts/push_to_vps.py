#!/usr/bin/env python3
"""Push the current coach day's ActivityWatch summary to the coach VPS (fork addition).

Runs on the Windows machine that hosts ActivityWatch, e.g. every 30 minutes
from Task Scheduler:

    pyw -3 -X utf8 push_to_vps.py

or, where Task Scheduler is not allowed, as a resident loop started from a
Startup-folder shortcut:

    pyw -3 -X utf8 push_to_vps.py --loop 1800

Settings come from push_config.json next to this script (not committed):

    {
      "ssh_host": "203.0.113.10",
      "ssh_port": 22,
      "ssh_user": "root",
      "ssh_key": "C:/Users/me/.ssh/aw_push_ed25519",
      "category_config": "category_config.windows-zh.json",
      "private_config": "my_categories.json",
      "day_start_hour": 4
    }

The summary is built by coach_summary.py (time at the computer only, coach day
from day_start_hour to day_start_hour). The VPS side should pin the key to a
forced command that only accepts the summary on stdin (see README). Nothing but
the aggregated summary is sent - no raw event stream.

When the coach day rolls over, the previous day is pushed once more so its
last minutes are included (state in .push_state.json).
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import coach_summary  # noqa: E402

LOG = HERE / "push.log"
STATE = HERE / ".push_state.json"


def log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    # keep the log small
    try:
        lines = LOG.read_text(encoding="utf-8").splitlines()
        if len(lines) > 500:
            LOG.write_text("\n".join(lines[-300:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def push(cfg: dict, day: str, payload: dict) -> None:
    body = json.dumps({"date": day, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                       "summary": payload}, ensure_ascii=False).encode("utf-8")
    cmd = ["ssh", "-i", cfg["ssh_key"], "-p", str(cfg.get("ssh_port", 22)),
           "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "StrictHostKeyChecking=accept-new",
           f"{cfg.get('ssh_user', 'root')}@{cfg['ssh_host']}", f"aw-push {day}"]
    res = subprocess.run(cmd, input=body, capture_output=True, timeout=60,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if res.returncode != 0:
        raise RuntimeError(f"ssh push failed ({res.returncode}): {res.stderr.decode('utf-8', 'replace').strip()[-300:]}")


def run_once() -> int:
    cfg = json.loads((HERE / "push_config.json").read_text(encoding="utf-8"))
    hour = int(cfg.get("day_start_hour", coach_summary.DAY_START_HOUR))
    rules = coach_summary.load_config(HERE / cfg.get("category_config", "category_config.windows-zh.json"),
                                      HERE / cfg.get("private_config", "my_categories.json"))
    today = coach_summary.coach_day(day_start_hour=hour)
    try:
        last = json.loads(STATE.read_text(encoding="utf-8")).get("last_day")
    except (OSError, ValueError):
        last = None
    days = [today]
    if last and last < today:
        days.insert(0, last)  # finalize the previous coach day once
    ok = True
    for day in days:
        try:
            summary = coach_summary.build_summary(day, rules, hour, cfg.get("aw_url", coach_summary.AW_URL))
            push(cfg, day, summary)
            log(f"pushed {day} active={summary['totals']['active_min']}min")
        except Exception as exc:  # keep going; the next run retries
            ok = False
            log(f"ERROR {day}: {exc}")
    if ok:
        STATE.write_text(json.dumps({"last_day": today}), encoding="utf-8")
    return 0 if ok else 1


def main() -> int:
    # --loop SECONDS: stay resident and push every SECONDS (for a Startup-folder
    # shortcut when Task Scheduler is not available). A localhost socket acts as a
    # single-instance lock.
    if "--loop" in sys.argv:
        import socket
        import time
        interval = int(sys.argv[sys.argv.index("--loop") + 1])
        lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            lock.bind(("127.0.0.1", 47615))
        except OSError:
            return 0  # another instance is already running
        log(f"loop started, interval {interval}s")
        while True:
            try:
                run_once()
            except Exception as exc:
                log(f"ERROR loop: {exc}")
            time.sleep(interval)
    return run_once()


if __name__ == "__main__":
    sys.exit(main())
