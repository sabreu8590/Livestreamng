#!/usr/bin/env bash
# Mac (home IP) downloader: brew install yt-dlp ffmpeg deno
# Usage: ./mac_download.sh <url>... ; then rsync the files to the VPS.
set -euo pipefail
OUT="${OUT:-$HOME/StoryStack/downloads}"; mkdir -p "$OUT"
yt-dlp -f "bv*+ba/b" -S "res:1080" --merge-output-format mp4 \
  --cookies-from-browser "${BROWSER:-chrome}" \
  -o "$OUT/%(id)s.%(ext)s" --exec 'ffprobe -v error -select_streams v:0 -show_entries stream=width,height,codec_name -of csv=p=0 {}' "$@"
if [ -n "${VPS:-}" ]; then rsync -av "$OUT/" "$VPS:/var/lib/storystack/incoming/"; fi
