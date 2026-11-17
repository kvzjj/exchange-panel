#!/usr/bin/env bash
# 在目标机执行（需要 root）。假设代码已经在 /opt/leadmon。
set -euo pipefail

SRC="${1:-/opt/leadmon}"

if ! id -u leadmon >/dev/null 2>&1; then
  useradd --system --home-dir "$SRC" --shell /usr/sbin/nologin leadmon
fi

mkdir -p "$SRC/data"
chown -R root:root "$SRC"
chown -R leadmon:leadmon "$SRC/data"
# .secrets 里是推送凭据，只能 leadmon 读；注意别被上面的 chown -R root:root 覆盖
if [ -d "$SRC/.secrets" ]; then
  chown -R leadmon:leadmon "$SRC/.secrets"
  chmod 700 "$SRC/.secrets"
  find "$SRC/.secrets" -type f -exec chmod 600 {} \;
fi
# 配置里有带单 ID，给 leadmon 组读权限即可，不必对所有人可读
chown root:leadmon "$SRC/config.json"
chmod 640 "$SRC/config.json"

install -m 644 "$SRC/deploy/leadmon.service" /etc/systemd/system/leadmon.service
systemctl daemon-reload
systemctl enable leadmon
systemctl restart leadmon   # enable --now 不会重启已在跑的服务，升级后会跑旧代码
sleep 3
systemctl --no-pager --full status leadmon || true
