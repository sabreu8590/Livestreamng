"""Drop-in replacement for format selection in server/providers.py.

YouTube serves Shorts as H.264/mp4 only up to 608x1080; the 1080x1920 stream is
VP9 (webm) or AV1. So: no [ext=mp4] filter anywhere, and cap resolution by the
SHORT side using yt-dlp's `res` sort field (res = min(width, height)), which is
correct for vertical and landscape alike.
"""

def format_string(max_res: int = 1080) -> str:
    # Any codec/container; best video + best audio, else best progressive.
    return "bv*+ba/b"


def format_sort(max_res: int = 1080) -> list[str]:
    # Prefer the largest short-side <= max_res (1080 -> 1080x1920 / 1920x1080).
    return [f"res:{max_res}"]


def ydl_format_opts(max_res: int = 1080) -> dict:
    """Merge these into the YoutubeDL options dict."""
    return {
        "format": format_string(max_res),
        "format_sort": format_sort(max_res),
        # ffmpeg muxes VP9/AV1 + Opus into mp4 fine; keeps .mp4 paths for the stitcher.
        "merge_output_format": "mp4",
    }
