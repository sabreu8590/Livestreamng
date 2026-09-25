#!/usr/bin/env bash
# Install the proof-of-origin token provider.
#
# YouTube now demands a PO token for the higher quality formats on several
# player clients. Desktop apps get one from the browser they run beside; a
# server has to run a small provider of its own. This is the standard way
# server-side yt-dlp works now, and it is what makes 1080p downloads reliable
# from a VPS.
#
# Run as root:  sudo /opt/storystack/setup-potoken.sh
set -euo pipefail
PREFIX=/opt/storystack
PORT=4416

echo "==> installing the yt-dlp plugin"
"$PREFIX/venv/bin/pip" install -q --upgrade bgutil-ytdlp-pot-provider
"$PREFIX/venv/bin/pip" install -q --upgrade yt-dlp

# A JavaScript runtime is no longer optional. yt-dlp has deprecated YouTube
# extraction without one, and without it formats come back missing and the
# challenge YouTube sets cannot be solved. deno is the runtime yt-dlp enables
# by default, so installing it needs no further configuration.
echo "==> checking for a JavaScript runtime"
if command -v deno >/dev/null 2>&1; then
  echo "    deno already present: $(deno --version 2>/dev/null | head -1)"
else
  echo "==> installing deno"
  apt-get update -qq
  apt-get install -y -qq curl unzip
  curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y >/dev/null 2>&1 || true
  if ! command -v deno >/dev/null 2>&1 && [ -x /usr/local/bin/deno ]; then
    ln -sf /usr/local/bin/deno /usr/bin/deno
  fi
  if command -v deno >/dev/null 2>&1; then
    echo "    deno installed: $(deno --version 2>/dev/null | head -1)"
  else
    echo "    deno install failed, falling back to nodejs"
    apt-get install -y -qq nodejs
    command -v node >/dev/null 2>&1 && echo "    node installed: $(node --version)"
  fi
fi

start_with_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "==> installing docker"
    apt-get update -qq
    apt-get install -y -qq docker.io
    systemctl enable --now docker
  fi
  echo "==> starting the token provider container"
  docker rm -f bgutil-provider >/dev/null 2>&1 || true
  docker run --name bgutil-provider -d --restart unless-stopped --init \
    -p 127.0.0.1:${PORT}:${PORT} brainicism/bgutil-ytdlp-pot-provider >/dev/null
}

start_with_node() {
  echo "==> docker unavailable, falling back to a node service"
  if ! command -v node >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq nodejs npm git
  fi
  rm -rf "$PREFIX/potoken"
  git clone -q --single-branch --branch 2.0.0 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$PREFIX/potoken"
  cd "$PREFIX/potoken/server"
  npm ci --silent
  npx tsc
  cat > /etc/systemd/system/storystack-potoken.service <<UNIT
[Unit]
Description=storystack proof-of-origin token provider
After=network-online.target

[Service]
ExecStart=/usr/bin/node ${PREFIX}/potoken/server/build/main.js --port ${PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now storystack-potoken
}

if start_with_docker; then :; else start_with_node; fi

echo "==> waiting for the provider to come up"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORT}/ping" >/dev/null 2>&1 \
     || curl -fsS "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
    echo "    provider is answering on port ${PORT}"
    break
  fi
  sleep 2
done

systemctl restart storystack-web
echo
echo "==> what is now in place"
command -v deno >/dev/null 2>&1 && echo "    JS runtime : deno" \
  || { command -v node >/dev/null 2>&1 && echo "    JS runtime : node" \
       || echo "    JS runtime : MISSING, downloads will keep failing"; }
curl -fsS "http://127.0.0.1:${PORT}/ping" >/dev/null 2>&1 \
  && echo "    token provider : running on ${PORT}" \
  || echo "    token provider : not answering"
echo "    yt-dlp     : $("$PREFIX/venv/bin/yt-dlp" --version 2>/dev/null)"
echo
echo "Done. Go back to the dashboard, open Settings, and press Test download."
echo "If it still fails, the Test button now tries every route and tells you"
echo "which one works."
