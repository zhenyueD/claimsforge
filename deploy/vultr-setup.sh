#!/usr/bin/env bash
# Vultr Ubuntu 24.04 one-shot installer for ClaimsForge.
# Usage on the VM (as root):
#   curl -fsSL https://raw.githubusercontent.com/zhenyueD/claimsforge/main/deploy/vultr-setup.sh | bash -s -- <GOOGLE_API_KEY> [<DOMAIN>]
# Or copy this file up and run: GOOGLE_API_KEY=... ./vultr-setup.sh
set -euo pipefail

GOOGLE_API_KEY="${1:-${GOOGLE_API_KEY:-}}"
DOMAIN="${2:-${DOMAIN:-}}"

if [[ -z "$GOOGLE_API_KEY" ]]; then
  echo "ERROR: pass GOOGLE_API_KEY as first arg or env var" >&2
  exit 1
fi

REPO_URL="https://github.com/zhenyueD/claimsforge.git"
INSTALL_DIR="/opt/claimsforge"
SERVICE_NAME="claimsforge"
PORT_INTERNAL=8000

echo "==> [1/7] System update + base deps"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl git nginx python3.12 python3.12-venv python3-pip ufw

echo "==> [2/7] Pull repo"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  cd "$INSTALL_DIR" && git pull --ff-only
else
  rm -rf "$INSTALL_DIR"
  git clone --depth=1 "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

echo "==> [3/7] Python venv + deps"
python3.12 -m venv .venv
.venv/bin/pip install --quiet -U pip
.venv/bin/pip install --quiet -r requirements.txt

echo "==> [4/7] Secrets file"
mkdir -p /etc/claimsforge
cat > /etc/claimsforge/env <<EOF
GOOGLE_API_KEY=${GOOGLE_API_KEY}
GEMINI_TEXT_MODEL=gemini-2.5-flash
GEMINI_VISION_MODEL=gemini-2.5-flash
PORT=${PORT_INTERNAL}
PYTHONUNBUFFERED=1
EOF
chmod 600 /etc/claimsforge/env

echo "==> [5/7] systemd unit"
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=ClaimsForge — multi-agent claims pipeline
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=/etc/claimsforge/env
ExecStart=${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/run.py
Restart=always
RestartSec=3
User=root

# logging
StandardOutput=journal
StandardError=journal

# basic sandboxing
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}
sleep 3
systemctl --no-pager status ${SERVICE_NAME} | head -20 || true

echo "==> [6/7] nginx reverse proxy"
SERVER_NAME="${DOMAIN:-_}"
cat > /etc/nginx/sites-available/claimsforge <<EOF
upstream cf_app { server 127.0.0.1:${PORT_INTERNAL}; }

server {
    listen 80 default_server;
    server_name ${SERVER_NAME};

    # client uploads max 6MB (Pillow handles 5MB cap inside app)
    client_max_body_size 6m;

    location / {
        proxy_pass http://cf_app;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # WebSocket upgrade
    location /ws {
        proxy_pass http://cf_app/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_read_timeout 86400;
    }
}
EOF
ln -sf /etc/nginx/sites-available/claimsforge /etc/nginx/sites-enabled/claimsforge
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "==> [7/7] firewall"
ufw --force enable
ufw allow OpenSSH
ufw allow 'Nginx Full'

echo
echo "============================================================"
echo "  ClaimsForge deployed successfully"
echo "  Health:  curl http://localhost/api/claimsforge/health"
echo "  Logs:    journalctl -u ${SERVICE_NAME} -f"
echo "============================================================"
