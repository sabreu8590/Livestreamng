# After your nap: 4 steps (about 15 minutes)

Everything below runs on the VPS (`ssh root@2.25.133.110`). Paste one block at a time.

## Step 1: GitHub key (once)
```bash
ssh-keygen -t ed25519 -f ~/.ssh/storystack_github -N "" -C "storystack-vps" && cat ~/.ssh/storystack_github.pub
```
GitHub -> sabreu8590/Livestreamng -> Settings -> Deploy keys -> Add deploy key.
Paste the printed line, tick **Allow write access**, save. Make sure the repo is **Private**.

## Step 2: get the new tools onto the VPS
```bash
export GIT_SSH_COMMAND="ssh -i ~/.ssh/storystack_github -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
git clone -q -b claude/storystack-1080x1920-resolution-ktmy7f git@github.com:sabreu8590/Livestreamng.git /opt/storystack-tools && echo cloned
```
Later updates: `cd /opt/storystack-tools && git pull` (with the same export line).

## Step 3: upload the dashboard code to GitHub
```bash
bash /opt/storystack-tools/storystack-fix/push_code_to_github.sh
```
It checks for keys and passwords first and refuses to upload if it finds any.
After this, Claude and your dev team can read and improve the real code.

## Step 4: overnight pre-downloader
See what it would cost first (clips over 1M views that aren't downloaded yet):
```bash
/opt/storystack/venv/bin/python -c "import sqlite3;c=sqlite3.connect('/var/lib/storystack/storystack.db');n=c.execute(\"select count(*) from videos where view_count>=1000000 and duration>0 and duration<=240 and (local_path is null or local_path='')\").fetchone()[0];print(n,'clips to pre-download, about',round(n*0.013,1),'GB, about $',round(n*0.013,1))"
```
Install it (runs nightly at 06:20 UTC; stops at 5 GB = about $5 per night):
```bash
bash /opt/storystack-tools/storystack-fix/install_prefetch.sh
systemctl start storystack-prefetch --no-block
```
Watch it: `journalctl -u storystack-prefetch -f` (Ctrl+C to stop watching; the download keeps going).
Check progress any time: `/opt/storystack/venv/bin/python /opt/storystack/prefetch.py --status`

Other options:
- whole library once: `/opt/storystack/venv/bin/python /opt/storystack/prefetch.py --min-views 0 --max-gb 30`
- only 2M+: `... prefetch.py --min-views 2000000`
- faster: `--workers 4` (more proxy IPs at once)

## Optional: private web address with HTTPS
1. Add a DNS **A record**: `storystack.plotpointedashboard.com` -> `2.25.133.110` (whoever manages the plotpointedashboard.com DNS can do this in about 1 minute)
2. `bash /opt/storystack-tools/storystack-fix/setup_domain.sh storystack.plotpointedashboard.com`

It requires login and tells Google not to index it. It stops by itself if the DNS isn't ready or ports 80/443 are already in use.
