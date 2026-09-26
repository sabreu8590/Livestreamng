"""storystack dashboard - the web front end.

Browse a channel's clips, click the ones you want, drag them into order,
press Build. The heavy lifting happens in jobs.py on a worker thread.
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import db
import jobs
import providers
import recipes as rules
import streams

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("STORYSTACK_DATA", "/var/lib/storystack"))
PASSWORD = os.environ.get("STORYSTACK_PASSWORD", "")
SECRET = os.environ.get("STORYSTACK_SECRET", "")
COOKIE = "storystack_session"
SESSION_DAYS = 30

app = FastAPI(title="storystack", docs_url=None, redoc_url=None)


# ------------------------------------------------------------------- setup

@app.on_event("startup")
def _startup():
    global SECRET
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.init(DATA_DIR / "storystack.db")
    jobs.configure(DATA_DIR)
    jobs.start_worker()
    jobs.start_scheduler()
    if not SECRET:
        SECRET = db.get_setting("secret") or secrets.token_hex(32)
        db.set_setting("secret", SECRET)


# -------------------------------------------------------------------- auth

def _sign(payload):
    mac = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{mac}"


def _valid_cookie(raw):
    if not raw or "." not in raw:
        return False
    payload, _, mac = raw.rpartition(".")
    expected = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return False
    try:
        return float(payload) > time.time()
    except ValueError:
        return False


def _authed(request):
    if not PASSWORD:
        return True  # no password configured: local-only mode
    return _valid_cookie(request.cookies.get(COOKIE))


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    path = request.url.path
    public = (path in ("/login", "/api/login", "/health")
              or path.startswith("/static/"))
    if not public and not _authed(request):
        if path.startswith("/api/"):
            return JSONResponse({"error": "not signed in"}, status_code=401)
        return HTMLResponse(LOGIN_HTML, status_code=200)
    return await call_next(request)


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    supplied = str(body.get("password") or "")
    if not PASSWORD:
        return {"ok": True}
    if not hmac.compare_digest(supplied, PASSWORD):
        time.sleep(1.0)  # slow down guessing
        raise HTTPException(status_code=401, detail="wrong password")
    expiry = time.time() + SESSION_DAYS * 86400
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, _sign(f"{expiry:.0f}"), httponly=True,
                    samesite="lax", max_age=SESSION_DAYS * 86400)
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------- channels

@app.get("/api/channels")
def list_channels():
    return {"channels": db.query(
        "SELECT c.*, (SELECT COUNT(*) FROM videos v WHERE v.channel_id = c.id) "
        "AS video_count FROM channels c ORDER BY c.added_at")}


@app.post("/api/channels")
async def add_channel(request: Request):
    body = await request.json()
    raw = str(body.get("url") or "").strip()
    kind = str(body.get("kind") or "youtube")
    if not raw:
        raise HTTPException(400, "a channel URL or folder path is required")

    if kind == "youtube":
        url = providers.normalize_channel_url(raw)
        handle = url.rstrip("/").split("/")[-1]
    else:
        url, handle = raw, Path(raw).name

    existing = db.one("SELECT id FROM channels WHERE url = ?", (url,))
    channel_id = existing["id"] if existing else uuid.uuid4().hex[:12]
    if not existing:
        db.execute("INSERT INTO channels(id, url, handle, title, kind, added_at) "
                   "VALUES(?,?,?,?,?,?)",
                   (channel_id, url, handle, handle, kind, time.time()))

    threading.Thread(target=jobs.sync_channel, args=(channel_id,),
                     kwargs={"include_long": bool(body.get("include_long", True))},
                     daemon=True).start()
    return {"id": channel_id, "url": url}


@app.post("/api/channels/{channel_id}/sync")
async def sync_channel(channel_id: str, request: Request):
    if not db.one("SELECT id FROM channels WHERE id = ?", (channel_id,)):
        raise HTTPException(404, "no such channel")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    threading.Thread(target=jobs.sync_channel, args=(channel_id,),
                     kwargs={"include_long": bool(body.get("include_long", True))},
                     daemon=True).start()
    return {"ok": True}


@app.post("/api/channels/{channel_id}/hydrate")
def hydrate_channel(channel_id: str):
    if not db.one("SELECT id FROM channels WHERE id = ?", (channel_id,)):
        raise HTTPException(404, "no such channel")
    pending = len(jobs.needs_detail(channel_id))
    threading.Thread(target=jobs.hydrate_channel, args=(channel_id,),
                     daemon=True).start()
    return {"pending": pending}


@app.delete("/api/channels/{channel_id}")
def delete_channel(channel_id: str):
    db.execute("DELETE FROM videos WHERE channel_id = ?", (channel_id,))
    db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    return {"ok": True}


# ------------------------------------------------------------------ videos

@app.get("/api/videos")
def list_videos(channel: str = "", q: str = "", month: str = "",
                shorts: str = "", unused: str = "", sort: str = "date_desc",
                limit: int = 500, offset: int = 0):
    where, params = ["1=1"], []
    if channel:
        where.append("channel_id = ?"); params.append(channel)
    if q:
        where.append("LOWER(title) LIKE ?"); params.append(f"%{q.lower()}%")
    if month:
        where.append("upload_date LIKE ?"); params.append(f"{month}%")
    if shorts == "only":
        where.append("is_short = 1")
    elif shorts == "exclude":
        where.append("is_short = 0")
    if unused == "1":
        where.append("used_count = 0")

    order = {
        "date_desc": "upload_date DESC, title ASC",
        "date_asc": "upload_date ASC, title ASC",
        "views_desc": "view_count DESC",
        "views_asc": "view_count ASC",
        "title_asc": "title ASC",
        "duration_asc": "duration ASC",
        "duration_desc": "duration DESC",
    }.get(sort, "upload_date DESC")

    sql = (f"SELECT * FROM videos WHERE {' AND '.join(where)} "
           f"ORDER BY {order} LIMIT ? OFFSET ?")
    rows = db.query(sql, (*params, max(1, min(limit, 2000)), max(0, offset)))
    agg = db.one(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(MAX(duration,0)),0) AS secs, "
        f"SUM(CASE WHEN duration IS NULL OR duration <= 0 THEN 1 ELSE 0 END) AS unknown "
        f"FROM videos WHERE {' AND '.join(where)}", tuple(params))
    total = agg["n"]
    months = db.query(
        "SELECT DISTINCT substr(upload_date, 1, 7) AS month FROM videos "
        "WHERE upload_date IS NOT NULL AND upload_date != '' "
        "ORDER BY month DESC LIMIT 36")
    lib = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(MAX(duration,0)),0) AS secs "
                 "FROM videos WHERE channel_id = ?", (channel,)) if channel else None
    return {"videos": rows, "total": total,
            "runtime": agg["secs"] or 0,
            "unknown": agg["unknown"] or 0,
            "library": {"count": lib["n"], "runtime": lib["secs"]} if lib else None,
            "months": [m["month"] for m in months if m["month"]]}


@app.get("/thumbs/{name}")
def thumb(name: str):
    path = jobs.paths()["thumbs"] / Path(name).name
    if not path.is_file():
        raise HTTPException(404, "no thumbnail")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


# ------------------------------------------------------------------ builds

@app.post("/api/builds")
async def create_build(request: Request):
    body = await request.json()
    video_ids = body.get("video_ids") or []
    if not video_ids:
        raise HTTPException(400, "select at least one clip")
    name = (body.get("name") or f"compilation-{time.strftime('%Y%m%d-%H%M')}").strip()
    cfg = jobs.build_config(body, jobs.slugify(name))
    build_id = jobs.create_build(name, video_ids, cfg,
                                 titles=body.get("titles") or {},
                                 trims=body.get("trims") or {})
    return {"id": build_id}


# ----------------------------------------------------------------- recipes

@app.get("/api/recipes")
def list_recipes():
    rows = db.query("SELECT r.*, c.title AS channel_title FROM recipes r "
                    "LEFT JOIN channels c ON c.id = r.channel_id ORDER BY r.id DESC")
    for row in rows:
        row["channel_ids"] = jobs.recipe_channels(row)
        for key in ("rules_json", "label_json", "video_json", "output_json"):
            try:
                row[key.replace("_json", "")] = json.loads(row[key] or "{}")
            except json.JSONDecodeError:
                row[key.replace("_json", "")] = {}
    return {"recipes": rows, "defaults": rules.DEFAULT_RULES}


@app.post("/api/recipes/preview")
async def preview_recipe(request: Request):
    body = await request.json()
    channel_ids = [c for c in (body.get("channel_ids") or []) if c]
    if not channel_ids and body.get("channel_id"):
        channel_ids = [body["channel_id"]]
    if not channel_ids:
        raise HTTPException(400, "pick a channel first")

    rule_dict = body.get("rules") or {}
    per_channel = []
    for cid in channel_ids:
        channel = db.one("SELECT title, handle FROM channels WHERE id = ?", (cid,))
        res = jobs.preview_recipe(cid, rule_dict)
        per_channel.append({
            "channel_id": cid,
            "channel": (channel or {}).get("title") or cid,
            "qualified": res["qualified"], "selected": res["selected"],
            "runtime": res["runtime"], "clips": res["clips"],
        })

    # The detailed sample shows the first channel; the rest report totals, so
    # a rule across eight channels stays readable.
    first = per_channel[0]
    result = {
        "window": jobs.rules.apply_recipe([], rule_dict)["window"],
        "qualified": sum(c["qualified"] for c in per_channel),
        "selected": sum(c["selected"] for c in per_channel),
        "runtime": sum(c["runtime"] for c in per_channel),
        "channels": [{k: v for k, v in c.items() if k != "clips"}
                     for c in per_channel],
        "sample_channel": first["channel"],
    }
    clips = first["clips"]
    result["sample"] = [
        {"id": c["id"], "title": c.get("title"), "views": c.get("view_count"),
         "date": c.get("upload_date"), "duration": c.get("duration"),
         "used": c.get("used_count")}
        for c in clips[:60]
    ]
    return result


@app.post("/api/recipes")
async def save_recipe(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    channel_id = body.get("channel_id") or (body.get("channel_ids") or [None])[0]
    if not name:
        raise HTTPException(400, "give the recipe a name")
    if not channel_id:
        raise HTTPException(400, "pick at least one channel")

    channel_ids = [c for c in (body.get("channel_ids") or []) if c] or [channel_id]
    fields = (name, channel_ids[0], json.dumps(channel_ids),
              1 if body.get("enabled", True) else 0,
              json.dumps(rules.merge_rules(body.get("rules") or {})),
              json.dumps(body.get("label") or {}),
              json.dumps(body.get("video") or {}),
              json.dumps(body.get("output") or {}),
              str(body.get("schedule") or "off"),
              int(body.get("schedule_day") or 1),
              int(body.get("schedule_hour") or 4))

    recipe_id = body.get("id")
    if recipe_id:
        db.execute(
            "UPDATE recipes SET name=?, channel_id=?, channels_json=?, enabled=?, "
            "rules_json=?, label_json=?, video_json=?, output_json=?, schedule=?, "
            "schedule_day=?, schedule_hour=? WHERE id=?", (*fields, int(recipe_id)))
    else:
        cur = db.execute(
            "INSERT INTO recipes(name, channel_id, channels_json, enabled, rules_json, "
            "label_json, video_json, output_json, schedule, schedule_day, "
            "schedule_hour, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (*fields, time.time()))
        recipe_id = cur.lastrowid

    next_run = jobs.reschedule(recipe_id)
    return {"id": recipe_id, "next_run": next_run}


@app.delete("/api/recipes/{recipe_id}")
def delete_recipe(recipe_id: int):
    db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    return {"ok": True}


@app.post("/api/recipes/{recipe_id}/run")
def run_recipe_now(recipe_id: int):
    try:
        built = jobs.run_recipe(recipe_id, manual=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"builds": built, "build_id": built[0]["build_id"]}


# ----------------------------------------------------------------- streams

@app.get("/api/streams")
def list_streams():
    try:
        units = streams.list_units()
    except Exception as exc:  # noqa: BLE001
        return {"streams": [], "pattern": streams.pattern(), "error": str(exc)}
    return {"streams": units, "pattern": streams.pattern()}


@app.post("/api/streams/pattern")
async def set_stream_pattern(request: Request):
    body = await request.json()
    streams.set_pattern(body.get("pattern"))
    return {"pattern": streams.pattern()}


@app.post("/api/streams/{unit}/{action}")
def control_stream(unit: str, action: str):
    try:
        return {"stream": streams.control(unit, action)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc))


@app.get("/api/streams/{unit}/log")
def stream_log(unit: str, lines: int = 40):
    try:
        return {"log": streams.journal(unit, lines)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc))


@app.get("/api/builds")
def list_builds(limit: int = 30):
    return {"builds": db.query(
        "SELECT id, name, status, stage, created_at, started_at, finished_at, "
        "total_clips, done_clips, message, output_path, output_bytes, duration_s "
        "FROM builds ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),))}


@app.get("/api/builds/{build_id}")
def get_build(build_id: int):
    build = db.one("SELECT * FROM builds WHERE id = ?", (build_id,))
    if not build:
        raise HTTPException(404, "no such build")
    build["items"] = db.query(
        "SELECT bi.position, bi.video_id, bi.label, bi.status, bi.note, "
        "bi.trim_start, bi.trim_end, "
        "v.title AS video_title FROM build_items bi "
        "LEFT JOIN videos v ON v.id = bi.video_id "
        "WHERE bi.build_id = ? ORDER BY bi.position", (build_id,))
    if build.get("error"):
        try:
            build["skipped"] = json.loads(build["error"])
        except (json.JSONDecodeError, TypeError):
            build["skipped"] = None
    return build


@app.post("/api/builds/{build_id}/cancel")
def cancel_build(build_id: int):
    db.execute("UPDATE builds SET cancel = 1 WHERE id = ?", (build_id,))
    return {"ok": True}


@app.get("/api/builds/{build_id}/download")
def download_build(build_id: int, file: str = "video"):
    build = db.one("SELECT * FROM builds WHERE id = ?", (build_id,))
    if not build or not build.get("output_path"):
        raise HTTPException(404, "nothing to download yet")
    base = Path(build["output_path"])
    target = {"video": base,
              "chapters": base.with_suffix(".chapters.txt"),
              "manifest": base.with_suffix(".manifest.csv")}.get(file, base)
    if not target.is_file():
        raise HTTPException(404, f"{target.name} is not there")
    return FileResponse(target, filename=target.name,
                        media_type="application/octet-stream")


# ---------------------------------------------------------------- settings

@app.post("/api/diagnose")
async def diagnose_downloads(request: Request):
    """Try one real download and report what happened, in the browser."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    video_id = body.get("video_id")
    if not video_id:
        row = db.one("SELECT id FROM videos WHERE id NOT LIKE 'local:%' "
                     "ORDER BY rowid DESC LIMIT 1")
        video_id = row["id"] if row else None
    return providers.diagnose(video_id)


@app.post("/api/settings/cookies")
async def upload_cookies(request: Request):
    body = await request.json()
    try:
        path = providers.save_cookies(body.get("text") or "")
    except providers.ProviderError as exc:
        raise HTTPException(400, str(exc))
    db.set_setting("cookie_file", path)
    return {"ok": True, "path": path}


@app.delete("/api/settings/cookies")
def clear_cookies():
    path = providers.cookie_file()
    if path:
        try:
            os.remove(path)
        except OSError:
            pass
    db.set_setting("cookie_file", "")
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    key = providers.api_key()
    cookies = providers.cookie_file()
    return {"settings": db.get_setting("defaults", {}),
            "cpu_count": os.cpu_count() or 2,
            "output_dir": str(jobs.paths()["out"]),
            "aspects": list(jobs.engine.ASPECTS.keys()),
            # Never echo the key back, only whether one is set.
            "yt_api_key_set": bool(key),
            "yt_api_key_source": ("environment"
                                  if os.environ.get("STORYSTACK_YT_API_KEY")
                                  else ("saved" if key else "none")),
            "cookies_set": bool(cookies),
            "cookie_path": cookies or providers.DEFAULT_COOKIE_FILE,
            "cookie_age_days": _cookie_age(cookies),
            "proxy_set": bool(providers.proxy()),
            "player_clients": providers.player_clients(),
            "pot_plugin": providers.pot_plugin_installed(),
            "pot_running": providers.pot_available(),
            "js_runtime": (providers.js_runtime() or {}).get("name"),
            "max_height": providers.max_height()}


def _cookie_age(path):
    """Cookies expire; knowing how old they are explains a sudden failure."""
    if not path:
        return None
    try:
        return round((time.time() - os.path.getmtime(path)) / 86400, 1)
    except OSError:
        return None


@app.post("/api/settings")
async def save_settings(request: Request):
    body = await request.json()
    if "yt_api_key" in body:
        db.set_setting("yt_api_key", str(body.pop("yt_api_key") or "").strip())
    if "max_height" in body:
        db.set_setting("max_height", int(body.pop("max_height") or 1080))
    if "cookie_file" in body:
        db.set_setting("cookie_file", str(body.pop("cookie_file") or "").strip())
    if "proxy" in body:
        db.set_setting("proxy", str(body.pop("proxy") or "").strip())
    if "player_clients" in body:
        value = body.pop("player_clients")
        db.set_setting("player_clients",
                       ",".join(value) if isinstance(value, list) else str(value or ""))
    if "pot_url" in body:
        db.set_setting("pot_url", str(body.pop("pot_url") or "").strip())
    if body:
        db.set_setting("defaults", body)
    return {"ok": True}


# ------------------------------------------------------------------- pages

app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (HERE / "static" / "index.html").read_text(encoding="utf-8")


LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>storystack</title>
<style>
:root{color-scheme:dark}
body{margin:0;height:100vh;display:grid;place-items:center;background:#0d0f12;
color:#e8eaed;font:15px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}
form{background:#161a1f;padding:32px;border-radius:14px;width:300px;
border:1px solid #262b33}
h1{margin:0 0 4px;font-size:19px}
p{margin:0 0 20px;color:#8b939e;font-size:13px}
input{width:100%;box-sizing:border-box;padding:11px 13px;border-radius:9px;
border:1px solid #2d333c;background:#0d0f12;color:#e8eaed;font-size:14px}
button{width:100%;margin-top:12px;padding:11px;border:0;border-radius:9px;
background:#4c8dff;color:#fff;font-size:14px;font-weight:600;cursor:pointer}
button:hover{background:#3d7bec}
.err{color:#ff6b6b;font-size:13px;margin-top:10px;min-height:18px}
</style></head><body>
<form onsubmit="go(event)">
<h1>storystack</h1><p>Compilation builder</p>
<input id="pw" type="password" placeholder="Password" autofocus>
<button type="submit">Sign in</button>
<div class="err" id="err"></div>
</form>
<script>
async function go(e){e.preventDefault();
 const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({password:document.getElementById('pw').value})});
 if(r.ok){location.href='/'}else{document.getElementById('err').textContent='Wrong password'}}
</script></body></html>"""
