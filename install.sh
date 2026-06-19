#!/usr/bin/env bash
# pi-z2-display-hat-mini をデバイス(pi-z2)上に導入する。
# 前提: このリポジトリが ~/pi-z2-display-hat-mini に配置済み。
#       venv ~/.venvs/displayhatmini と ~/mbgl-render / ~/mbgl-libs が用意済み。
set -euo pipefail

DEST="$HOME/pi-z2-display-hat-mini"
VENV="$HOME/.venvs/displayhatmini"

echo "== Python 依存を導入 =="
"$VENV/bin/pip" install --quiet -r "$DEST/requirements.txt"

echo "== pi-map / pi-maps / pi-screensaver / pi-saver を /usr/local/bin へ =="
sudo install -m 0755 "$DEST/bin/pi-map" /usr/local/bin/pi-map
sudo install -m 0755 "$DEST/bin/pi-maps" /usr/local/bin/pi-maps
sudo install -m 0755 "$DEST/bin/pi-screensaver" /usr/local/bin/pi-screensaver
sudo install -m 0755 "$DEST/bin/pi-saver" /usr/local/bin/pi-saver

echo "== systemd サービスを導入・有効化 =="
sudo install -m 0644 "$DEST/systemd/pi-display.service" /etc/systemd/system/pi-display.service
sudo systemctl daemon-reload
sudo systemctl enable --now pi-display.service

echo "== 完了。状態: =="
systemctl --no-pager status pi-display.service | head -5 || true
echo "ログ: journalctl -u pi-display -f"
