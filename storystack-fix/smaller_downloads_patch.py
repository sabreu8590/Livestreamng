
# --- StoryStack fix v4: smaller downloads, smarter retries --------------------
# Builds re-encode every clip to H.264 anyway, so the source codec doesn't matter
# for the finished video. AV1/VP9 at the same 1080x1920 are ~3x smaller than
# YouTube's H.264, which means ~3x less DataImpulse bandwidth. Drop the
# "prefer h264" and "prefer biggest file" sort keys so yt-dlp picks AV1/VP9.
_v4_prev_opts = _ytdlp_opts

def _ytdlp_opts(*args, **kwargs):
    o = _v4_prev_opts(*args, **kwargs)
    fs = o.get("format_sort")
    if fs:
        o["format_sort"] = [x for x in fs if x not in ("vcodec:h264", "size", "acodec:m4a")]
    return o

# friendly_download_error() rewords errors ("refused", "blocking"), which hid them
# from the retry-on-a-new-IP check. Teach it those words.
_RETRYABLE = tuple(_RETRYABLE) + ("refused", "blocking", "rate limit", "token")
