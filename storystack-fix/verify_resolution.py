#!/usr/bin/env python3
"""Run ON THE VPS from /opt/storystack:  python3 /path/to/verify_resolution.py <short-url>

Downloads one real Short through the saved DataImpulse proxy + bgutil PO tokens
using the new format selection, then ffprobes the result.
"""
import json, sqlite3, subprocess, sys, tempfile
sys.path.insert(0, "/opt/storystack")
sys.path.insert(0, "/opt/storystack/server")
import yt_dlp
from format_string import ydl_format_opts

try:
    from server import db
except ImportError:
    import db
db.init()  # required, otherwise the proxy setting is invisible

def get_proxy():
    for fn in ("get_setting", "get"):
        if hasattr(db, fn):
            try:
                v = getattr(db, fn)("proxy")
                if v:
                    return v
            except Exception:
                pass
    con = sqlite3.connect("/var/lib/storystack/storystack.db")
    for (t,) in con.execute("select name from sqlite_master where type='table'"):
        cols = [c[1] for c in con.execute(f"pragma table_info({t})")]
        if "key" in cols and "value" in cols:
            row = con.execute(f"select value from {t} where key='proxy'").fetchone()
            if row:
                return row[0]
    return None

url = sys.argv[1]
proxy = get_proxy()
print("proxy:", "set" if proxy else "MISSING")
out = tempfile.mkdtemp(prefix="ssverify-")
opts = {
    **ydl_format_opts(1080),
    "proxy": proxy,
    "outtmpl": f"{out}/%(id)s.%(ext)s",
    "js_runtimes": {"deno": {}},
    "extractor_args": {"youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]}},
}
with yt_dlp.YoutubeDL(opts) as ydl:
    info = ydl.extract_info(url, download=True)
    path = info["requested_downloads"][0]["filepath"]
print("selected format:", info["format_id"], info.get("format"))
probe = json.loads(subprocess.check_output(
    ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
     "stream=codec_name,width,height", "-of", "json", path]))["streams"][0]
print(f"FILE {path}\nACTUAL {probe['width']}x{probe['height']} {probe['codec_name']}")
