#!/usr/bin/env bash
# Install the overnight pre-downloader: runs daily at 06:20 UTC (right after the 05:52 sync).
# Defaults: clips >= 1M views, Shorts only, 3 at once, stop at 5 GB (~$5) per night.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
install -m 755 "$HERE/prefetch.py" /opt/storystack/prefetch.py
cat > /etc/systemd/system/storystack-prefetch.service <<'UNIT'
[Unit]
Description=StoryStack overnight pre-download of clips
After=network-online.target storystack-sync.service
[Service]
Type=oneshot
WorkingDirectory=/opt/storystack/server
EnvironmentFile=-/etc/storystack/web.env
Nice=10
ExecStart=/opt/storystack/venv/bin/python /opt/storystack/prefetch.py --min-views 1000000 --workers 3 --max-gb 5
UNIT
cat > /etc/systemd/system/storystack-prefetch.timer <<'UNIT'
[Unit]
Description=Run StoryStack pre-download every night
[Timer]
OnCalendar=*-*-* 06:20
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now storystack-prefetch.timer
echo "installed. next run: $(systemctl list-timers storystack-prefetch.timer --no-pager | sed -n 2p)"
echo "run now:      systemctl start storystack-prefetch --no-block"
echo "progress:     /opt/storystack/venv/bin/python /opt/storystack/prefetch.py --status"
echo "live log:     journalctl -u storystack-prefetch -f"
