#!/usr/bin/env python3
"""Coach-day ActivityWatch summary (fork addition).

Builds the compact JSON that the Hermes coach reads from ``~/coach/aw/<day>.json``.

What counts as computer time
    Only time you were actually at the computer, computed the way the
    ActivityWatch web UI does it: window events intersected with the AFK
    watcher's "not-afk" periods. Two additions, because watching a video
    produces no keyboard/mouse input:
      - a focused browser whose active tab is playing audio, and
      - a focused app listed in ``always_active_apps`` (video players)
    also count as present.

Away time
    Periods when the computer was on but nobody was at it (AFK, minus the
    presence additions above) are reported separately as ``away``; periods with
    no AFK data at all (computer off/asleep) as ``no_data``. Nothing about what
    was on screen while away is sent.

Browser time
    Browser window time is split by the web watcher's active-tab events, so
    each minute in Chrome is attributed to the site that was open, not to
    "chrome.exe". Browsers without a web watcher fall back to the window title.

Coach day
    The day runs from ``day_start_hour`` (default 04:00) to the same hour next
    morning, so a late night belongs to the evening before it.

Usage (on the machine that runs ActivityWatch; stdlib only):
    python -X utf8 coach_summary.py [--day YYYY-MM-DD] [--config FILE] [--private FILE]
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

SCHEMA = 2
AW_URL = "http://127.0.0.1:5600"
DAY_START_HOUR = 4
FLOOD_GAP_S = 5            # close gaps up to 5 s between events (ActivityWatch flood())
SLOT_MIN = 15              # timeline resolution
SESSION_GAP_S = 10 * 60    # breaks shorter than this stay inside one session
FOCUS_GAP_S = 2 * 60       # interruptions up to this long don't end a focus run
PRODUCTIVE_MIN_WEIGHT = 0.5

HERE = Path(__file__).resolve().parent

# Browser key (as in the web watcher bucket "aw-watcher-web-<key>") -> window app names.
BROWSER_KEYS = {
    "chrome": ("chrome.exe", "google chrome", "chrome", "chromium"),
    "edge": ("msedge.exe", "microsoft edge", "edge"),
    "firefox": ("firefox.exe", "firefox"),
    "brave": ("brave.exe", "brave browser", "brave"),
    "opera": ("opera.exe", "opera"),
    "vivaldi": ("vivaldi.exe", "vivaldi"),
}

DEFAULT_ALWAYS_ACTIVE = ["potplayer", "vlc", "mpv", "mpc-hc", "mpc-be"]

# Domain -> (category, weight). Config "domains" entries override these.
DEFAULT_DOMAINS = {
    # AI tools
    "anthropic.com": ("ai_tools", 0.8), "claude.ai": ("ai_tools", 0.8), "claude.com": ("ai_tools", 0.8),
    "openai.com": ("ai_tools", 0.8), "chatgpt.com": ("ai_tools", 0.8), "ai.google.dev": ("ai_tools", 0.8),
    "aistudio.google.com": ("ai_tools", 0.8), "gemini.google.com": ("ai_tools", 0.8),
    "perplexity.ai": ("ai_tools", 0.7), "huggingface.co": ("ai_tools", 0.8),
    "openrouter.ai": ("ai_tools", 0.7), "deepseek.com": ("ai_tools", 0.8),
    # Development
    "github.com": ("development", 0.9), "gitlab.com": ("development", 0.9),
    "stackoverflow.com": ("development", 0.8), "localhost": ("development", 1.0),
    "127.0.0.1": ("development", 1.0), "vercel.com": ("development", 0.8),
    "supabase.com": ("development", 0.8), "netlify.com": ("development", 0.8),
    "console.aws.amazon.com": ("development", 0.8), "console.cloud.google.com": ("development", 0.8),
    "dash.cloudflare.com": ("development", 0.8), "pypi.org": ("development", 0.8),
    "docs.python.org": ("development", 0.8), "developer.mozilla.org": ("development", 0.8),
    # Writing / docs / design / product
    "docs.google.com": ("writing", 0.7), "notion.so": ("writing", 0.8), "coda.io": ("writing", 0.7),
    "obsidian.md": ("writing", 0.8), "figma.com": ("design", 0.9), "canva.com": ("design", 0.7),
    "webflow.com": ("design", 0.8), "linear.app": ("product_work", 0.8), "trello.com": ("product_work", 0.6),
    "asana.com": ("product_work", 0.6), "calendar.google.com": ("business_tools", 0.5),
    "stripe.com": ("business_tools", 0.6), "analytics.google.com": ("business_tools", 0.6),
    "studio.youtube.com": ("content_creation", 0.7),
    # Email
    "mail.google.com": ("email", 0.3), "outlook.com": ("email", 0.3), "outlook.office.com": ("email", 0.3),
    # Learning
    "coursera.org": ("learning", 0.8), "udemy.com": ("learning", 0.7), "edx.org": ("learning", 0.8),
    "arxiv.org": ("learning", 0.9),
    # Distracting
    "youtube.com": ("video", -0.2), "netflix.com": ("entertainment", -0.5), "twitch.tv": ("entertainment", -0.4),
    "primevideo.com": ("entertainment", -0.5), "disneyplus.com": ("entertainment", -0.5),
    "x.com": ("social_media", -0.3), "twitter.com": ("social_media", -0.3), "reddit.com": ("social_media", -0.3),
    "facebook.com": ("social_media", -0.4), "instagram.com": ("social_media", -0.4),
    "tiktok.com": ("social_media", -0.5), "linkedin.com": ("social_media", -0.2),
    "douyin.com": ("social_media", -0.5), "kuaishou.com": ("social_media", -0.5),
    "weibo.com": ("social_media", -0.4), "xiaohongshu.com": ("social_media", -0.4),
    "zhihu.com": ("social_media", -0.3), "bilibili.com": ("video", -0.3), "hupu.com": ("social_media", -0.4),
    "v2ex.com": ("social_media", -0.2), "nodeseek.com": ("social_media", -0.2),
    "iqiyi.com": ("entertainment", -0.5), "youku.com": ("entertainment", -0.5), "v.qq.com": ("entertainment", -0.5),
}

# Lower-case title keyword -> (site label, category, weight); used for browser
# time without web-watcher data (e.g. browsers that have no extension).
DEFAULT_TITLE_SITES = {
    "抖音": ("Douyin", "social_media", -0.5), "douyin": ("Douyin", "social_media", -0.5),
    "快手": ("Kuaishou", "social_media", -0.5), "微博": ("Weibo", "social_media", -0.4),
    "小红书": ("Xiaohongshu", "social_media", -0.4), "知乎": ("Zhihu", "social_media", -0.3),
    "哔哩哔哩": ("Bilibili", "video", -0.3), "bilibili": ("Bilibili", "video", -0.3),
    "youtube": ("YouTube", "video", -0.2), "netflix": ("Netflix", "entertainment", -0.5),
    "twitch": ("Twitch", "entertainment", -0.4), " / x": ("Twitter/X", "social_media", -0.3),
    "twitter": ("Twitter/X", "social_media", -0.3), "reddit": ("Reddit", "social_media", -0.3),
    "instagram": ("Instagram", "social_media", -0.3), "tiktok": ("TikTok", "social_media", -0.4),
    "chatgpt": ("ChatGPT", "ai_tools", 0.8), "claude": ("Claude", "ai_tools", 0.8),
    "github": ("GitHub", "development", 0.9), "stack overflow": ("Stack Overflow", "development", 0.8),
    "google docs": ("Google Docs", "writing", 0.9), "notion": ("Notion", "writing", 0.9),
    "gmail": ("Gmail", "email", 0.3),
    "google cloud": ("Google Cloud", "development", 0.8),
    "cloudflare": ("Cloudflare", "development", 0.8),
    "openrouter": ("OpenRouter", "ai_tools", 0.7),
}


# ---------------------------------------------------------------- config

def load_config(base_path: Path, private_path: Path | None = None) -> dict:
    """Public category config with the private overrides merged on top.

    Private file format: {"domains": {"site.com": ["category", weight]},
    "<category>": {"apps": [...], "titles": [...], "weight": w}, ...}.
    """
    cfg = json.loads(Path(base_path).read_text(encoding="utf-8"))
    if private_path and Path(private_path).exists():
        priv = json.loads(Path(private_path).read_text(encoding="utf-8"))
        for key, val in priv.items():
            if key.startswith("_"):
                continue
            if key == "domains" and isinstance(val, dict):
                cfg.setdefault("domains", {}).update(val)
            elif isinstance(val, dict) and isinstance(cfg.get(key), dict):
                for field in ("apps", "titles"):
                    if field in val:
                        cfg[key][field] = list(dict.fromkeys(val[field] + cfg[key].get(field, [])))
                if "weight" in val:
                    cfg[key]["weight"] = val["weight"]
            else:
                cfg[key] = val
    return cfg


class Rules:
    def __init__(self, cfg: dict):
        self.categories = []
        for name, val in cfg.items():
            if name.startswith("_") or not isinstance(val, dict) or "weight" not in val:
                continue
            self.categories.append((name, float(val["weight"]),
                                    [a.lower() for a in val.get("apps", [])],
                                    [t.lower() for t in val.get("titles", [])]))
        self.domains = dict(DEFAULT_DOMAINS)
        for dom, (cat, weight) in (cfg.get("domains") or {}).items():
            self.domains[dom.lower()] = (cat, float(weight))
        self.title_sites = DEFAULT_TITLE_SITES
        self.browser_apps = {a.lower() for a in cfg.get("browser_apps", [])}
        for names in BROWSER_KEYS.values():
            self.browser_apps.update(names)
        self.telegram = [(cat, float(v.get("weight", 0.0)), [p.lower() for p in v.get("patterns", [])])
                         for cat, v in (cfg.get("telegram_chats") or {}).items()
                         if not cat.startswith("_") and isinstance(v, dict)]
        self.always_active = [a.lower() for a in cfg.get("always_active_apps", DEFAULT_ALWAYS_ACTIVE)]

    def match_domain(self, domain: str):
        parts = domain.split(".")
        for i in range(len(parts) - 1 if len(parts) > 1 else 1):
            hit = self.domains.get(".".join(parts[i:]))
            if hit:
                return hit
        return None

    def categorize(self, app: str, title: str, domain: str | None) -> tuple[str, float, str]:
        """Return (category, weight, label). label = site for browsers, else app."""
        a, t = app.lower(), title.lower()
        if domain:
            hit = self.match_domain(domain)
            if hit:
                return hit[0], hit[1], domain
        if "telegram" in a:
            for cat, weight, pats in self.telegram:
                if any(p in t for p in pats):
                    return cat, weight, app
        is_browser = domain is not None or a in self.browser_apps
        if is_browser:
            for kw, (site, cat, weight) in self.title_sites.items():
                if kw in t:
                    return cat, weight, site
        for name, weight, apps, titles in self.categories:
            if any(p in a for p in apps) or any(p in t for p in titles):
                return name, weight, domain or app
        return "uncategorized", 0.0, domain or app


def kind(weight: float) -> str:
    if weight >= PRODUCTIVE_MIN_WEIGHT:
        return "productive"
    return "distracting" if weight < 0 else "neutral"


# ---------------------------------------------------------------- intervals
# Times are POSIX seconds (float). Events are (start, end, data).

def tidy(events: list) -> list:
    """Sort, merge overlapping duplicates, resolve other overlaps, close gaps <= FLOOD_GAP_S.

    Overlapping events with the same data are merged (the Windows AFK watcher
    writes duplicates). With different data, the later event takes over from
    its start. Small gaps are closed like ActivityWatch's flood().
    """
    out: list = []
    for s, t, d in sorted(events, key=lambda e: (e[0], -e[1])):
        if out:
            ps, pt, pd = out[-1]
            if s < pt and d == pd:
                out[-1] = (ps, max(pt, t), pd)
                continue
            if s < pt:
                if s <= ps:
                    out.pop()
                else:
                    out[-1] = (ps, s, pd)
            elif s - pt <= FLOOD_GAP_S:
                out[-1] = (ps, s, pd)
        if t > s:
            out.append((s, t, d))
    return out


def merge(intervals, gap: float = 0.0) -> list:
    out: list[list[float]] = []
    for s, t in sorted(intervals):
        if out and s <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], t)
        else:
            out.append([s, t])
    return [(s, t) for s, t in out]


def intersect(events: list, intervals: list) -> list:
    """Pieces of sorted events that fall inside sorted, merged intervals."""
    out, j = [], 0
    for s, t, d in events:
        while j < len(intervals) and intervals[j][1] <= s:
            j += 1
        k = j
        while k < len(intervals) and intervals[k][0] < t:
            a, b = max(s, intervals[k][0]), min(t, intervals[k][1])
            if b > a:
                out.append((a, b, d))
            k += 1
    return out


def subtract(a: list, b: list) -> list:
    """Parts of merged intervals `a` not covered by merged intervals `b`."""
    out, j = [], 0
    for s, t in a:
        while j < len(b) and b[j][1] <= s:
            j += 1
        cur, k = s, j
        while k < len(b) and b[k][0] < t:
            if b[k][0] > cur:
                out.append((cur, b[k][0]))
            cur = max(cur, b[k][1])
            k += 1
        if cur < t:
            out.append((cur, t))
    return out


def split_by(s: float, t: float, events: list, starts: list) -> list:
    """Cover [s, t) with the sorted, non-overlapping events; gaps get data None."""
    out, cur = [], s
    k = max(bisect.bisect_right(starts, s) - 1, 0)
    while k < len(events) and events[k][0] < t:
        es, et, ed = events[k]
        a, b = max(cur, es), min(t, et)
        if b > a:
            if a > cur:
                out.append((cur, a, None))
            out.append((a, b, ed))
            cur = b
        k += 1
    if cur < t:
        out.append((cur, t, None))
    return out


def total(intervals) -> float:
    return sum(t - s for s, t, *_ in intervals)


# ---------------------------------------------------------------- ActivityWatch

class AW:
    def __init__(self, url: str = AW_URL):
        self.url = url.rstrip("/")

    def get(self, path: str, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        with urllib.request.urlopen(self.url + path + q, timeout=60) as r:
            return json.load(r)

    def events(self, bucket: str, start: float, end: float) -> list:
        raw = self.get(f"/api/0/buckets/{urllib.parse.quote(bucket, safe='')}/events",
                       start=_iso(start), end=_iso(end), limit=-1)
        out = []
        for e in raw:
            s = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp()
            t = s + float(e.get("duration") or 0)
            s, t = max(s, start), min(t, end)
            if t > s:
                out.append((s, t, e.get("data") or {}))
        return out


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat()


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%H:%M")


def _min(seconds: float) -> float:
    return round(seconds / 60, 1)


def coach_day(now: datetime | None = None, day_start_hour: int = DAY_START_HOUR) -> str:
    now = now or datetime.now().astimezone()
    return (now - timedelta(hours=day_start_hour)).date().isoformat()


def day_window(day: str, day_start_hour: int = DAY_START_HOUR) -> tuple[float, float]:
    d = date.fromisoformat(day)
    local_tz = datetime.now().astimezone().tzinfo
    start = datetime(d.year, d.month, d.day, day_start_hour, tzinfo=local_tz)
    # Re-resolve the offset at that moment (handles DST zones when tzdata is present).
    start = start.astimezone()
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    return host[4:] if host.startswith("www.") else host


def fetch(aw: AW, start: float, end: float) -> dict:
    """Pull the raw events needed for one window."""
    buckets = aw.get("/api/0/buckets/")
    try:
        host = aw.get("/api/0/info").get("hostname")
    except Exception:
        host = None

    def pick(prefix: str):
        ids = [b for b in buckets if b.startswith(prefix)]
        own = [b for b in ids if buckets[b].get("hostname") == host]
        return (own or ids or [None])[0]

    win_id, afk_id = pick("aw-watcher-window"), pick("aw-watcher-afk")
    if not win_id:
        raise RuntimeError("no aw-watcher-window bucket")
    web: dict[str, list] = defaultdict(list)
    for b in buckets:
        if b.startswith("aw-watcher-web-"):
            key = b[len("aw-watcher-web-"):].split("_")[0].lower()
            web[key] += aw.events(b, start, end)
    return {
        "window": aw.events(win_id, start, end),
        "afk": aw.events(afk_id, start, end) if afk_id else None,
        "web": dict(web),
    }


# ---------------------------------------------------------------- summary

def summarize(raw: dict, rules: Rules, start: float, end: float, day: str,
              day_start_hour: int = DAY_START_HOUR) -> dict:
    window = tidy(raw["window"])
    web = {k: tidy(v) for k, v in raw["web"].items()}
    web_starts = {k: [e[0] for e in v] for k, v in web.items()}

    def browser_key(app: str):
        a = app.lower()
        for key, names in BROWSER_KEYS.items():
            if a in names:
                return key
        return None

    # 1. Presence: not-afk, plus audible focused browser tab, plus always-active apps.
    afk_filter = raw.get("afk") is not None
    afk = tidy(raw["afk"]) if afk_filter else []
    if afk_filter:
        not_afk = merge([(s, t) for s, t, d in afk if d.get("status") == "not-afk"])
    else:
        not_afk = merge([(s, t) for s, t, _ in window])
    extra = []
    for key, events in web.items():
        focused = merge([(s, t) for s, t, d in window if browser_key(d.get("app", "")) == key])
        audible = [e for e in events if e[2].get("audible")]
        extra += [(a, b) for a, b, _ in intersect(audible, focused)]
    extra += [(s, t) for s, t, d in window
              if any(p in (d.get("app") or "").lower() for p in rules.always_active)]
    present = merge(not_afk + extra)
    # Away = computer on, nobody at it. No data = computer off/asleep or watcher not running.
    if afk_filter:
        away = subtract(merge([(s, t) for s, t, d in afk if d.get("status") == "afk"]), present)
        no_data = subtract([(start, end)], merge([(s, t) for s, t, _ in afk] + present))
    else:
        away, no_data = [], []

    # 2. Active pieces, browser time split by active tab.
    pieces = []  # (s, t, app, label, category, weight, title)
    for s, t, d in intersect(window, present):
        app = d.get("app") or "unknown"
        title = d.get("title") or ""
        key = browser_key(app)
        parts = split_by(s, t, web[key], web_starts[key]) if key in web else [(s, t, None)]
        for a, b, wd in parts:
            dom = _domain(wd.get("url")) if wd else None
            ttl = (wd.get("title") if wd else None) or title
            cat, weight, label = rules.categorize(app, ttl, dom)
            pieces.append((a, b, app, label, cat, weight, ttl))
    pieces.sort(key=lambda p: p[0])

    # 3. Aggregates.
    by_kind = defaultdict(float)
    by_cat: dict[str, list] = {}
    by_app = defaultdict(lambda: defaultdict(float))
    by_site = defaultdict(lambda: defaultdict(float))
    distracting = defaultdict(float)
    uncategorized: dict[tuple, dict] = {}
    for s, t, app, label, cat, weight, ttl in pieces:
        dur = t - s
        k = kind(weight)
        by_kind[k] += dur
        by_cat.setdefault(cat, [0.0, weight])[0] += dur
        by_app[app][cat] += dur
        if label != app:
            by_site[label][cat] += dur
        if k == "distracting":
            distracting[label] += dur
        if cat == "uncategorized":
            u = uncategorized.setdefault((app, label), {"sec": 0.0, "titles": defaultdict(float)})
            u["sec"] += dur
            u["titles"][ttl[:60]] += dur
    active = sum(by_kind.values())

    def top(table: dict, n: int, name: str) -> list:
        rows = []
        for key, cats in sorted(table.items(), key=lambda kv: -sum(kv[1].values()))[:n]:
            sec = sum(cats.values())
            if sec >= 30:
                rows.append({name: key, "min": _min(sec), "category": max(cats, key=cats.get)})
        return rows

    # 4. Timeline slots.
    slot_s = SLOT_MIN * 60
    slots: dict[int, dict] = {}

    def slot_spans(s: float, t: float):
        cur = s
        while cur < t:
            idx = int((cur - start) // slot_s)
            nxt = min(t, start + (idx + 1) * slot_s)
            sl = slots.setdefault(idx, {"active": 0.0, "productive": 0.0, "neutral": 0.0,
                                        "distracting": 0.0, "away": 0.0, "cats": defaultdict(float),
                                        "dist": defaultdict(float)})
            yield sl, nxt - cur
            cur = nxt

    for s, t, app, label, cat, weight, _ in pieces:
        k = kind(weight)
        for sl, dur in slot_spans(s, t):
            sl["active"] += dur
            sl[k] += dur
            sl["cats"][cat] += dur
            if k == "distracting":
                sl["dist"][label] += dur
    for s, t in away:
        for sl, dur in slot_spans(s, t):
            sl["away"] += dur
    slot_rows = []
    for idx in sorted(slots):
        sl = slots[idx]
        if sl["active"] < 30 and sl["away"] < 30:
            continue
        row = {"t": _hm(start + idx * slot_s), "active": _min(sl["active"]),
               "productive": _min(sl["productive"]), "distracting": _min(sl["distracting"]),
               "away": _min(sl["away"])}
        if sl["cats"]:
            row["top"] = max(sl["cats"], key=sl["cats"].get)
        if sl["dist"]:
            row["top_distracting"] = max(sl["dist"], key=sl["dist"].get)
        slot_rows.append(row)

    # 5. Sessions, longest focus run, switches.
    sessions = [{"start": _hm(s), "end": _hm(t), "min": _min(t - s)}
                for s, t in merge([(p[0], p[1]) for p in pieces], gap=SESSION_GAP_S) if t - s >= 60]
    best, run = None, None
    for s, t, *_rest in (p for p in pieces if kind(p[5]) == "productive"):
        if run and s - run[1] <= FOCUS_GAP_S:
            run[1] = t
        else:
            run = [s, t]
        if not best or run[1] - run[0] > best[1] - best[0]:
            best = list(run)
    switches = sum(1 for a, b in zip(pieces, pieces[1:]) if a[2] != b[2])

    return {
        "schema": SCHEMA,
        "day": day,
        "window": {"start": _iso(start), "end": _iso(end), "day_start_hour": day_start_hour},
        "data_quality": {
            "afk_filter": afk_filter,
            "web_watchers": sorted(web),
            "extra_present_min": _min(total(present) - total(not_afk)) if afk_filter else 0.0,
            "last_window_event": _hm(window[-1][1]) if window else None,
        },
        "totals": {
            "active_min": _min(active),
            "productive_min": _min(by_kind["productive"]),
            "neutral_min": _min(by_kind["neutral"]),
            "distracting_min": _min(by_kind["distracting"]),
            "productive_pct": round(100 * by_kind["productive"] / active, 1) if active else 0.0,
            "distracting_pct": round(100 * by_kind["distracting"] / active, 1) if active else 0.0,
            "away_min": _min(total(away)),
            "no_data_min": _min(total(no_data)),
            "first_active": sessions[0]["start"] if sessions else None,
            "last_active": sessions[-1]["end"] if sessions else None,
            "switches_per_hour": round(switches / (active / 3600), 1) if active >= 600 else None,
            "longest_focus": ({"start": _hm(best[0]), "end": _hm(best[1]), "min": _min(best[1] - best[0])}
                              if best else None),
        },
        "categories": [{"category": c, "min": _min(v[0]), "weight": v[1], "kind": kind(v[1])}
                       for c, v in sorted(by_cat.items(), key=lambda kv: -kv[1][0]) if v[0] >= 30],
        "apps": top(by_app, 15, "app"),
        "sites": top(by_site, 15, "site"),
        "distracting": [{"name": n, "min": _min(sec)}
                        for n, sec in sorted(distracting.items(), key=lambda kv: -kv[1])[:10] if sec >= 30],
        "uncategorized": [{"app": app, "label": label, "min": _min(u["sec"]),
                           "sample_title": max(u["titles"], key=u["titles"].get)}
                          for (app, label), u in sorted(uncategorized.items(), key=lambda kv: -kv[1]["sec"])[:10]
                          if u["sec"] >= 60],
        "sessions": sessions,
        "away_periods": [{"start": _hm(s), "end": _hm(t),
                          "min": _min(total(intersect([(s, t, None)], away)))}
                         for s, t in merge(away, gap=FOCUS_GAP_S) if t - s >= 5 * 60],
        "slots": slot_rows,
    }


def build_summary(day: str, cfg: dict, day_start_hour: int = DAY_START_HOUR,
                  aw_url: str = AW_URL, now: datetime | None = None) -> dict:
    start, day_end = day_window(day, day_start_hour)
    end = min(day_end, (now or datetime.now().astimezone()).timestamp())
    if end <= start:
        raise ValueError(f"coach day {day} has not started yet")
    raw = fetch(AW(aw_url), start, end)
    return summarize(raw, Rules(cfg), start, end, day, day_start_hour)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--day", help="coach day YYYY-MM-DD (default: current coach day)")
    ap.add_argument("--config", default=str(HERE / "category_config.windows-zh.json"))
    ap.add_argument("--private", default=str(HERE / "my_categories.json"))
    ap.add_argument("--day-start-hour", type=int, default=DAY_START_HOUR)
    ap.add_argument("--aw-url", default=AW_URL)
    args = ap.parse_args()
    day = args.day or coach_day(day_start_hour=args.day_start_hour)
    summary = build_summary(day, load_config(Path(args.config), Path(args.private)),
                            args.day_start_hour, args.aw_url)
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=1)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
