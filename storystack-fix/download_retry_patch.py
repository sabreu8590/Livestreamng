
# --- StoryStack fix: retry failed downloads on a fresh sticky proxy port ---
# A sticky DataImpulse IP can rotate between lookup and download, and YouTube
# then answers 403. Retry on the next ports (10000 -> 10001 -> ...) and give
# every failure a plain-English reason.
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
