import sqlite3, subprocess, sys
sys.path[:0] = ["/opt/storystack", "/opt/storystack/server"]
import yt_dlp
try:
    from server import db
except ImportError:
    import db
DB = "/var/lib/storystack/storystack.db"
db.init(DB)
def get_proxy():
    for fn in ("get_setting", "get"):
        try:
            v = getattr(db, fn)("proxy")
            if v: return v
        except Exception: pass
    con = sqlite3.connect(DB)
    for (t,) in con.execute("select name from sqlite_master where type='table'"):
        cols = [c[1] for c in con.execute(f"pragma table_info({t})")]
        if "key" in cols and "value" in cols:
            row = con.execute(f"select value from {t} where key='proxy'").fetchone()
            if row and row[0]: return row[0]
proxy = get_proxy()
print("proxy:", proxy.split("@")[-1], "| scheme:", proxy.split("://")[0])
opts = {"proxy": proxy, "format": "bv*+ba/b", "format_sort": ["res:1080", "proto:https"],
        "js_runtimes": {"deno": {}}, "quiet": True, "legacy_server_connect": True,
        "extractor_args": {"youtube": {"skip": ["hls"]}, "youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]}}}
with yt_dlp.YoutubeDL(opts) as ydl:
    info = ydl.extract_info(sys.argv[1], download=False)
v = next(f for f in info["requested_formats"] if f.get("vcodec") != "none")
host = v["url"].split("/")[2]
print("media host:", host, "| format", v["format_id"], f"{v['width']}x{v['height']}")
def curl(label, url, via_proxy):
    cmd = ["curl", "-sS", "-o", "/dev/null", "-m", "20", "-r", "0-1023", "-w", "%{http_code}", url]
    if via_proxy: cmd[1:1] = ["-x", proxy]
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(f"{label:34} {r.stdout.strip()} {r.stderr.strip()[:110]}")
for vp in (True, False):
    tag = "PROXY " if vp else "DIRECT"
    curl(f"{tag} youtube.com", "https://www.youtube.com/generate_204", vp)
    curl(f"{tag} redirector.googlevideo.com", "https://redirector.googlevideo.com/generate_204", vp)
    curl(f"{tag} media host generate_204", f"https://{host}/generate_204", vp)
    curl(f"{tag} actual media URL", v["url"], vp)
