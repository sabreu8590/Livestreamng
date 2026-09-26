"""Saved rules that pick and arrange clips on their own.

A recipe answers three questions without anyone clicking a grid:
  which clips qualify   (filters: views, month, how often already used)
  which of those to take (all, the strongest, a random draw, fill N hours)
  what order they go in  (and this is the part that matters for retention)

The ordering worth caring about is "spread": strongest clip first so the hook
lands, second strongest last so the loop point is strong, and the rest of the
top performers spaced evenly through the middle so a three hour compilation
never sags for twenty minutes at a stretch.

Everything here is a pure function over a list of clip dicts, so it can be
tested without a database or a video file.
"""

import calendar
import hashlib
import re
import math
import random
import time
from datetime import date, timedelta

DEFAULT_RULES = {
    # --- which clips qualify
    "month": "",            # "" | "last_month" | "this_month" | "2026-09"
    "date_from": "",
    "date_to": "",
    "min_views": 0,
    "max_views": 0,         # 0 = no ceiling
    "type": "only",         # only = shorts only, exclude = long form, "" = both
    "max_used": None,       # None = any, 0 = never used, 2 = used twice or less
    "min_duration": 0,
    "max_duration": 0,
    "search": "",

    # --- how many to take
    "select": "newest",     # newest | oldest | top_views | random
    "limit": 0,             # max clips, 0 = no cap
    "target_hours": 0,      # fill to roughly this runtime, 0 = ignore

    # --- what order they end up in
    "order": "spread",      # spread | views_desc | date_desc | date_asc | random | as_selected
    "anchor_every": 5,      # spread: one top performer every N clips
    "seed": 0,              # 0 = new shuffle each run, anything else = reproducible

    # --- trim the same boilerplate off every clip
    "trim_start": 0,        # seconds cut from the head of each clip
    "trim_end": 0,          # seconds cut from the tail
}


def merge_rules(rules):
    out = dict(DEFAULT_RULES)
    out.update({k: v for k, v in (rules or {}).items() if k in DEFAULT_RULES})
    return out


# --------------------------------------------------------------- date windows

DAYS_BACK = re.compile(r"^last_(\d{1,3})_days$")


def resolve_window(rules, today=None):
    """Turn a window setting into a concrete (from, to) date pair.

    Accepts a month ("last_month", "this_month", "2026-09") or a day window
    ("today", "yesterday", "last_7_days"), because a daily compilation of what
    a channel posted in the past day is as reasonable an ask as a monthly one.
    """
    today = today or date.today()
    month = str(rules.get("month") or "").strip()

    if month == "today":
        return today.isoformat(), today.isoformat()

    if month == "yesterday":
        day = today - timedelta(days=1)
        return day.isoformat(), day.isoformat()

    m = DAYS_BACK.match(month)
    if m:
        span = max(1, min(365, int(m.group(1))))
        return (today - timedelta(days=span - 1)).isoformat(), today.isoformat()

    if month == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        start = last_prev.replace(day=1)
        return start.isoformat(), last_prev.isoformat()

    if month == "this_month":
        start = today.replace(day=1)
        end_day = calendar.monthrange(today.year, today.month)[1]
        return start.isoformat(), today.replace(day=end_day).isoformat()

    if len(month) == 7 and month[4] == "-":
        try:
            year, mon = int(month[:4]), int(month[5:])
            end_day = calendar.monthrange(year, mon)[1]
            return f"{month}-01", f"{month}-{end_day:02d}"
        except ValueError:
            pass

    return (str(rules.get("date_from") or ""), str(rules.get("date_to") or ""))


# -------------------------------------------------------------------- filters

def qualifies(clip, rules, window):
    start, end = window
    date_str = clip.get("upload_date") or ""

    if start and (not date_str or date_str < start):
        return False
    if end and (not date_str or date_str > end):
        return False

    views = clip.get("view_count")
    min_views = int(rules.get("min_views") or 0)
    max_views = int(rules.get("max_views") or 0)
    if min_views:
        # A clip with unknown views cannot be proven to clear the bar.
        if views is None or views < min_views:
            return False
    if max_views and views is not None and views > max_views:
        return False

    kind = rules.get("type")
    is_short = bool(clip.get("is_short"))
    if kind == "only" and not is_short:
        return False
    if kind == "exclude" and is_short:
        return False

    max_used = rules.get("max_used")
    if max_used is not None and int(clip.get("used_count") or 0) > int(max_used):
        return False

    duration = float(clip.get("duration") or 0)
    if rules.get("min_duration") and duration < float(rules["min_duration"]):
        return False
    if rules.get("max_duration") and duration > float(rules["max_duration"]):
        return False

    search = str(rules.get("search") or "").strip().lower()
    if search and search not in str(clip.get("title") or "").lower():
        return False

    return True


def filter_clips(clips, rules, today=None):
    window = resolve_window(rules, today)
    return [c for c in clips if qualifies(c, rules, window)]


# ------------------------------------------------------------------ selection

def _rng(rules):
    seed = int(rules.get("seed") or 0)
    if seed:
        return random.Random(seed)
    return random.Random(int(time.time() * 1000) ^ random.getrandbits(32))


def _views(clip):
    value = clip.get("view_count")
    return -1 if value is None else int(value)


def select_clips(clips, rules, today=None):
    """Narrow the qualifying pool down to what actually goes in."""
    pool = list(clips)
    mode = rules.get("select") or "newest"

    if mode == "top_views":
        pool.sort(key=lambda c: (-_views(c), c.get("upload_date") or ""))
    elif mode == "oldest":
        pool.sort(key=lambda c: (c.get("upload_date") or "", c.get("id") or ""))
    elif mode == "random":
        _rng(rules).shuffle(pool)
    else:  # newest
        pool.sort(key=lambda c: (c.get("upload_date") or "", c.get("id") or ""),
                  reverse=True)

    limit = int(rules.get("limit") or 0)
    if limit > 0:
        pool = pool[:limit]

    target_hours = float(rules.get("target_hours") or 0)
    if target_hours > 0:
        budget = target_hours * 3600.0
        kept, running = [], 0.0
        for clip in pool:
            duration = float(clip.get("duration") or 0)
            if duration <= 0:
                continue
            if kept and running + duration > budget:
                break
            kept.append(clip)
            running += duration
        pool = kept

    return pool


# ------------------------------------------------------------------- ordering

def spread_order(clips, anchor_every=5, rng=None):
    """Distribute the strongest clips evenly instead of front-loading them.

    Strongest opens, second strongest closes (the loop point on a 24/7 stream
    is a real edge, not a throwaway), and the remaining anchors land at even
    intervals with everything else shuffled into the gaps.
    """
    n = len(clips)
    if n <= 2:
        return sorted(clips, key=lambda c: -_views(c))

    rng = rng or random.Random()
    anchor_every = max(2, int(anchor_every or 5))

    ranked = sorted(clips, key=lambda c: (-_views(c), c.get("id") or ""))
    # K anchors spread across positions 0..n-1 leave gaps of (n-1)/(K-1).
    # For a strong clip at least every `anchor_every` slots, that gap must not
    # exceed anchor_every, which needs one more anchor than ceil(n/every).
    anchor_count = max(2, min(n, math.ceil((n - 1) / anchor_every) + 1))
    anchors, fill = ranked[:anchor_count], ranked[anchor_count:]

    # Even positions across the whole run, always including first and last.
    positions = [round(i * (n - 1) / (anchor_count - 1))
                 for i in range(anchor_count)]
    positions = sorted(set(positions))
    while len(positions) < anchor_count:  # collisions on very short runs
        for candidate in range(n):
            if candidate not in positions:
                positions.append(candidate)
                break
        positions = sorted(set(positions))
    positions = positions[:anchor_count]

    slots = [None] * n
    # Best clip opens. Second best closes. The rest fan out through the middle.
    slots[positions[0]] = anchors[0]
    if anchor_count > 1:
        slots[positions[-1]] = anchors[1]
    middle = anchors[2:]
    for pos, clip in zip(positions[1:-1], middle):
        slots[pos] = clip
    leftover_anchors = middle[max(0, len(positions) - 2):]

    rest = fill + leftover_anchors
    rng.shuffle(rest)
    it = iter(rest)
    for i in range(n):
        if slots[i] is None:
            slots[i] = next(it, None)

    return [c for c in slots if c is not None]


def order_clips(clips, rules):
    mode = rules.get("order") or "spread"
    if mode == "as_selected":
        return list(clips)
    if mode == "views_desc":
        return sorted(clips, key=lambda c: -_views(c))
    if mode == "date_desc":
        return sorted(clips, key=lambda c: c.get("upload_date") or "", reverse=True)
    if mode == "date_asc":
        return sorted(clips, key=lambda c: c.get("upload_date") or "")
    if mode == "random":
        out = list(clips)
        _rng(rules).shuffle(out)
        return out
    return spread_order(clips, rules.get("anchor_every"), _rng(rules))


# ------------------------------------------------------------------ the whole

def apply_recipe(clips, rules, today=None):
    """Run the full pipeline and report what happened at each step."""
    rules = merge_rules(rules)
    window = resolve_window(rules, today)
    qualified = filter_clips(clips, rules, today)
    selected = select_clips(qualified, rules, today)
    ordered = order_clips(selected, rules)
    runtime = sum(float(c.get("duration") or 0) for c in ordered)
    return {
        "window": {"from": window[0], "to": window[1]},
        "pool": len(clips),
        "qualified": len(qualified),
        "selected": len(ordered),
        "runtime": runtime,
        "clips": ordered,
    }


# ------------------------------------------------------------------ schedules

def next_run_at(schedule, day, hour, after=None):
    """When a recipe should fire next. Returns a unix timestamp, or None."""
    if schedule not in ("monthly", "weekly", "daily"):
        return None
    after = after or time.time()
    base = time.localtime(after)
    hour = max(0, min(23, int(hour if hour is not None else 4)))

    if schedule == "daily":
        candidate = _stamp(base.tm_year, base.tm_mon, base.tm_mday, hour)
        return candidate if candidate > after else candidate + 86400

    if schedule == "weekly":
        want = max(0, min(6, int(day if day is not None else 0)))
        for ahead in range(0, 8):
            probe = time.localtime(after + ahead * 86400)
            if probe.tm_wday == want:
                candidate = _stamp(probe.tm_year, probe.tm_mon, probe.tm_mday, hour)
                if candidate > after:
                    return candidate
        return after + 7 * 86400

    # monthly
    want_day = max(1, min(28, int(day if day is not None else 1)))
    year, month = base.tm_year, base.tm_mon
    candidate = _stamp(year, month, want_day, hour)
    if candidate <= after:
        month += 1
        if month > 12:
            month, year = 1, year + 1
        candidate = _stamp(year, month, want_day, hour)
    return candidate


def _stamp(year, month, day, hour):
    return time.mktime((year, month, day, hour, 0, 0, 0, 0, -1))


def slug(text, fallback="compilation"):
    out = "".join(c if c.isalnum() or c in "-_" else "-"
                  for c in str(text or "")).strip("-").lower()
    while "--" in out:
        out = out.replace("--", "-")
    return out or fallback


def build_name(recipe_name, rules, today=None, channel=None):
    """A predictable, human filename for a build.

    Day windows get a full date, month windows get the month, so a daily run
    and a monthly run never collide on the same filename.
    """
    today = today or date.today()
    window_from, window_to = resolve_window(rules, today)
    month = str(rules.get("month") or "").strip()
    day_window = (month in ("today", "yesterday")
                  or DAYS_BACK.match(month) is not None)
    if window_from:
        stamp = window_from if day_window else window_from[:7]
    else:
        stamp = today.isoformat()
    parts = [slug(recipe_name)]
    if channel:
        parts.append(slug(channel, ""))
    parts.append(stamp)
    return "-".join(p for p in parts if p)
