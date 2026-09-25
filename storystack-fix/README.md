# StoryStack 1080x1920 fix

1. In `server/providers.py`, replace the format string with `ydl_format_opts()` from
   `format_string.py` (format `bv*+ba/b`, `format_sort=["res:1080"]`, merge to mp4).
   Remove every `[ext=mp4]` / `[height<=1080]` filter.
2. `sudo systemctl restart storystack-web`
3. Verify with a real Short: `cd /opt/storystack && python3 storystack-fix/verify_resolution.py <url>`
   Expect `ACTUAL 1080x1920 av1` (or `vp9`).

`selector_test.py` runs yt-dlp's real selector on a Short-shaped format list:
old string -> 608x1080 h264, new -> 1080x1920.

Stitching: the files are now AV1/VP9, so the compilation step must re-encode
(e.g. `-c:v libx264`), not stream-copy with `-c copy`.
