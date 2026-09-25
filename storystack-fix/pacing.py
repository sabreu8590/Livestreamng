"""Compilation selection and ordering for StoryStack.

Pure functions (no database, no network) so they are easy to test and to plug
into server/recipes.py once the code is on GitHub.

A clip is a dict with at least: id, views, duration (seconds), title.

    pick_livestream(clips, min_views=1_500_000, count=250, exclude_ids=recent)
    order_compilation(clips, pinned_ids=my_top_10, hit_every=4)

Ordering rules (tuned for retention):
  * pinned clips first, in the exact order given (your hand-picked Top 10/25)
  * then a strong opener: the biggest clips go first so viewers stay
  * a "big hit" (top 20% by views) roughly every `hit_every` clips
  * never two long clips back to back, never two clips with near-identical titles
  * one big hit saved for the very end
  * everything else shuffled, reproducible with `seed`
"""
import random
import re

LONG_SECONDS = 90


def _words(title):
    return set(w for w in re.findall(r"[a-z0-9']+", (title or "").lower()) if len(w) > 3)


def _similar(a, b):
    wa, wb = _words(a.get("title")), _words(b.get("title"))
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.6


def _ok_after(prev, clip):
    if prev is None:
        return True
    if prev.get("duration", 0) > LONG_SECONDS and clip.get("duration", 0) > LONG_SECONDS:
        return False
    return not _similar(prev, clip)


def _take(pool, prev):
    """Pop the first clip in pool that fits after prev (or the first one)."""
    for i, clip in enumerate(pool):
        if _ok_after(prev, clip):
            return pool.pop(i)
    return pool.pop(0)


def pick_livestream(clips, min_views=1_000_000, count=250, exclude_ids=(), seed=None):
    """Random clips above a view threshold, skipping ones used recently."""
    rng = random.Random(seed)
    excluded = set(exclude_ids)
    pool = [c for c in clips if (c.get("views") or 0) >= min_views and c["id"] not in excluded]
    if len(pool) < count:  # not enough fresh clips: allow recently used ones again
        pool += [c for c in clips if (c.get("views") or 0) >= min_views and c["id"] in excluded]
    rng.shuffle(pool)
    return pool[:count]


def order_compilation(clips, pinned_ids=(), hit_every=4, opener=2, save_closer=True,
                      hit_share=0.2, seed=None):
    rng = random.Random(seed)
    by_id = {c["id"]: c for c in clips}
    out = [by_id[i] for i in pinned_ids if i in by_id]
    rest = [c for c in clips if c["id"] not in set(pinned_ids)]
    if not rest:
        return out

    rest.sort(key=lambda c: c.get("views") or 0, reverse=True)
    n_hits = max(1, round(len(rest) * hit_share))
    hits, others = rest[:n_hits], rest[n_hits:]
    closer = hits.pop() if save_closer and len(hits) > 1 else None

    if not out:  # no pinned clips: open with the biggest ones
        for _ in range(min(opener, len(hits))):
            out.append(_take(hits, out[-1] if out else None))
    rng.shuffle(hits)
    rng.shuffle(others)

    since_hit = 0
    while hits or others:
        prev = out[-1] if out else None
        # at least hit_every apart; spread evenly when there aren't enough hits
        gap = max(hit_every - 1, len(others) // len(hits)) if hits else 0
        if hits and (since_hit >= gap or not others):
            out.append(_take(hits, prev))
            since_hit = 0
        else:
            out.append(_take(others, prev))
            since_hit += 1
    if closer:
        out.append(closer)
    return out
