"""Background workers for channel syncs and compilation builds.

Builds are long running, so they go on a single worker thread and report
progress into SQLite. The web layer only ever reads that progress, which keeps
the UI responsive and means a page refresh mid-build loses nothing.
"""

import json
import os
import queue
import shutil
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import db
import providers
import recipes as rules

# Reuse the tested render/concat engine rather than reimplementing it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import storystack as engine  # noqa: E402

_build_queue = queue.Queue()
_worker = None
_paths = {}


def configure(data_dir):
    data_dir = Path(data_dir)
    _paths["data"] = data_dir
    _paths["downloads"] = data_dir / "downloads"
    _paths["thumbs"] = data_dir / "thumbs"
    _paths["work"] = data_dir / "work"
    _paths["out"] = data_dir / "output"
    _paths["cache"] = data_dir / "cache" / "bodies"
    for key in ("downloads", "thumbs", "work", "out", "cache"):
        _paths[key].mkdir(parents=True, exist_ok=True)


def paths():
    if not _paths:
        raise RuntimeError("jobs.configure() was never called")
    return _paths


def start_worker():
    global _worker
    if _worker and _worker.is_alive():
        return
    _worker = threading.Thread(target=_build_loop, name="build-worker", daemon=True)
    _worker.start()
    # Anything left running when the process died cannot still be running.
    db.execute("UPDATE builds SET status='failed', error='interrupted by restart' "
               "WHERE status IN ('running','queued')")


# ------------------------------------------------------------------- sync

def sync_channel(channel_id, *, include_long=True):
    """Run in a thread: refresh one channel's clip list."""
    ch = db.one("SELECT * FROM channels WHERE id = ?", (channel_id,))
    if not ch:
        return

    def note(msg):
        db.execute("UPDATE channels SET sync_message = ? WHERE id = ?", (msg, channel_id))

    db.execute("UPDATE channels SET sync_state='syncing', sync_message='starting' "
               "WHERE id = ?", (channel_id,))
    try:
        if ch["kind"] == "local":
            result = providers.list_local(ch["url"])
            for entry in result["entries"]:
                entry.setdefault("local_path", entry.get("url"))
        else:
            result = providers.list_channel(ch["url"], include_long=include_long,
                                            progress=note)

        count = db.upsert_videos(channel_id, result["entries"])

        # Local files are on disk already, so record where and make thumbnails.
        if ch["kind"] == "local":
            for entry in result["entries"]:
                db.execute("UPDATE videos SET local_path = ? WHERE id = ?",
                           (entry.get("local_path"), entry["id"]))
            _thumbnail_local(result["entries"], note)

        db.execute(
            "UPDATE channels SET title = COALESCE(?, title), last_synced = ?, "
            "sync_state='idle', sync_message = ? WHERE id = ?",
            (result.get("title"), time.time(), f"{count} clips", channel_id))

        # The listing gave us ids and titles; go get the durations and dates.
        if ch["kind"] != "local":
            threading.Thread(target=hydrate_channel, args=(channel_id,),
                             daemon=True).start()
    except Exception as exc:  # noqa: BLE001 - surfaced in the UI
        db.execute("UPDATE channels SET sync_state='error', sync_message = ? "
                   "WHERE id = ?", (str(exc)[:500], channel_id))


def _thumbnail_local(entries, note):
    thumbs = paths()["thumbs"]
    todo = [e for e in entries
            if e.get("local_path") and not (thumbs / f"{_safe(e['id'])}.jpg").is_file()]
    for i, entry in enumerate(todo, 1):
        out = thumbs / f"{_safe(entry['id'])}.jpg"
        providers.make_thumbnail(entry["local_path"], out)
        if out.is_file():
            db.execute("UPDATE videos SET thumb = ? WHERE id = ?",
                       (f"/thumbs/{out.name}", entry["id"]))
        if i % 10 == 0:
            note(f"thumbnails {i}/{len(todo)}")


# ---------------------------------------------------------------- hydration
#
# A flat channel listing is fast but returns no duration and no upload date,
# which is why the month filter comes up empty and every clip reads 0:00.
# This fills those in afterwards: one API call per 50 videos when a key is
# set, otherwise one yt-dlp lookup per video, newest first so the useful end
# of the library becomes usable immediately.

_hydrating = set()


def needs_detail(channel_id, limit=None):
    sql = ("SELECT id FROM videos WHERE channel_id = ? "
           "AND (duration IS NULL OR duration <= 0 OR upload_date IS NULL "
           "     OR upload_date = '') ORDER BY rowid DESC")
    params = [channel_id]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [r["id"] for r in db.query(sql, tuple(params))]


def hydrate_channel(channel_id):
    """Fill in missing durations and dates. Safe to run repeatedly."""
    if channel_id in _hydrating:
        return
    _hydrating.add(channel_id)

    def note(msg):
        db.execute("UPDATE channels SET sync_message = ? WHERE id = ?",
                   (msg, channel_id))

    try:
        pending = needs_detail(channel_id)
        if not pending:
            note("details complete")
            return

        key = providers.api_key()
        total, done = len(pending), 0
        _t0 = time.time()

        if key:
            note(f"filling in details for {total} clips")
            for i in range(0, len(pending), 50):
                chunk = pending[i:i + 50]
                try:
                    details = providers.fetch_details_api(chunk, key)
                except Exception as exc:  # noqa: BLE001
                    note(f"detail lookup failed: {str(exc)[:120]}")
                    key = None      # fall through to yt-dlp for the rest
                    pending = pending[i:]
                    break
                _apply_details(details)
                done += len(chunk)
                note(f"details {done}/{total}{_fill_rate(_t0, done, total)}")
            else:
                note("details complete")
                return

        # No key, or the key gave out. One lookup per video, and slow enough
        # not to get the server rate limited.
        note(f"filling in details for {len(pending)} clips (no API key, this is slow)")
        for n, vid in enumerate(pending, 1):
            try:
                _apply_details({vid: providers.fetch_details_ytdlp(vid)})
            except Exception:  # noqa: BLE001
                db.execute("UPDATE videos SET duration = -1 WHERE id = ? "
                           "AND (duration IS NULL OR duration = 0)", (vid,))
            if n % 10 == 0:
                note(f"details {n}/{len(pending)}{_fill_rate(_t0, n, len(pending))} (add an API key to make this instant)")
            time.sleep(0.4)
        note("details complete")
    finally:
        _hydrating.discard(channel_id)


def _apply_details(details):
    rows = []
    for vid, d in details.items():
        rows.append((float(d.get("duration") or 0),
                     d.get("upload_date"),
                     d.get("view_count"),
                     1 if 0 < float(d.get("duration") or 0) <= providers.SHORT_MAX_SECONDS else 0,
                     vid))
    if rows:
        db.executemany(
            "UPDATE videos SET duration = ?, upload_date = COALESCE(?, upload_date), "
            "view_count = COALESCE(?, view_count), is_short = ? WHERE id = ?", rows)


def _safe(video_id):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(video_id))


# ------------------------------------------------------------------ builds

def build_config(body, safe_name, out_dir=None):
    """Assemble an engine config from whatever the caller supplied."""
    label = body.get("label") or {}
    video = body.get("video") or {}
    audio = body.get("audio") or {}
    output = body.get("output") or {}
    out_path = output.get("path") or str((out_dir or paths()["out"]) / f"{safe_name}.mp4")
    return {
        "channel": safe_name,
        "label": {k: v for k, v in label.items() if v is not None},
        "video": {k: v for k, v in video.items() if v is not None},
        "audio": {k: v for k, v in audio.items() if v is not None},
        "build": {"jobs": int(body.get("jobs") or 0) or (os.cpu_count() or 2),
                  "keep_intermediates": True,
                  # Fast builds: each clip is encoded once without a label and
                  # cached; a build only re-encodes the labeled first seconds.
                  "fast": body.get("fast", True) is not False,
                  "cache_dir": str(paths()["cache"]),
                  # A test build never counts towards "used".
                  "test": bool(body.get("test")),
                  # Clips that may stand in for any clip that fails to download.
                  "spares": [str(v) for v in (body.get("spares") or [])][:200]},
        "trim": body.get("trim") or {},
        "output": {"path": out_path, "write_chapters": True,
                   "write_manifest": True, "embed_chapters": True,
                   "post_command": output.get("post_command") or None},
    }


def slugify(name):
    return ("".join(c if c.isalnum() or c in "-_" else "-"
                    for c in (name or "compilation")).strip("-").lower()
            or "compilation")


def create_build(name, video_ids, cfg, *, titles=None, trims=None,
                 recipe_id=None, enqueue=True):
    """Record a build and its running order, then hand it to the worker."""
    if not video_ids:
        raise ValueError("a build needs at least one clip")
    titles = titles or {}
    trims = trims or {}
    default_trim = (cfg.get("trim") or {})
    head = float(default_trim.get("start") or 0)
    tail = float(default_trim.get("end") or 0)
    cur = db.execute(
        "INSERT INTO builds(name, status, created_at, total_clips, config_json, "
        "recipe_id) VALUES(?,?,?,?,?,?)",
        (name, "queued", time.time(), len(video_ids), json.dumps(cfg), recipe_id))
    build_id = cur.lastrowid

    template = (cfg.get("label") or {}).get("template") or "Story {n}"
    rows = []
    for position, vid in enumerate(video_ids):
        try:
            label = template.format(n=position + 1, total=len(video_ids),
                                    index=position + 1,
                                    title=titles.get(vid, ""), stem="")
        except (KeyError, IndexError):
            label = f"Story {position + 1}"
        per = trims.get(vid) or {}
        rows.append((build_id, position, vid, label, titles.get(vid, ""),
                     float(per.get("start", head) or 0),
                     float(per.get("end", tail) or 0)))
    db.executemany(
        "INSERT INTO build_items(build_id, position, video_id, label, title, "
        "trim_start, trim_end) VALUES(?,?,?,?,?,?,?)", rows)

    if enqueue:
        enqueue_build(build_id)
    return build_id


# ----------------------------------------------------------------- recipes

def channel_clips(channel_id):
    return db.query("SELECT * FROM videos WHERE channel_id = ?", (channel_id,))


def preview_recipe(channel_id, rule_dict):
    """What this rule would pick right now. Used by the UI, changes nothing."""
    return rules.apply_recipe(channel_clips(channel_id), rule_dict)


def recipe_channels(recipe):
    """Every channel a recipe targets. Falls back to its single legacy column."""
    raw = recipe.get("channels_json")
    if raw:
        try:
            ids = [c for c in json.loads(raw) if c]
            if ids:
                return ids
        except (json.JSONDecodeError, TypeError):
            pass
    return [recipe["channel_id"]] if recipe.get("channel_id") else []


def _channel_output(output, channel_name, channel_slug, many):
    """Give each channel its own destination so builds cannot overwrite each other."""
    out = dict(output or {})
    path = str(out.get("path") or "").strip()
    if path:
        if "{channel}" in path:
            path = path.replace("{channel}", channel_slug)
        elif many:
            base, dot, ext = path.rpartition(".")
            path = f"{base}-{channel_slug}{dot}{ext}" if dot else f"{path}-{channel_slug}"
        out["path"] = path
    post = str(out.get("post_command") or "").strip()
    if post:
        out["post_command"] = (post.replace("{channel}", channel_slug)
                                   .replace("{channel_name}", channel_name))
    return out


def run_recipe(recipe_id, *, manual=False, test=False):
    """Turn a recipe into one queued build per channel it targets."""
    recipe = db.one("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
    if not recipe:
        raise ValueError(f"no recipe {recipe_id}")

    channel_ids = recipe_channels(recipe)
    if not channel_ids:
        raise ValueError("this recipe has no channels")

    rule_dict = rules.merge_rules(json.loads(recipe["rules_json"] or "{}"))
    label = json.loads(recipe["label_json"] or "{}")
    video = json.loads(recipe.get("video_json") or "{}") if "video_json" in recipe else {}
    output = json.loads(recipe["output_json"] or "{}")
    many = len(channel_ids) > 1

    built, skipped = [], []
    for channel_id in channel_ids:
        channel = db.one("SELECT * FROM channels WHERE id = ?", (channel_id,))
        channel_name = (channel or {}).get("title") or channel_id
        result = rules.apply_recipe(channel_clips(channel_id), rule_dict)
        clips = result["clips"]
        if not clips:
            skipped.append(channel_name)
            continue

        name = rules.build_name(recipe["name"], rule_dict,
                                channel=channel_name if many else None)
        cfg = build_config({"label": label, "video": video,
                            "output": _channel_output(output, channel_name,
                                                      rules.slug(channel_name),
                                                      many)},
                           slugify(name))
        cfg["build"]["test"] = bool(test)
        cfg["trim"] = {"start": float(rule_dict.get("trim_start") or 0),
                       "end": float(rule_dict.get("trim_end") or 0)}
        chosen = {c["id"] for c in clips}
        pool = rules.filter_clips(channel_clips(channel_id), rule_dict)
        pool.sort(key=lambda c: -(c.get("view_count") or 0))
        cfg["build"]["spares"] = [c["id"] for c in pool if c["id"] not in chosen][:200]
        titles = {c["id"]: (c.get("title") or "") for c in clips}
        titles.update({c["id"]: (c.get("title") or "") for c in pool[:400]})
        build_id = create_build(name, [c["id"] for c in clips], cfg,
                                titles=titles, recipe_id=recipe_id)
        db.execute("UPDATE builds SET channel_id = ? WHERE id = ?",
                   (channel_id, build_id))
        built.append({"build_id": build_id, "channel": channel_name,
                      "clips": len(clips), "runtime": result["runtime"]})

    nxt = rules.next_run_at(recipe["schedule"], recipe["schedule_day"],
                            recipe["schedule_hour"])
    if built:
        status = (f"{'ran manually' if manual else 'ran on schedule'}: "
                  f"{len(built)} compilation(s), "
                  f"{sum(b['clips'] for b in built)} clips")
        if skipped:
            status += f"; nothing matched for {', '.join(skipped[:3])}"
    else:
        status = f"no clips matched for any channel ({', '.join(skipped[:4])})"

    db.execute(
        "UPDATE recipes SET last_run = ?, next_run = ?, last_build_id = ?, "
        "last_status = ? WHERE id = ?",
        (time.time(), nxt, built[-1]["build_id"] if built else None,
         status[:300], recipe_id))

    if not built:
        raise ValueError(status)
    return built


def reschedule(recipe_id):
    recipe = db.one("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
    if not recipe:
        return None
    nxt = (rules.next_run_at(recipe["schedule"], recipe["schedule_day"],
                             recipe["schedule_hour"])
           if recipe["enabled"] else None)
    db.execute("UPDATE recipes SET next_run = ? WHERE id = ?", (nxt, recipe_id))
    return nxt


def _scheduler_loop():
    """Fire any recipe whose time has come. Checks every few minutes."""
    while True:
        try:
            now = time.time()
            due = db.query(
                "SELECT id, name FROM recipes WHERE enabled = 1 AND schedule != 'off' "
                "AND next_run IS NOT NULL AND next_run <= ?", (now,))
            for recipe in due:
                try:
                    run_recipe(recipe["id"])
                except Exception as exc:  # noqa: BLE001
                    # A recipe that fails must still move its clock forward,
                    # or the scheduler retries it every five minutes forever.
                    db.execute("UPDATE recipes SET last_run = ?, last_status = ? "
                               "WHERE id = ?",
                               (now, f"failed: {exc}"[:300], recipe["id"]))
                    reschedule(recipe["id"])
        except Exception:  # noqa: BLE001 - the scheduler must never die
            pass
        time.sleep(300)


def start_scheduler():
    thread = threading.Thread(target=_scheduler_loop, name="scheduler", daemon=True)
    thread.start()
    # Recompute every due time on boot, so a restart cannot strand a recipe.
    for row in db.query("SELECT id FROM recipes WHERE enabled = 1"):
        reschedule(row["id"])
    return thread


def enqueue_build(build_id):
    db.execute("UPDATE builds SET status='queued', message='waiting to start' "
               "WHERE id = ?", (build_id,))
    _build_queue.put(build_id)


def _build_loop():
    while True:
        build_id = _build_queue.get()
        try:
            _run_build(build_id)
        except Exception:  # noqa: BLE001 - never let the worker die
            db.execute("UPDATE builds SET status='failed', error=?, finished_at=? "
                       "WHERE id = ?",
                       (traceback.format_exc()[-2000:], time.time(), build_id))
        finally:
            _build_queue.task_done()


def _cancelled(build_id):
    row = db.one("SELECT cancel FROM builds WHERE id = ?", (build_id,))
    return bool(row and row["cancel"])


def _progress(build_id, *, stage=None, message=None, done=None):
    sets, params = [], []
    if stage is not None:
        sets.append("stage = ?"); params.append(stage)
    if message is not None:
        sets.append("message = ?"); params.append(message)
    if done is not None:
        sets.append("done_clips = ?"); params.append(done)
    if not sets:
        return
    params.append(build_id)
    db.execute(f"UPDATE builds SET {', '.join(sets)} WHERE id = ?", params)


def _run_build(build_id):
    build = db.one("SELECT * FROM builds WHERE id = ?", (build_id,))
    if not build:
        return
    # apply_aspect turns {"aspect": "16:9"} into real pixel dimensions. The CLI
    # does this when it loads a config file; the server path has to do it here
    # or a recipe's shape setting silently does nothing.
    cfg = engine.apply_aspect(
        engine.deep_merge(engine.DEFAULT_CONFIG, json.loads(build["config_json"] or "{}")))
    items_rows = db.query(
        "SELECT bi.*, v.local_path, v.title AS video_title, v.duration AS src_duration, "
        "v.id AS vid FROM build_items bi JOIN videos v ON v.id = bi.video_id "
        "WHERE bi.build_id = ? ORDER BY bi.position", (build_id,))
    if not items_rows:
        db.execute("UPDATE builds SET status='failed', error='no clips selected', "
                   "finished_at=? WHERE id = ?", (time.time(), build_id))
        return

    started = time.time()
    db.execute("UPDATE builds SET status='running', started_at=?, total_clips=?, "
               "done_clips=0, stage='download', message='starting', error=NULL "
               "WHERE id = ?", (started, len(items_rows), build_id))

    work_dir = paths()["work"] / f"build-{build_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    downloads = paths()["downloads"]

    # ---------------------------------------------------------- 1. fetch
    ready, failures = [], []
    for i, row in enumerate(items_rows, 1):
        if _cancelled(build_id):
            return _finish_cancelled(build_id)
        local = row.get("local_path")
        try:
            if local and Path(local).is_file():
                path = Path(local)
            else:
                _progress(build_id, message=f"downloading {i}/{len(items_rows)}: "
                                            f"{(row['video_title'] or '')[:40]}")
                path = providers.download(row["video_id"], downloads)
                db.execute("UPDATE videos SET local_path = ? WHERE id = ?",
                           (str(path), row["video_id"]))
            ready.append((row, path))
            db.execute("UPDATE build_items SET status='ready' "
                       "WHERE build_id=? AND position=?", (build_id, row["position"]))
        except Exception as exc:  # noqa: BLE001
            failures.append((row["video_id"], f"download failed: {exc}"))
            db.execute("UPDATE build_items SET status='failed', note=? "
                       "WHERE build_id=? AND position=?",
                       (str(exc)[:300], build_id, row["position"]))
        _progress(build_id, done=i)

    # A failed download is usually a passing proxy or YouTube hiccup, so every
    # failed clip gets one more attempt at the end of the stage. If it still
    # fails and the build has spares, a spare takes its place, so a 250 clip
    # build still comes out with 250 clips.
    failed_rows = [row for row in items_rows
                   if row["video_id"] in {f[0] for f in failures}]
    if failed_rows and not _cancelled(build_id):
        spares = [v for v in (cfg["build"].get("spares") or [])
                  if v not in {r["video_id"] for r in items_rows}]
        still_failed = []
        for row in failed_rows:
            _progress(build_id, message=f"retrying {(row['video_title'] or '')[:40]}")
            try:
                path = providers.download(row["video_id"], downloads)
                db.execute("UPDATE videos SET local_path = ? WHERE id = ?",
                           (str(path), row["video_id"]))
                ready.append((row, path))
                failures = [f for f in failures if f[0] != row["video_id"]]
                db.execute("UPDATE build_items SET status='ready', note=NULL "
                           "WHERE build_id=? AND position=?",
                           (build_id, row["position"]))
                continue
            except Exception:  # noqa: BLE001
                pass
            replaced = False
            while spares and not replaced:
                spare_id = spares.pop(0)
                spare = db.one("SELECT id, title, local_path FROM videos WHERE id = ?",
                               (spare_id,))
                if not spare:
                    continue
                try:
                    if spare.get("local_path") and Path(spare["local_path"]).is_file():
                        path = Path(spare["local_path"])
                    else:
                        _progress(build_id, message=f"replacing with "
                                                    f"{(spare['title'] or '')[:40]}")
                        path = providers.download(spare_id, downloads)
                        db.execute("UPDATE videos SET local_path = ? WHERE id = ?",
                                   (str(path), spare_id))
                except Exception:  # noqa: BLE001
                    continue
                old_title = row["video_title"] or row["video_id"]
                new_row = dict(row, video_id=spare_id, vid=spare_id,
                               video_title=spare["title"], title=spare["title"] or "")
                ready.append((new_row, path))
                failures = [f for f in failures if f[0] != row["video_id"]]
                db.execute("UPDATE build_items SET video_id=?, title=?, status='ready', "
                           "note=? WHERE build_id=? AND position=?",
                           (spare_id, spare["title"] or "",
                            f"replaced \"{old_title[:60]}\" (could not download)",
                            build_id, row["position"]))
                replaced = True
            if not replaced:
                still_failed.append(row)
        ready.sort(key=lambda rp: rp[0]["position"])

    if not ready:
        db.execute("UPDATE builds SET status='failed', error=?, finished_at=? "
                   "WHERE id = ?",
                   ("every clip failed to download", time.time(), build_id))
        return

    # ---------------------------------------------------------- 2. encode
    _progress(build_id, stage="encode", done=0,
              message=f"labeling and normalizing {len(ready)} clips")

    total = len(ready)
    plan = []
    for idx, (row, path) in enumerate(ready):
        plan.append({
            "path": Path(path),
            "index": idx,
            "n": idx + 1,
            "total": total,
            "title": row["title"] or row["video_title"] or "",
            "position": row["position"],
            "video_id": row["video_id"],
            "trim_start": float(row.get("trim_start") or 0),
            "trim_end": float(row.get("trim_end") or 0),
        })

    jobs = int(cfg["build"].get("jobs") or 0) or 2
    metas, done = [], 0
    with ThreadPoolExecutor(max_workers=max(1, min(jobs, 16))) as pool:
        futures = {pool.submit(engine.render_clip, item, cfg, work_dir): item
                   for item in plan}
        for future in as_completed(futures):
            item = futures[future]
            done += 1
            if _cancelled(build_id):
                for f in futures:
                    f.cancel()
                return _finish_cancelled(build_id)
            try:
                meta = future.result()
                meta["video_id"] = item["video_id"]
                # Chapter text: the on-screen label plus the real title when we
                # have one, so a three hour upload is navigable on YouTube.
                real_title = (item.get("title") or "").strip()
                label = meta.get("label") or ""
                meta["title"] = (f"{label} - {real_title}"
                                 if real_title and real_title != label else label)
                metas.append(meta)
                db.execute("UPDATE build_items SET status='done' "
                           "WHERE build_id=? AND position=?",
                           (build_id, item["position"]))
            except Exception as exc:  # noqa: BLE001
                failures.append((item["video_id"], f"encode failed: {exc}"))
                db.execute("UPDATE build_items SET status='failed', note=? "
                           "WHERE build_id=? AND position=?",
                           (str(exc)[:300], build_id, item["position"]))
            fast_note = ""
            if metas and str(metas[-1].get("mode", "")).startswith("fast"):
                reused = sum(1 for m in metas if m.get("mode") == "fast (body reused)")
                fast_note = f" (fast: {reused} clip(s) reused from cache)"
            _progress(build_id, done=done,
                      message=f"encoded {done}/{total}{fast_note}")

    if not metas:
        db.execute("UPDATE builds SET status='failed', error=?, finished_at=? "
                   "WHERE id = ?", ("every clip failed to encode", time.time(), build_id))
        return

    # ---------------------------------------------------------- 3. assemble
    metas.sort(key=lambda m: m["index"])
    _progress(build_id, stage="assemble", message="stitching the compilation")

    out_path = Path(cfg["output"]["path"])
    if not out_path.is_absolute():
        out_path = paths()["out"] / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    engine.concat_parts(metas, cfg, out_path, work_dir)
    engine.write_chapter_text(metas, out_path.with_suffix(".chapters.txt"))
    engine.write_manifest(metas, out_path.with_suffix(".manifest.csv"))

    if not cfg["build"].get("test"):
        db.mark_used([m["video_id"] for m in metas if m.get("video_id")])
    try:
        engine.prune_cache(paths()["cache"], float(db.get_setting("body_cache_gb") or 60))
    except Exception:  # noqa: BLE001 - housekeeping must never fail a build
        pass

    total_seconds = sum(float(m["duration"]) for m in metas)
    note = "done"
    if failures:
        note = f"done, {len(failures)} clip(s) skipped"
    db.execute(
        "UPDATE builds SET status='done', stage='done', finished_at=?, message=?, "
        "output_path=?, output_bytes=?, duration_s=?, error=? WHERE id = ?",
        (time.time(), note, str(out_path), out_path.stat().st_size, total_seconds,
         json.dumps(failures) if failures else None, build_id))

    if not cfg["build"].get("keep_intermediates", True):
        shutil.rmtree(work_dir, ignore_errors=True)

    post = cfg["output"].get("post_command")
    if post:
        import subprocess
        subprocess.run(post, shell=True)


def _finish_cancelled(build_id):
    db.execute("UPDATE builds SET status='cancelled', message='cancelled', "
               "finished_at=? WHERE id = ?", (time.time(), build_id))

import time

def _fill_rate(t0, done, total):
    secs = max(time.time() - t0, 0.001)
    rate = done / secs
    left = (total - done) / rate if rate else 0
    r = f"{rate:.0f}/s" if rate >= 1 else f"{rate * 60:.1f}/min"
    eta = f"{left:.0f}s" if left < 90 else f"{left / 60:.0f} min"
    return f" · {r} · ~{eta} left · {secs:.0f}s elapsed"


# ------------------------------------------------------------ prepare step
#
# Before a build: check every picked clip is on disk, download the missing
# ones several at a time, and report each clip's state so the dashboard can
# show ready / downloading / failed (with the reason) per clip. The build then
# only has to encode.

_prepare = {}          # video_id -> {"state": ..., "error": ...}
_prepare_lock = threading.Lock()


def clip_status(video_ids):
    rows = {r["id"]: r for r in db.query(
        f"SELECT id, title, duration, view_count, local_path, thumb FROM videos "
        f"WHERE id IN ({','.join('?' * len(video_ids))})", tuple(video_ids))} if video_ids else {}
    out = []
    for vid in video_ids:
        row = rows.get(vid) or {"id": vid, "title": vid}
        on_disk = bool(row.get("local_path") and Path(row["local_path"]).is_file())
        with _prepare_lock:
            job = dict(_prepare.get(vid) or {})
        state = "ready" if on_disk else (job.get("state") or "missing")
        out.append({"id": vid, "title": row.get("title"), "duration": row.get("duration"),
                    "views": row.get("view_count"), "state": state,
                    "error": job.get("error") if state == "failed" else None})
    return out


def prepare_clips(video_ids, workers=3):
    """Download every missing clip in the background, several at a time."""
    todo = [c["id"] for c in clip_status(video_ids) if c["state"] in ("missing", "failed")]
    with _prepare_lock:
        for vid in todo:
            _prepare[vid] = {"state": "queued"}

    def one(vid):
        with _prepare_lock:
            _prepare[vid] = {"state": "downloading"}
        try:
            path = providers.download(vid, paths()["downloads"])
            db.execute("UPDATE videos SET local_path = ? WHERE id = ?", (str(path), vid))
            with _prepare_lock:
                _prepare[vid] = {"state": "ready"}
        except Exception as exc:  # noqa: BLE001
            with _prepare_lock:
                _prepare[vid] = {"state": "failed", "error": str(exc)[:300]}

    def run_all():
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 6))) as pool:
            list(pool.map(one, todo))

    if todo:
        threading.Thread(target=run_all, name="prepare", daemon=True).start()
    return len(todo)
