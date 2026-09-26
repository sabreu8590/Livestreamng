#!/usr/bin/env bash
# Install the dashboard update (fast builds, preview, check clips, smart shuffle,
# view/date filters, test builds, reset used, auto-replace of failed clips).
#
#   bash /opt/storystack-tools/storystack-fix/deploy_dashboard_update.sh
#
# Safe: backs up every file it touches, refuses to overwrite a file that was
# changed on the VPS since the code was uploaded to GitHub, and rolls back on
# its own if the dashboard does not come back up.
set -euo pipefail
TOOLS=/opt/storystack-tools
APP=/opt/storystack
export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/storystack_github -o IdentitiesOnly=yes"
FILES="bin/storystack.py server/jobs.py server/app.py server/static/app.js server/static/index.html server/static/extras.js"

cd "$TOOLS"
git pull -q
git fetch -q origin storystack-main
echo "== tools updated"

# 1. nothing on the VPS may have changed since it was uploaded (the label patch
#    on bin/storystack.py is expected and is included in the update)
for f in server/jobs.py server/app.py server/static/app.js server/static/index.html; do
  if ! git show "origin/storystack-main:$f" | cmp -s - "$APP/$f"; then
    echo "STOP: $APP/$f was edited on the VPS after the GitHub upload. Nothing was changed."
    echo "      Send Claude a screenshot of:  diff <(cd $TOOLS && git show origin/storystack-main:$f) $APP/$f | head -40"
    exit 1
  fi
done
echo "== VPS files match the uploaded code"

# 2. back up
BK="$APP/backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BK"
for f in $FILES; do
  [ -f "$APP/$f" ] && { mkdir -p "$BK/$(dirname "$f")"; cp -p "$APP/$f" "$BK/$f"; }
done
echo "== backup saved in $BK"

rollback() {
  echo "!! dashboard did not come back up, restoring the backup"
  for f in $FILES; do
    if [ -f "$BK/$f" ]; then cp -p "$BK/$f" "$APP/$f"; else rm -f "$APP/$f"; fi
  done
  systemctl restart storystack-web
  echo "!! restored. Send Claude:  journalctl -u storystack-web -n 40 --no-pager"
  exit 1
}

# 3. install and check
for f in $FILES; do install -m 644 "$TOOLS/$f" "$APP/$f"; done
chmod 755 "$APP/bin/storystack.py"
"$APP/venv/bin/python" -m py_compile "$APP/bin/storystack.py" "$APP/server/jobs.py" "$APP/server/app.py" || rollback
systemctl restart storystack-web
ok=""
for i in $(seq 1 15); do
  sleep 1
  if curl -fs -o /dev/null http://127.0.0.1:8080/health; then ok=1; break; fi
done
[ -n "$ok" ] || rollback
echo "== dashboard updated and running"

# 4. nightly pre-downloader: also pre-encode the top 300 clips for fast builds
install -m 755 "$TOOLS/storystack-fix/prefetch.py" "$APP/prefetch.py"
SVC=/etc/systemd/system/storystack-prefetch.service
if [ -f "$SVC" ] && ! grep -q -- "--encode" "$SVC"; then
  sed -i 's|\(ExecStart=.*prefetch.py.*\)$|\1 --encode 300 --encode-workers 3|' "$SVC"
  systemctl daemon-reload
fi
echo "== nightly job: $(grep -o 'prefetch.py.*' "$SVC" 2>/dev/null || echo 'not installed')"

# 5. keep GitHub in sync with what now runs on the VPS
bash "$TOOLS/storystack-fix/push_code_to_github.sh" >/dev/null 2>&1 && echo "== GitHub copy updated" || echo "(GitHub copy not updated; harmless)"
echo
echo "Done. Hard-refresh the dashboard (Cmd+Shift+R)."
echo "To undo:  for f in $FILES; do cp -p $BK/\$f $APP/\$f 2>/dev/null; done; systemctl restart storystack-web"
