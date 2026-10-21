#!/usr/bin/env bash
# Installs the NAS dashboard as a systemd service.
# Run from the project folder:  sudo ./install.sh
set -euo pipefail

APP_DIR=/opt/nasdash
CONF_DIR=/etc/nasdash
SVC_USER=nasdash
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo: sudo ./install.sh" >&2
  exit 1
fi

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq smartmontools hdparm python3-venv python3-pip i2c-tools python3-lgpio >/dev/null || \
  apt-get install -y -qq smartmontools hdparm python3-venv python3-pip i2c-tools >/dev/null

if [[ ! -e /dev/i2c-1 ]]; then
  echo "==> Enabling I2C"
  raspi-config nonint do_i2c 0 || echo "   Could not enable I2C automatically - use: sudo raspi-config"
fi

echo "==> Creating service user '$SVC_USER'"
if ! id "$SVC_USER" &>/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
fi
for g in i2c video gpio; do
  getent group "$g" >/dev/null && usermod -aG "$g" "$SVC_USER"
done

echo "==> Copying files to $APP_DIR"
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/nasdash"
cp -r "$SRC_DIR/nasdash" "$SRC_DIR/requirements.txt" "$APP_DIR/"

echo "==> Creating Python environment (this can take a few minutes)"
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  python3 -m venv --system-site-packages "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R root:root "$APP_DIR"

cat > /usr/local/bin/nasdash-passwd <<'EOS'
#!/bin/sh
# Change the NAS dashboard login.  Usage: sudo nasdash-passwd [--user NAME | --disable]
cd /opt/nasdash && exec /opt/nasdash/venv/bin/python -m nasdash.setpass --config /etc/nasdash/config.json "$@"
EOS
chmod 755 /usr/local/bin/nasdash-passwd

echo "==> Installing sudo rule (SMART read, spin-down, poweroff/reboot only)"
install -m 0440 "$SRC_DIR/deploy/sudoers" /etc/sudoers.d/nasdash
visudo -cf /etc/sudoers.d/nasdash >/dev/null

echo "==> Config in $CONF_DIR"
mkdir -p "$CONF_DIR"
chown "$SVC_USER:$SVC_USER" "$CONF_DIR"
chmod 700 "$CONF_DIR"

# Stop the service before touching the password, so it can't overwrite it.
systemctl stop nasdash 2>/dev/null || true

if ! grep -q '"hash": "pbkdf2' "$CONF_DIR/config.json" 2>/dev/null; then
  echo
  echo "==> Set a password for the web dashboard (user: admin)"
  /usr/local/bin/nasdash-passwd
fi
chown "$SVC_USER:$SVC_USER" "$CONF_DIR/config.json"
chmod 600 "$CONF_DIR/config.json"

echo "==> Installing and starting the service"
install -m 0644 "$SRC_DIR/deploy/nasdash.service" /etc/systemd/system/nasdash.service
systemctl daemon-reload
systemctl enable --now nasdash
systemctl restart nasdash
sleep 6

# "active" plus no restarts during the wait means it really came up.
if systemctl is-active --quiet nasdash && [[ "$(systemctl show -p NRestarts --value nasdash)" == "0" ]]; then
  PORT=$(python3 -c "import json;print(json.load(open('$CONF_DIR/config.json'))['server']['port'])" 2>/dev/null || echo 8080)
  IP=$(hostname -I | awk '{print $1}')
  echo
  echo "Done. Open http://$IP:$PORT"
else
  echo
  echo "The service did not start. See the log with: journalctl -u nasdash -n 50" >&2
  exit 1
fi
