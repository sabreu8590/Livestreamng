# StoryStack 1080x1920 fix

## Verified result (VPS, 2026-09-25)
`verify_resolution.py` on https://www.youtube.com/shorts/cRf9ZrV77AQ via DataImpulse
sticky ports 10000/10001/10002: `selected 399+251-17`, `ACTUAL 1080x1920 av1`, 12.6 MB.

Dashboard confirmed after switching the saved proxy to `:10000`: fresh download
`/var/lib/storystack/downloads/XLM5Ur7mMMU.mp4` = `h264,1080,1920` (build 5).
Old downloads cached before the fix (e.g. 608x1080) must be deleted to be re-fetched.

## Root causes
1. Format: `[ext=mp4]` capped some Shorts at 608x1080 H.264. Use `res:<short side>` sorting
   (`server/providers.py` already has `format_sort: res:{max_height()}` with max_height=1080).
2. Proxy: `gw.dataimpulse.com:823` is the ROTATING port. YouTube media URLs are locked to the
   IP that extracted them (direct download from the VPS -> 403), and rotating exits break
   the googlevideo download (TLS handshake failures / timeouts).
   Fix: use a STICKY port (10000+) so extraction and download share one IP.

## Files
- `verify_resolution.py`  download one Short through the saved proxy, ffprobe it (`PP=10000` overrides the port)
- `proxy_diag.py`         curl youtube.com / googlevideo via proxy vs direct
- `selector_test.py`      offline check of format selection
- `mac_download.sh`       home-IP fallback (no proxy / PO token needed)
- `format_string.py`      reference format options
