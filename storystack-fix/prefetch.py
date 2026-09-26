#!/usr/bin/env python3
"""Pre-download clips so compilation builds skip the download step.

Downloads several clips at once, each worker on its own sticky proxy IP,
highest-view clips first, with a nightly data budget and a disk guard.
Builds reuse anything already downloaded (videos.local_path).

  prefetch.py                         # defaults: >=1M views, Shorts only, 5 GB budget
  prefetch.py --min-views 0 --max-gb 30   # whole library
  prefetch.py --status                # show progress / last run

Progress is written to /var/lib/storystack/prefetch_status.json.
"""
import argparse, fcntl, json, multiprocessing as mp, os, shutil, sqlite3, sys, time
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED

SERVER = os.environ.get("STORYSTACK_SERVER", "/opt/storystack/server")
DB = os.environ.get("STORYSTACK_DB", "/var/lib/storystack/storystack.db")
DL = os.environ.get("STORYSTACK_DOWNLOADS", "/var/lib/storystack/downloads")
STATUS = os.environ.get("STORYSTACK_PREFETCH_STATUS", "/var/lib/storystack/prefetch_status.json")
LOCK = STATUS + ".lock"
sys.path.insert(0, SERVER)


def pending(min_views, max_seconds, channel, limit):
    c = sqlite3.connect(DB, timeout=30)
    q = ("SELECT id, title, COALESCE(view_count, 0), local_path FROM videos "
         "WHERE COALESCE(view_count, 0) >= ? AND duration > 0 AND duration <= ?")
    p = [min_views, max_seconds]
    if channel:
        q += " AND channel_id = ?"
        p.append(channel)
    q += " ORDER BY view_count DESC"
    out = []
    for vid, title, views, lp in c.execute(q, p):
        if lp and os.path.exists(lp) and os.path.getsize(lp) > 0:
            continue
        out.append((vid, title or vid, views))
        if limit and len(out) >= limit:
            break
    return out


_slots = None

def _init(slots):
    """Each worker process gets its own sticky proxy port."""
    slot = slots.get()
    import db, providers
    db.init(DB)
    orig = getattr(providers, "_orig_proxy", None)
    base = orig() if orig else None
    if base and hasattr(providers, "_v3_with_port"):
        mine = providers._v3_with_port(base, 10010 + slot * 10)
        providers._orig_proxy = lambda: mine


def _fetch(vid):
    import providers
    t = time.time()
    path = providers.download(vid, DL)
    return str(path), os.path.getsize(path), time.time() - t


def write_status(st):
    tmp = STATUS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATUS)


def show_status():
    try:
        st = json.load(open(STATUS))
    except FileNotFoundError:
        print("no prefetch has run yet")
        return
    print(f"{st['state']}: {st['done']}/{st['total']} downloaded, {st['failed']} failed, "
          f"{st['gb']:.2f} GB (~${st['est_cost_usd']:.2f}), "
          f"{st['rate_per_min']:.1f}/min, ~{st['eta_min']:.0f} min left")
    for e in st.get("errors", [])[-5:]:
        print(f"  failed: {e['title'][:50]} -- {e['error'][:120]}")


def encode_bodies(a, st):
    """Pre-encode the unlabeled clip bodies so the next builds only encode labels.

    Uses the shape and fill of the most recent build, so the cache matches the
    compilations actually being made. Clips already in the cache are skipped.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(SERVER), "bin"))
    import storystack as engine
    from concurrent.futures import ThreadPoolExecutor as Pool
    c = sqlite3.connect(DB, timeout=30)
    row = c.execute("SELECT config_json FROM builds ORDER BY id DESC LIMIT 1").fetchone()
    cfg = engine.apply_aspect(engine.deep_merge(engine.DEFAULT_CONFIG,
                                                json.loads(row[0]) if row and row[0] else {}))
    bodies = os.path.join(os.path.dirname(DB), "cache", "bodies")
    os.makedirs(bodies, exist_ok=True)
    q = ("SELECT id, title, local_path FROM videos WHERE COALESCE(view_count, 0) >= ? "
         "AND duration > 0 AND duration <= ? AND local_path IS NOT NULL AND local_path != '' "
         "ORDER BY view_count DESC LIMIT ?")
    todo = [(vid, t, lp) for vid, t, lp in c.execute(q, (a.min_views, a.max_seconds, a.encode))
            if os.path.exists(lp)]
    t0, done, fresh = time.time(), 0, 0
    st["encode"] = {"total": len(todo), "done": 0, "new": 0}
    print(f"pre-encoding up to {len(todo)} clips for fast builds "
          f"({cfg['video']['width']}x{cfg['video']['height']}, {cfg['video'].get('fill')})",
          flush=True)

    def one(args):
        vid, title, lp = args
        item = {"path": engine.Path(lp), "trim_start": 0, "trim_end": 0}
        _, _, cached = engine.prepare_body(item, cfg, bodies)
        return title, cached

    with Pool(max(1, a.encode_workers)) as ex:
        for title, cached in ex.map(one, todo):
            done += 1
            fresh += 0 if cached else 1
            st["encode"].update(done=done, new=fresh)
            if not cached:
                print(f"  encoded {title[:60]}", flush=True)
            if done % 10 == 0:
                write_status(st)
    print(f"pre-encode done: {fresh} new, {done - fresh} already cached, "
          f"{(time.time() - t0) / 60:.1f} min", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-views", type=int, default=1_000_000)
    ap.add_argument("--max-seconds", type=int, default=240, help="skip long videos")
    ap.add_argument("--channel")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--max-gb", type=float, default=5.0, help="data budget for this run")
    ap.add_argument("--min-free-gb", type=float, default=40.0, help="stop if disk gets this full")
    ap.add_argument("--usd-per-gb", type=float, default=1.0)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--encode", type=int, default=0, metavar="N",
                    help="after downloading, pre-encode the top N clips for fast builds")
    ap.add_argument("--encode-workers", type=int, default=4)
    a = ap.parse_args()
    if a.status:
        return show_status()

    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("a prefetch is already running")

    os.makedirs(DL, exist_ok=True)
    todo = pending(a.min_views, a.max_seconds, a.channel, a.limit)
    t0 = time.time()
    st = {"state": "running", "started": t0, "total": len(todo), "done": 0, "failed": 0,
          "gb": 0.0, "est_cost_usd": 0.0, "rate_per_min": 0.0, "eta_min": 0.0, "errors": [],
          "filters": {"min_views": a.min_views, "max_seconds": a.max_seconds}}
    write_status(st)
    print(f"prefetch: {len(todo)} clips to download (>= {a.min_views:,} views)", flush=True)
    if not todo:
        if a.encode:
            encode_bodies(a, st)
        st["state"] = "done"; write_status(st); return

    c = sqlite3.connect(DB, timeout=30)
    slots = mp.get_context("spawn").Queue()
    for i in range(a.workers):
        slots.put(i)
    queue, inflight, stop = list(todo), {}, None
    with ProcessPoolExecutor(a.workers, mp_context=mp.get_context("spawn"),
                             initializer=_init, initargs=(slots,)) as ex:
        while queue or inflight:
            while queue and not stop and len(inflight) < a.workers * 2:
                vid, title, views = queue.pop(0)
                inflight[ex.submit(_fetch, vid)] = (vid, title)
            if not inflight:
                break
            finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in finished:
                vid, title = inflight.pop(fut)
                try:
                    path, size, secs = fut.result()
                    c.execute("UPDATE videos SET local_path = ? WHERE id = ?", (path, vid))
                    c.commit()
                    st["done"] += 1
                    st["gb"] += size / 1e9
                    print(f"  ok   {title[:60]} ({size / 1e6:.1f} MB, {secs:.0f}s)", flush=True)
                except Exception as exc:  # noqa: BLE001
                    st["failed"] += 1
                    st["errors"].append({"id": vid, "title": title, "error": str(exc)[:300]})
                    st["errors"] = st["errors"][-50:]
                    print(f"  FAIL {title[:60]}: {str(exc)[:160]}", flush=True)
                    if "TRAFFIC_EXHAUSTED" in str(exc) or "407" in str(exc):
                        stop = "proxy traffic used up or proxy login rejected (HTTP 407)"
                        queue.clear()
            mins = max((time.time() - t0) / 60, 1e-6)
            finished_n = st["done"] + st["failed"]
            st["rate_per_min"] = finished_n / mins
            st["eta_min"] = (st["total"] - finished_n) / st["rate_per_min"] if st["rate_per_min"] else 0
            st["est_cost_usd"] = st["gb"] * a.usd_per_gb
            if not stop and st["gb"] >= a.max_gb:
                stop = f"data budget reached ({a.max_gb} GB)"
            if not stop and shutil.disk_usage(DL).free / 1e9 < a.min_free_gb:
                stop = f"disk nearly full (< {a.min_free_gb} GB free)"
            if stop:
                st["stopped_because"] = stop
            write_status(st)
    if a.encode:
        encode_bodies(a, st)
    st["state"] = "done" if not stop else "paused"
    st["finished"] = time.time()
    write_status(st)
    print(f"prefetch {st['state']}: {st['done']} ok, {st['failed']} failed, "
          f"{st['gb']:.2f} GB (~${st['est_cost_usd']:.2f}) in {(time.time() - t0) / 60:.1f} min"
          + (f" -- {stop}" if stop else ""), flush=True)


if __name__ == "__main__":
    main()
