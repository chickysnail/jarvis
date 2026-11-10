#!/usr/bin/env bash
# One-time: create the named tunnel, point jarvis.chickysnail.blog at it, install user services.
# Requires: `cloudflared tunnel login` done (~/.cloudflared/cert.pem exists).
set -euo pipefail
CF=~/.local/bin/cloudflared
HOSTNAME=jarvis.chickysnail.blog
cd "$(dirname "$0")/.."
[ -f ~/.cloudflared/cert.pem ] || { echo "run: cloudflared tunnel login"; exit 1; }
$CF tunnel list -o json | grep -q '"name":"jarvis"' || $CF tunnel create jarvis
ID=$($CF tunnel list -o json | python3 -c 'import json,sys;print(next(t["id"] for t in json.load(sys.stdin) if t["name"]=="jarvis"))')
$CF tunnel route dns --overwrite-dns jarvis "$HOSTNAME"
sed -e "s#TUNNEL_ID#$ID#g" -e "s#HOME_DIR#$HOME#g" deploy/cloudflared.yml.template > ~/.cloudflared/config.yml
mkdir -p ~/.config/systemd/user
cp deploy/jarvis.service deploy/jarvis-tunnel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now jarvis.service jarvis-tunnel.service
echo "tunnel $ID -> https://$HOSTNAME"
