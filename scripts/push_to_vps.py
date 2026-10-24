#!/usr/bin/env python3
"""Push today's ActivityWatch summary to the coach VPS (fork addition).

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
      "private_config": "my_categories.json"
    }

The VPS side should pin that key to a forced command that only accepts the
summary on stdin (see README). Nothing but the aggregated JSON summary is sent:
categories, app/site minutes, scores, hourly totals - no raw event stream.
"""
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "push.log"


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


def merged_config(cfg: dict) -> Path:
    """Overlay the private config (personal sites/apps) on the public one."""
    base = json.loads((HERE / cfg.get("category_config", "category_config.windows-zh.json")).read_text(encoding="utf-8"))
    priv_path = HERE / cfg.get("private_config", "my_categories.json")
    if priv_path.exists():
        priv = json.loads(priv_path.read_text(encoding="utf-8"))
        for key, val in priv.items():
            if key == "domains" and isinstance(val, dict):
                base.setdefault("domains", {}).update(val)
            elif isinstance(val, dict) and key in base and isinstance(base[key], dict):
                for field in ("apps", "titles"):
                    if field in val:
                        base[key][field] = list(dict.fromkeys(val[field] + base[key].get(field, [])))
                if "weight" in val:
                    base[key]["weight"] = val["weight"]
            else:
                base[key] = val
    out = HERE / ".merged_config.json"
    out.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    return out


def summarize(day: str, config_path: Path) -> dict:
    cmd = [sys.executable, "-X", "utf8", str(HERE / "analyze_aw.py"), "--fetch",
           "--from", day, "--to", day, "--config", str(config_path)]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if res.returncode != 0:
        raise RuntimeError(f"analyze_aw failed: {res.stderr.strip()[-400:]}")
    out = res.stdout
    start = out.find("{")
    if start < 0:
        raise RuntimeError("analyze_aw printed no JSON (no events for this day?)")
    summary = json.loads(out[start:])
    # Drop YouTube video titles: the coach needs minutes, not what you watched.
    yt = (summary.get("chrome_extension") or {}).get("youtube")
    if isinstance(yt, dict):
        yt.pop("title_breakdown", None)
    return summary


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
    config_path = merged_config(cfg)
    now = datetime.now()
    days = [now.strftime("%Y-%m-%d")]
    # Shortly after midnight, also finalize yesterday.
    if now.hour < 2:
        days.insert(0, (now - timedelta(days=1)).strftime("%Y-%m-%d"))
    ok = True
    for day in days:
        try:
            push(cfg, day, summarize(day, config_path))
            log(f"pushed {day}")
        except Exception as exc:  # keep going; the next run retries
            ok = False
            log(f"ERROR {day}: {exc}")
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
