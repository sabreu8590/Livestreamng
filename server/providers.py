"""Where the clip library comes from.

Two providers with the same shape so the rest of the app does not care:
  youtube  - lists a channel with yt-dlp and downloads on demand
  local    - treats a folder of video files as a channel (used for testing,
             and useful if the masters are already on disk)
"""

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BUILD = "2026.09.23-res2"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi", ".ts", ".m2ts"}

# YouTube's Shorts cutoff. Anything at or under this from a channel's main tab
# is treated as a short when the shorts tab itself is not what was listed.
SHORT_MAX_SECONDS = 180


class ProviderError(RuntimeError):
    pass


# ------------------------------------------------------------------ youtube

DEFAULT_COOKIE_FILE = "/etc/storystack/cookies.txt"


def cookie_file():
    """Path to a Netscape cookies.txt, if one has been provided.

    YouTube blocks most datacenter IPs outright. A desktop app never notices
    because it runs on a home connection with a logged-in browser; a VPS gets
    "Sign in to confirm you're not a bot" on every download. Cookies exported
    from a signed-in browser are the standard way through.
    """
    path = os.environ.get("STORYSTACK_COOKIES") or DEFAULT_COOKIE_FILE
    try:
        import db
        saved = db.get_setting("cookie_file")
        if saved:
            path = saved
    except Exception:  # noqa: BLE001
        pass
    return path if path and Path(path).is_file() else None


def has_cookies():
    return cookie_file() is not None


def proxy():
    """An outbound proxy for yt-dlp, if one is configured.

    Cookies get most people past a datacenter block. When they do not, routing
    through a residential proxy does, because the block is on the IP's
    reputation rather than on the account.
    """
    value = os.environ.get("STORYSTACK_PROXY") or ""
    if not value:
        try:
            import db
            value = db.get_setting("proxy") or ""
        except Exception:  # noqa: BLE001
            value = ""
    return value.strip() or None


POT_DEFAULT = "http://127.0.0.1:4416"


def pot_base_url():
    """Where the proof-of-origin token provider is listening, if anywhere."""
    value = os.environ.get("STORYSTACK_POT_URL")
    if value is None:
        try:
            import db
            value = db.get_setting("pot_url")
        except Exception:  # noqa: BLE001
            value = None
    if value is None:
        value = POT_DEFAULT
    return (value or "").strip() or None


def pot_available():
    """Is the token provider actually up? Cheap enough to check on demand."""
    url = pot_base_url()
    if not url:
        return False
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/ping", timeout=3) as r:
            return r.status < 500
    except Exception:  # noqa: BLE001
        # Some builds have no /ping; a refused connection is the real signal.
        try:
            with urllib.request.urlopen(url, timeout=3):
                return True
        except Exception:  # noqa: BLE001
            return False


def js_runtime():
    """Which JavaScript runtime yt-dlp can use, if any.

    yt-dlp deprecated YouTube extraction without one: formats come back
    incomplete and the challenge YouTube sets cannot be solved. deno is the
    only runtime enabled by default, so that is the one worth having.
    """
    import shutil
    for name in ("deno", "node", "bun", "qjs"):
        path = shutil.which(name)
        if path:
            return {"name": name, "path": path, "default_enabled": name == "deno"}
    return None


def pot_plugin_installed():
    try:
        import importlib.util
        return importlib.util.find_spec("bgutil_ytdlp_pot_provider") is not None
    except Exception:  # noqa: BLE001
        return False


def player_clients():
    """Which YouTube player clients to try, in order.

    This used to be hardcoded to include web_safari, which was a mistake: that
    client requires a proof-of-origin token, and without one YouTube answers
    "The page needs to be reloaded" instead of serving the video. Empty means
    let yt-dlp decide, which is almost always the right answer because the
    maintainers retune it as YouTube changes.
    """
    try:
        import db
        value = (db.get_setting("player_clients") or "").strip()
    except Exception:  # noqa: BLE001
        value = ""
    value = os.environ.get("STORYSTACK_PLAYER_CLIENTS", value).strip()
    return [c.strip() for c in value.split(",") if c.strip()] if value else []


# Clients that need no PO token at all, used as the fallback ladder when the
# preferred route fails. Ordered by how reliable they have been in practice.
NO_TOKEN_CLIENTS = ["tv", "android_vr", "web_embedded"]


def max_height():
    """Cap on download resolution. 1080 by default."""
    try:
        import db
        value = int(db.get_setting("max_height") or 0)
    except Exception:  # noqa: BLE001
        value = 0
    return value or int(os.environ.get("STORYSTACK_MAX_HEIGHT") or 1080)


# "1080p" means 1080 on the SHORT side. A vertical short at 1080p is 1080 wide
# and 1920 tall, so a height<=1080 filter silently halves it to 608x1080.
QUALITY_TO_MAX_HEIGHT = {480: 854, 720: 1280, 1080: 1920, 1440: 2560, 2160: 3840}


def tallest_allowed(cap):
    """Height limit that lets `cap` through in either orientation."""
    cap = int(cap or 1080)
    if cap in QUALITY_TO_MAX_HEIGHT:
        return QUALITY_TO_MAX_HEIGHT[cap]
    return int(round(cap * 16 / 9))


def format_string(cap=None):
    """Best stream at or under the cap, stepping down gracefully.

    No [ext=mp4] filter. YouTube only ships avc1/mp4 up to 608x1080 for a
    vertical short; the real 1080x1920 is VP9 in webm. Asking for mp4 first
    matches the small one and never falls through.
    """
    cap = int(cap or max_height())
    tall = tallest_allowed(cap)
    fallback = tallest_allowed(720)
    return (f"bestvideo[height<={tall}]+bestaudio/"
            f"best[height<={tall}]/"
            f"bestvideo[height<={fallback}]+bestaudio/"
            f"best[height<={fallback}]/"
            f"bestvideo+bestaudio/best")


BOT_BLOCK = ("sign in to confirm", "not a bot", "confirm you're not",
             "this content isn't available", "failed to extract any player response")

# A different failure: YouTube accepted us but the client we asked for needs a
# proof-of-origin token we did not supply.
NEEDS_TOKEN = ("page needs to be reloaded", "playability status: unplayable",
               "requested format is not available", "no video formats found")

STALE_COOKIES = ("no longer valid", "have likely been rotated", "login_required")


def friendly_download_error(exc):
    """Turn yt-dlp's wall of text into something actionable."""
    text = str(exc)
    low = text.lower()

    if not js_runtime():
        return ("This server has no JavaScript runtime, and yt-dlp needs one for "
                "YouTube now. Without it formats come back missing no matter what "
                "else is configured. Run sudo /opt/storystack/setup-potoken.sh on "
                "the VPS, which installs it.")
    if any(marker in low for marker in STALE_COOKIES):
        return ("The cookies have been rotated and are dead. This happens when they "
                "are exported from a browser that then keeps using YouTube. Export "
                "again from a browser you close straight afterwards and do not open "
                "YouTube in again, then upload the new file.")
    if any(marker in low for marker in NEEDS_TOKEN):
        if pot_available():
            return ("The token provider is running but YouTube still refused this "
                    "format. Update yt-dlp (Settings, or "
                    "sudo /opt/storystack/update-ytdlp.sh) and try again.")
        return ("YouTube wants a proof-of-origin token for this format, and no "
                "token provider is running. This is the normal way servers "
                "download from YouTube now. Run "
                "sudo /opt/storystack/setup-potoken.sh on the VPS, which takes "
                "about a minute, then press Test download again.")
    if any(marker in low for marker in BOT_BLOCK):
        if has_cookies():
            return ("YouTube rejected the request even with cookies. The cookies "
                    "have probably expired; export a fresh cookies.txt from a "
                    "signed-in browser and upload it again. Updating yt-dlp also "
                    "helps: sudo /opt/storystack/update-ytdlp.sh")
        return ("YouTube is blocking this server because it is a datacenter IP. "
                "Export cookies.txt from a browser signed in to YouTube and put "
                "it at /etc/storystack/cookies.txt, then try again. "
                "See the Settings panel for the steps.")
    if "http error 429" in low or "too many requests" in low:
        return ("YouTube is rate limiting this server. Wait a while, and keep "
                "builds smaller or less frequent.")
    if "video unavailable" in low or "private video" in low:
        return "That video is private, removed, or region blocked."
    return text.strip().splitlines()[-1][:300] if text.strip() else "download failed"


def _ytdlp_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "noprogress": True,
        "retries": 3,
        "socket_timeout": 30,
    }
    cookies = cookie_file()
    if cookies:
        opts["cookiefile"] = cookies
    prox = proxy()
    if prox:
        opts["proxy"] = prox

    # No js_runtimes override here on purpose: the setup script installs deno,
    # which yt-dlp enables by default, so nothing needs configuring. Passing an
    # unverified option shape would risk breaking every call.

    args = {}
    clients = player_clients()
    if clients:
        args["youtube"] = {"player_client": clients}
    pot = pot_base_url()
    if pot and pot != POT_DEFAULT:
        args["youtubepot-bgutilhttp"] = {"base_url": [pot]}
    if args:
        opts["extractor_args"] = args

    if extra:
        opts.update(extra)
    return opts


def normalize_channel_url(raw):
    raw = (raw or "").strip()
    if not raw:
        raise ProviderError("no channel URL given")
    if raw.startswith("@"):
        return f"https://www.youtube.com/{raw}"
    if not raw.startswith("http"):
        return f"https://www.youtube.com/@{raw.lstrip('/')}"
    return raw.rstrip("/")


def _tab_urls(base):
    """Return (shorts_tab, videos_tab) for a channel URL."""
    base = re.sub(r"/(videos|shorts|streams|featured)/?$", "", base.rstrip("/"))
    return f"{base}/shorts", f"{base}/videos"


def thumb_url(video_id):
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


# ---------------------------------------------------- YouTube Data API (optional)
#
# yt-dlp needs no key and works fine. A Data API key is worth having anyway when
# you run this across a network of channels: listing is one HTTP call per 50
# videos instead of a page scrape, view counts are exact rather than rounded,
# and it does not break when YouTube changes its markup. The free quota is
# 10,000 units a day and listing a 1,000 video channel costs about 40.

API_ROOT = "https://www.googleapis.com/youtube/v3"
ISO_DURATION = re.compile(
    r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$")


def api_key():
    key = os.environ.get("STORYSTACK_YT_API_KEY")
    if key:
        return key.strip()
    try:
        import db
        return (db.get_setting("yt_api_key") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _api(endpoint, params, key):
    params = dict(params)
    params["key"] = key
    url = f"{API_ROOT}/{endpoint}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = json.loads(exc.read().decode("utf-8"))
            body = body.get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            pass
        if exc.code == 403 and "quota" in body.lower():
            raise ProviderError("the YouTube API key is out of quota for today; "
                                "it resets at midnight Pacific") from exc
        raise ProviderError(f"YouTube API error {exc.code}: {body or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"could not reach the YouTube API: {exc.reason}") from exc


def parse_iso_duration(text):
    m = ISO_DURATION.match(str(text or ""))
    if not m:
        return 0.0
    days, hours, minutes, seconds = m.groups()
    return (int(days or 0) * 86400 + int(hours or 0) * 3600
            + int(minutes or 0) * 60 + float(seconds or 0))


def _resolve_channel(url, key):
    """Turn a handle, /channel/UC..., or /user/name into an uploads playlist."""
    raw = normalize_channel_url(url)
    tail = raw.rstrip("/").split("/")[-1]
    path = urllib.parse.urlparse(raw).path

    if "/channel/" in path:
        params = {"part": "contentDetails,snippet", "id": path.split("/channel/")[1].split("/")[0]}
    elif tail.startswith("@"):
        params = {"part": "contentDetails,snippet", "forHandle": tail}
    else:
        params = {"part": "contentDetails,snippet", "forUsername": tail}

    data = _api("channels", params, key)
    items = data.get("items") or []
    if not items and "forUsername" in params:
        data = _api("channels", {"part": "contentDetails,snippet",
                                 "forHandle": "@" + tail}, key)
        items = data.get("items") or []
    if not items:
        raise ProviderError(f"the YouTube API found no channel for {raw}")
    item = items[0]
    uploads = (item.get("contentDetails", {})
                   .get("relatedPlaylists", {}).get("uploads"))
    if not uploads:
        raise ProviderError("that channel has no uploads playlist")
    return uploads, (item.get("snippet", {}).get("title") or tail)


def list_channel_api(channel_url, key, *, include_long=True, progress=None,
                     max_items=5000):
    uploads, title = _resolve_channel(channel_url, key)

    ids, page = [], None
    while len(ids) < max_items:
        params = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
        if page:
            params["pageToken"] = page
        data = _api("playlistItems", params, key)
        for item in data.get("items") or []:
            vid = item.get("contentDetails", {}).get("videoId")
            if vid:
                ids.append(vid)
        if progress:
            progress(f"listed {len(ids)} videos")
        page = data.get("nextPageToken")
        if not page:
            break

    entries = []
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        data = _api("videos", {"part": "snippet,contentDetails,statistics",
                               "id": ",".join(chunk)}, key)
        for item in data.get("items") or []:
            snippet = item.get("snippet") or {}
            stats = item.get("statistics") or {}
            duration = parse_iso_duration(
                (item.get("contentDetails") or {}).get("duration"))
            is_short = 0 < duration <= SHORT_MAX_SECONDS
            if not include_long and not is_short:
                continue
            published = (snippet.get("publishedAt") or "")[:10] or None
            entries.append({
                "id": item["id"],
                "title": snippet.get("title") or item["id"],
                "duration": duration,
                "view_count": int(stats["viewCount"]) if stats.get("viewCount") else None,
                "upload_date": published,
                "is_short": is_short,
                "url": f"https://www.youtube.com/watch?v={item['id']}",
                "thumb": thumb_url(item["id"]),
                "width": None, "height": None,
            })
        if progress:
            progress(f"fetched details for {min(i + 50, len(ids))}/{len(ids)}")

    if not entries:
        raise ProviderError("the YouTube API returned no videos for that channel")
    return {"title": title, "entries": entries}


def list_channel(channel_url, *, include_long=True, progress=None):
    """List a channel's clips. Metadata only, nothing is downloaded."""
    key = api_key()
    if key:
        try:
            return list_channel_api(channel_url, key, include_long=include_long,
                                    progress=progress)
        except ProviderError as exc:
            # A bad key or an exhausted quota should not stop the sync; yt-dlp
            # still works without one.
            if progress:
                progress(f"API listing failed ({exc}); falling back to yt-dlp")

    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise ProviderError("yt-dlp is not installed (pip install yt-dlp)") from exc

    base = normalize_channel_url(channel_url)
    shorts_tab, videos_tab = _tab_urls(base)

    targets = [(shorts_tab, True)]
    if include_long:
        targets.append((videos_tab, False))

    seen, entries, channel_title = {}, [], None

    for url, force_short in targets:
        if progress:
            progress(f"listing {url}")
        try:
            with YoutubeDL(_ytdlp_opts({"extract_flat": "in_playlist"})) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001 - a missing tab is not fatal
            if progress:
                progress(f"could not list {url}: {exc}")
            continue
        if not info:
            continue
        channel_title = channel_title or info.get("channel") or info.get("title")
        for entry in (info.get("entries") or []):
            if not entry:
                continue
            vid = entry.get("id")
            if not vid or vid in seen:
                continue
            duration = float(entry.get("duration") or 0)
            is_short = force_short or (0 < duration <= SHORT_MAX_SECONDS)
            record = {
                "id": vid,
                "title": entry.get("title") or vid,
                "duration": duration,
                "view_count": entry.get("view_count"),
                "upload_date": _upload_date(entry),
                "is_short": is_short,
                "url": entry.get("url") or f"https://www.youtube.com/watch?v={vid}",
                "thumb": thumb_url(vid),
                "width": entry.get("width"),
                "height": entry.get("height"),
            }
            seen[vid] = record
            entries.append(record)
        if progress:
            progress(f"{len(entries)} clips so far")

    if not entries:
        raise ProviderError(
            "no clips came back for that channel. Check the URL, and check that "
            "this machine can reach youtube.com.")
    return {"title": channel_title or base, "entries": entries}


def _upload_date(entry):
    raw = entry.get("upload_date")
    if raw and re.fullmatch(r"\d{8}", str(raw)):
        s = str(raw)
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    ts = entry.get("timestamp") or entry.get("release_timestamp")
    if ts:
        try:
            return time.strftime("%Y-%m-%d", time.gmtime(float(ts)))
        except (TypeError, ValueError, OSError):
            return None
    return None


def fetch_video_meta(video_id):
    """Full metadata for one video, including an accurate upload date."""
    from yt_dlp import YoutubeDL
    url = f"https://www.youtube.com/watch?v={video_id}"
    with YoutubeDL(_ytdlp_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise ProviderError(f"no metadata for {video_id}")
    return {
        "id": video_id,
        "title": info.get("title") or video_id,
        "duration": float(info.get("duration") or 0),
        "view_count": info.get("view_count"),
        "upload_date": _upload_date(info),
        "width": info.get("width"),
        "height": info.get("height"),
    }


class _Capture:
    """Collects yt-dlp's own log lines so they can be shown in the browser."""

    def __init__(self):
        self.lines = []

    def _add(self, prefix, msg):
        text = str(msg).rstrip()
        if text:
            self.lines.append(f"{prefix}{text}")

    def debug(self, msg):
        self._add("", msg)

    def info(self, msg):
        self._add("", msg)

    def warning(self, msg):
        self._add("WARNING: ", msg)

    def error(self, msg):
        self._add("ERROR: ", msg)

    def text(self, limit=9000):
        joined = "\n".join(self.lines)
        return joined[-limit:] if len(joined) > limit else joined


def _probe_route(video_id, clients, use_cookies=True, capture=None):
    """Ask YouTube for this video using one specific player client.

    Returns (ok, detail). Metadata only, so the whole ladder runs in seconds
    rather than downloading six copies of the same clip.
    """
    from yt_dlp import YoutubeDL
    opts = _ytdlp_opts({"skip_download": True, "ignoreerrors": False})
    if clients:
        args = dict(opts.get("extractor_args") or {})
        args["youtube"] = {"player_client": list(clients)}
        opts["extractor_args"] = args
    if not use_cookies:
        opts.pop("cookiefile", None)
    if capture is not None:
        opts["logger"] = capture
        opts["verbose"] = True
        opts["quiet"] = False
        opts["no_warnings"] = False
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}",
                                    download=False)
    except Exception as exc:  # noqa: BLE001
        return False, {"error": friendly_download_error(exc), "raw": str(exc)[-400:]}
    if not info:
        return False, {"error": "no metadata came back"}

    heights = sorted({int(f["height"]) for f in (info.get("formats") or [])
                      if f.get("height")}, reverse=True)
    if not heights:
        return False, {"error": "no video formats were offered"}
    return True, {"best_height": heights[0], "title": info.get("title"),
                  "heights": heights[:6]}


# Tried in order. The no-token clients come first because they work without
# any extra infrastructure; the token ones are only reachable once a provider
# is running, and are listed after so the report can say so.
ROUTE_LADDER = [
    ("default, with cookies", [], True),
    ("default, no cookies", [], False),
    ("tv, with cookies", ["tv"], True),
    ("tv, no cookies", ["tv"], False),
    ("android_vr, no cookies", ["android_vr"], False),
    ("web_embedded, no cookies", ["web_embedded"], False),
    ("mweb, no cookies", ["mweb"], False),
    ("web, with cookies", ["web"], True),
]


def diagnose(video_id=None, *, deep=True):
    """Walk every route until one works, and say which did.

    A single failed attempt only tells you that one thing is broken. Trying
    the whole ladder tells you what to switch to.
    """
    import shutil
    import tempfile

    report = {
        "cookies": bool(cookie_file()),
        "cookie_path": cookie_file() or DEFAULT_COOKIE_FILE,
        "proxy": bool(proxy()),
        "max_height": max_height(),
        "video_id": video_id,
        "pot_plugin": pot_plugin_installed(),
        "pot_running": pot_available(),
        "js_runtime": (js_runtime() or {}).get("name"),
        "configured_clients": player_clients(),
        "routes": [],
    }
    try:
        from yt_dlp import version as ytv
        report["ytdlp_version"] = ytv.__version__
    except Exception:  # noqa: BLE001
        report["ytdlp_version"] = "unknown"

    if not video_id:
        report["ok"] = False
        report["stage"] = "setup"
        report["error"] = "no video to test with; sync a channel first"
        return report

    have_cookies = bool(cookie_file())
    capture = _Capture()
    winner = None
    for label, clients, want_cookies in ROUTE_LADDER:
        if want_cookies and not have_cookies:
            continue          # nothing to test with
        ok, detail = _probe_route(video_id, clients, want_cookies,
                                  capture if winner is None else None)
        row = {"route": label, "clients": clients, "cookies": want_cookies, "ok": ok}
        row.update(detail)
        report["routes"].append(row)
        if ok and winner is None:
            winner = (label, clients, want_cookies, detail)
            if not deep:
                break
    report["log"] = capture.text()

    if not winner:
        report["ok"] = False
        report["stage"] = "metadata"
        first = report["routes"][0] if report["routes"] else {}
        report["error"] = first.get("error") or "every route was refused"
        report["raw"] = first.get("raw")
        return report

    label, clients, want_cookies, detail = winner
    report["working_route"] = label
    report["working_clients"] = clients
    report["working_cookies"] = want_cookies
    report["best_height"] = detail.get("best_height")
    report["title"] = detail.get("title")

    # Confirm with a real download on the route that worked.
    tmp = Path(tempfile.mkdtemp(prefix="storystack-test-"))
    saved = os.environ.get("STORYSTACK_PLAYER_CLIENTS")
    try:
        os.environ["STORYSTACK_PLAYER_CLIENTS"] = ",".join(clients)
        path = download(video_id, tmp)
        info = ffprobe_basic(path)
        report["ok"] = True
        report["stage"] = "done"
        report["resolution"] = f"{info['width']}x{info['height']}"
        report["duration"] = info["duration"]
        report["size_mb"] = round(path.stat().st_size / 1048576, 1)
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["stage"] = "download"
        report["error"] = friendly_download_error(exc)
        report["raw"] = str(exc)[-400:]
    finally:
        if saved is None:
            os.environ.pop("STORYSTACK_PLAYER_CLIENTS", None)
        else:
            os.environ["STORYSTACK_PLAYER_CLIENTS"] = saved
        shutil.rmtree(tmp, ignore_errors=True)
    return report


COOKIE_HINT = "# Netscape HTTP Cookie File"


def save_cookies(text, path=None):
    """Write an uploaded cookie jar, after checking it looks like one."""
    path = Path(path or DEFAULT_COOKIE_FILE)
    body = (text or "").strip()
    if not body:
        raise ProviderError("that file was empty")
    lowered = body.lower()
    if "youtube.com" not in lowered:
        raise ProviderError(
            "that file has no youtube.com cookies in it. Export again with the "
            "extension while a YouTube tab is open and you are signed in.")
    if not body.startswith("#") and "\t" not in body:
        raise ProviderError(
            "that does not look like a cookies.txt. It should be the Netscape "
            "format, which is what the browser extensions export by default.")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not body.startswith("#"):
        body = COOKIE_HINT + "\n" + body
    path.write_text(body + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return str(path)


def fetch_details_api(video_ids, key):
    """Exact duration, date and view count for up to 50 ids in one call."""
    out = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        data = _api("videos", {"part": "snippet,contentDetails,statistics",
                               "id": ",".join(chunk)}, key)
        for item in data.get("items") or []:
            snippet = item.get("snippet") or {}
            stats = item.get("statistics") or {}
            out[item["id"]] = {
                "duration": parse_iso_duration(
                    (item.get("contentDetails") or {}).get("duration")),
                "upload_date": (snippet.get("publishedAt") or "")[:10] or None,
                "view_count": int(stats["viewCount"]) if stats.get("viewCount") else None,
                "title": snippet.get("title"),
            }
    return out


def fetch_details_ytdlp(video_id):
    """One video's details without downloading it. Slow, but needs no API key."""
    from yt_dlp import YoutubeDL
    opts = _ytdlp_opts({"skip_download": True, "ignoreerrors": False})
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}",
                                download=False)
    if not info:
        raise ProviderError(f"no details for {video_id}")
    return {
        "duration": float(info.get("duration") or 0),
        "upload_date": _upload_date(info),
        "view_count": info.get("view_count"),
        "title": info.get("title"),
    }


def download(video_id, dest_dir, *, progress=None):
    """Download one video at the best available quality. Returns a local path."""
    from yt_dlp import YoutubeDL

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Already have it from a previous build.
    for existing in dest_dir.glob(f"{video_id}.*"):
        if existing.suffix.lower() in VIDEO_EXTS and existing.stat().st_size > 10240:
            return existing

    opts = _ytdlp_opts({
        "outtmpl": str(dest_dir / f"{video_id}.%(ext)s"),
        "format": format_string(),
        "format_sort": [f"res:{max_height()}", "vcodec:h264",
                        "acodec:m4a", "size"],
        "merge_output_format": "mp4",
        "ignoreerrors": False,
        "concurrent_fragment_downloads": 4,
    })
    if progress:
        opts["progress_hooks"] = [
            lambda d: progress(d.get("status"), d.get("_percent_str", "").strip())
        ]

    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(friendly_download_error(exc)) from exc

    for candidate in sorted(dest_dir.glob(f"{video_id}.*")):
        if candidate.suffix.lower() in VIDEO_EXTS and candidate.stat().st_size > 10240:
            return candidate
    raise ProviderError(f"download produced no usable file for {video_id}")


# -------------------------------------------------------------------- local

def list_local(folder, *, recursive=False):
    """Treat a folder of files as a channel."""
    folder = Path(os.path.expanduser(folder))
    if not folder.is_dir():
        raise ProviderError(f"not a folder: {folder}")
    globber = folder.rglob("*") if recursive else folder.glob("*")
    files = sorted(
        (p for p in globber
         if p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.startswith(".")),
        key=lambda p: p.name)
    if not files:
        raise ProviderError(f"no video files in {folder}")

    entries = []
    for path in files:
        meta = ffprobe_basic(path)
        stamp = time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime))
        entries.append({
            "id": f"local:{path.name}",
            "title": path.stem,
            "duration": meta["duration"],
            "view_count": None,
            "upload_date": stamp,
            "is_short": 0 < meta["duration"] <= SHORT_MAX_SECONDS,
            "url": str(path),
            "thumb": None,
            "width": meta["width"],
            "height": meta["height"],
            "local_path": str(path),
        })
    return {"title": folder.name, "entries": entries}


def ffprobe_basic(path):
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        return {"duration": 0.0, "width": 0, "height": 0}
    data = json.loads(proc.stdout or "{}")
    video = next((s for s in data.get("streams", [])
                  if s.get("codec_type") == "video"), {})
    try:
        duration = float(data.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {"duration": duration,
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0)}


def make_thumbnail(video_path, out_path, *, at=None, width=360):
    """Grab a poster frame so the grid has something to show."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if at is None:
        meta = ffprobe_basic(video_path)
        at = max(0.0, min(1.0, meta["duration"] * 0.25)) if meta["duration"] else 0.5
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(video_path),
         "-frames:v", "1", "-vf", f"scale={width}:-2", str(out_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return out_path if proc.returncode == 0 and out_path.is_file() else None

# --- StoryStack fix: always pick the ORIGINAL audio track (not a YouTube auto-dub) ---
import re as _re
_orig_format_string = format_string
_AUDIO = _re.compile(r"\+(bestaudio|ba)(?![\w\[])")

def format_string(*args, **kwargs):
    base = _orig_format_string(*args, **kwargs)
    pref = [_AUDIO.sub(lambda m: "+" + m.group(1) + "[format_note*=original]", p)
            for p in base.split("/") if _AUDIO.search(p)]
    return "/".join(pref + [base])

# --- StoryStack fix: retry failed downloads on a fresh sticky proxy port ---
import re as _re2, time as _time
_orig_download = download
_orig_proxy = proxy
_proxy_override = [None]

def proxy():
    return _proxy_override[0] or _orig_proxy()

def _bump_port(p, n):
    m = _re2.match(r"^(.*:)(\d+)(/?)$", p or "")
    return f"{m.group(1)}{int(m.group(2)) + n}{m.group(3)}" if m else p

_RETRYABLE = ("403", "forbidden", "not a bot", "handshake", "timed out",
              "reset by peer", "unable to download", "connection")

def download(video_id, dest_dir, *args, **kwargs):
    base, last = _orig_proxy(), None
    for attempt in range(4):
        _proxy_override[0] = _bump_port(base, attempt) if base and attempt else None
        try:
            return _orig_download(video_id, dest_dir, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last, msg = exc, str(exc).lower()
            if "confirm your age" in msg:
                raise RuntimeError("age-restricted on YouTube: add YouTube cookies "
                                   "in Settings to include it") from exc
            if not any(s in msg for s in _RETRYABLE):
                raise
            _time.sleep(2)
        finally:
            _proxy_override[0] = None
    raise RuntimeError(f"YouTube refused the download on 4 proxy IPs: "
                       f"{str(last)[:120]}") from last

# --- StoryStack fix v3: never skip a clip without trying hard -----------------
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
    dest = _pl3.Path(dest_dir); dest.mkdir(parents=True, exist_ok=True)
    errors = []
    for port in _rnd3.sample(range(10003, 10060), 3):
        opts = {
            "format": format_string(),
            "format_sort": [f"res:{max_height() or 1080}", "proto:https"],
            "merge_output_format": "mp4",
            "outtmpl": str(dest / f"{video_id}.%(ext)s"),
            "js_runtimes": {"deno": {}},
            "extractor_args": {"youtube": {"skip": ["hls"]},
                               "youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]}},
            "legacy_server_connect": True, "retries": 10, "fragment_retries": 10,
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
