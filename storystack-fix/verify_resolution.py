#!/usr/bin/env python3
"""Run on the VPS:  python3 verify_resolution.py https://www.youtube.com/shorts/VIDEO_ID
Downloads one real Short via the proxy saved in StoryStack's SQLite settings
(+ bgutil PO tokens on :4416, deno), then ffprobes the actual file."""
import json, os, sqlite3, subprocess, sys, tempfile
sys.path[:0] = ["/opt/storystack", "/opt/storystack/server"]
import yt_dlp

try:
    from server import db
except ImportError:
    import db
DB = "/var/lib/storystack/storystack.db"
db.init(DB)  # without this the saved proxy is invisible

def get_proxy():
    for fn in ("get_setting", "get"):
        try:
            v = getattr(db, fn)("proxy")
            if v:
                return v
        except Exception:
            pass
    con = sqlite3.connect(DB)
    for (t,) in con.execute("select name from sqlite_master where type='table'"):
        cols = [c[1] for c in con.execute(f"pragma table_info({t})")]
        if "key" in cols and "value" in cols:
            row = con.execute(f"select value from {t} where key='proxy'").fetchone()
            if row and row[0]:
                return row[0]
    return None

url = sys.argv[1]
proxy = get_proxy()
if not proxy:
    sys.exit(f"no 'proxy' setting found in {DB} -- YouTube will bot-block this IP")
print("proxy:", proxy.split("@")[-1])  # host:port only, no credentials
out = tempfile.mkdtemp(prefix="ssverify-")
opts = {
    "format": "bv*+ba/b",
    # res = short side -> 1080x1920 for Shorts; prefer direct https (DASH) over HLS
    "format_sort": ["res:1080", "proto:https"],
    "legacy_server_connect": True,
    "retries": 5, "fragment_retries": 10,
    "merge_output_format": "mp4",
    "proxy": proxy,
    "outtmpl": f"{out}/%(id)s.%(ext)s",
    "js_runtimes": {"deno": {}},
    "extractor_args": {"youtube": {"skip": ["hls"]}, "youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]}},
}
with yt_dlp.YoutubeDL(opts) as ydl:
    info = ydl.extract_info(url, download=False)
    for f in info["formats"]:
        if (f.get("height") or 0) >= 1080 or (f.get("width") or 0) >= 1080:
            print(f"  offered {f['format_id']:>8} {f.get('width')}x{f.get('height')} "
                  f"{f.get('vcodec')} {f.get('protocol')}")
    print("selected:", info["format_id"], "-", info.get("format"))
    info = ydl.process_ie_result(info, download=True)
    path = info["requested_downloads"][0]["filepath"]
s = json.loads(subprocess.check_output(
    ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
     "stream=codec_name,width,height", "-of", "json", path]))["streams"][0]
print(f"FILE   {path} ({os.path.getsize(path)//1024} KiB)\nACTUAL {s['width']}x{s['height']} {s['codec_name']}")
