#!/usr/bin/env bash
# YouTube changes things and yt-dlp goes stale. Run this when listing or
# downloading suddenly stops working.
set -euo pipefail
/opt/storystack/venv/bin/pip install -q --upgrade yt-dlp
systemctl restart storystack-web
echo "yt-dlp updated to $(/opt/storystack/venv/bin/yt-dlp --version) and dashboard restarted"
