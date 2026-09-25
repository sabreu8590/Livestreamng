#!/usr/bin/env bash
# Put the dashboard on its own private web address with HTTPS.
#   1. First add a DNS "A" record:  storystack.plotpointedashboard.com -> 2.25.133.110
#   2. Then run:  bash setup_domain.sh storystack.plotpointedashboard.com
# Login stays required; search engines are told not to index it.
set -euo pipefail
DOMAIN="${1:?usage: setup_domain.sh your.sub.domain}"
IP="$(curl -4 -s ifconfig.me)"
DNS="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')"
[ "$DNS" = "$IP" ] || { echo "STOP: $DOMAIN points to '${DNS:-nothing}', not this server ($IP). Fix the DNS record first (can take a few minutes)."; exit 1; }
if ss -ltnp | grep -qE ':(80|443)\s' && ! ss -ltnp | grep -E ':(80|443)\s' | grep -q caddy; then
  echo "STOP: something else already uses port 80/443 on this server:"; ss -ltnp | grep -E ':(80|443)\s'; exit 1
fi
apt-get install -y caddy >/dev/null
printf 'User-agent: *\nDisallow: /\n' > /etc/caddy/robots.txt
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
    header X-Robots-Tag "noindex, nofollow"
    handle /robots.txt {
        root * /etc/caddy
        file_server
    }
    handle {
        reverse_proxy 127.0.0.1:8080
    }
}
CADDY
command -v ufw >/dev/null && ufw status | grep -q active && ufw allow 80,443/tcp >/dev/null || true
systemctl restart caddy
sleep 5
curl -s -o /dev/null -w "https://$DOMAIN -> HTTP %{http_code}\n" "https://$DOMAIN/login" || true
echo "done. Open https://$DOMAIN"
