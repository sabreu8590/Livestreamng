#!/usr/bin/env bash
# Upload the StoryStack code on this VPS to GitHub (branch: storystack-main).
# Safe to re-run: each run adds a new commit with whatever changed.
set -euo pipefail
KEY="$HOME/.ssh/storystack_github"
REPO="git@github.com:sabreu8590/Livestreamng.git"
BRANCH="storystack-main"
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
cd /opt/storystack

echo "== checking for secrets before upload"
if grep -rIlE 'AIza[0-9A-Za-z_-]{30,}|:[^@/ ]+@gw\.dataimpulse\.com|BEGIN [A-Z ]*PRIVATE KEY' \
     --exclude-dir=venv --exclude-dir=.venv --exclude-dir=.git --exclude='*.bak*' . ; then
  echo "STOP: the files above contain a key or password. Nothing was uploaded."; exit 1
fi
echo "   none found"

[ -d .git ] || git init -q
cat > .gitignore <<'GI'
venv/
.venv/
__pycache__/
*.pyc
*.bak*
*.db
*.sqlite*
*.log
cookies.txt
*.env
GI
git add -A
git -c user.name="StoryStack VPS" -c user.email="storystack-vps@users.noreply.github.com" \
    commit -qm "StoryStack code from VPS ($(date -u +%F\ %H:%M) UTC)" || echo "   no changes since last upload"
git push -q "$REPO" "HEAD:refs/heads/$BRANCH" && echo "== uploaded to GitHub branch $BRANCH"
git ls-files | sed 's/^/   /'
