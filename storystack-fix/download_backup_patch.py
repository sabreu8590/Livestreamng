
# --- StoryStack fix v3: never skip a clip without trying hard -----------------
# 1) Every yt-dlp call avoids HLS/m3u8 (DataImpulse breaks those streams) and
#    retries network errors.  2) If the normal download still fails, a second,
#    independent downloader (the method verified at 1080x1920) tries 3 fresh
#    sticky IPs.  3) Every failed attempt is logged with its reason.
import random as _rnd3, sys as _sys3, pathlib as _pl3
_v3_orig_opts = _ytdlp_opts
_v3_orig_download = download

def _ytdlp_opts(*args, **kwargs):
    o = _v3_orig_opts(*args, **kwargs)
    ea = o.setdefault("extractor_args", {})
    yt = ea.setdefault("youtube", {})
    skip = list(yt.get("skip") or [])
    if "hls" not in skip:
        yt["skip"] = skip + ["hls"]
    fs = o.get("format_sort")
    if fs is not None and not any(str(x).startswith("proto") for x in fs):
        o["format_sort"] = list(fs) + ["proto:https"]
    o.setdefault("retries", 10)
    o.setdefault("fragment_retries", 10)
    o["legacy_server_connect"] = True
    return o

def _v3_log(msg):
    print(f"[download-v3] {msg}", file=_sys3.stderr, flush=True)

def _v3_with_port(p, port):
    m = _re2.match(r"^(.*:)(\d+)(/?)$", p or "")
    return f"{m.group(1)}{port}{m.group(3)}" if m else p

def _v3_backup(video_id, dest_dir, base_proxy):
    from yt_dlp import YoutubeDL
    dest = _pl3.Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    errors = []
    for port in _rnd3.sample(range(10003, 10060), 3):
        opts = {
            "format": format_string(),
            "format_sort": [f"res:{max_height() or 1080}", "proto:https"],
            "merge_output_format": "mp4",
            "outtmpl": str(dest / f"{video_id}.%(ext)s"),
            "js_runtimes": {"deno": {}},
            "extractor_args": {
                "youtube": {"skip": ["hls"]},
                "youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]},
            },
            "legacy_server_connect": True,
            "retries": 10, "fragment_retries": 10,
            "quiet": True, "no_warnings": True, "noprogress": True,
        }
        if base_proxy:
            opts["proxy"] = _v3_with_port(base_proxy, port)
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
            path = _pl3.Path(info["requested_downloads"][0]["filepath"])
            _v3_log(f"{video_id}: backup downloader OK on port {port} ({info.get('format_id')})")
            return path
        except Exception as exc:  # noqa: BLE001
            errors.append(f"port {port}: {str(exc)[:100]}")
            _v3_log(f"{video_id}: backup failed on port {port}: {exc}")
            if "confirm your age" in str(exc).lower():
                break
    raise RuntimeError("; ".join(errors))

def download(video_id, dest_dir, *args, **kwargs):
    try:
        return _v3_orig_download(video_id, dest_dir, *args, **kwargs)
    except Exception as first:  # noqa: BLE001
        _v3_log(f"{video_id}: normal download failed: {first}")
        if "age-restricted" in str(first).lower():
            raise
        try:
            return _v3_backup(video_id, dest_dir, _orig_proxy())
        except Exception as second:  # noqa: BLE001
            raise RuntimeError(f"both downloaders failed. normal: {str(first)[:140]} | "
                               f"backup: {str(second)[:200]}") from second
