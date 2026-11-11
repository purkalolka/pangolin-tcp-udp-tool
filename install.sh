#!/usr/bin/env bash
# Установка тулзы на сервер с Pangolin. Запускать от root на самом сервере.
# Не трогает живой Pangolin, только копирует файлы тулзы и поднимает
# её панель на 127.0.0.1:8090 (снаружи не светится).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
install -d /opt/pangolin-tool /etc/pangolin-tool
install -m 755 "$DIR/pangolin_tool.py" /opt/pangolin-tool/pangolin_tool.py
ln -sf /opt/pangolin-tool/pangolin_tool.py /usr/local/bin/pangolin-tool
if [ ! -f /etc/pangolin-tool/config.json ]; then
  cp "$DIR/config.example.json" /etc/pangolin-tool/config.json
  chmod 600 /etc/pangolin-tool/config.json
  echo "Создан /etc/pangolin-tool/config.json — заполни org_id, api_key, ui_token."
fi
# Токен для веб-панели: сгенерировать один раз, если в конфиге заглушка.
python3 - <<'PY'
import json, secrets, pathlib
p = pathlib.Path("/etc/pangolin-tool/config.json")
cfg = json.loads(p.read_text())
if not cfg.get("ui_token") or cfg["ui_token"].startswith("ЗАПОЛНИТЬ"):
    cfg["ui_token"] = secrets.token_urlsafe(24)
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    print("ui_token сгенерирован и записан в конфиг (в чат/репозиторий не попадает).")
PY
cat > /etc/systemd/system/pangolin-tool.service <<'UNIT'
[Unit]
Description=Pangolin raw TCP/UDP rules tool
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/pangolin-tool/pangolin_tool.py serve --bind 127.0.0.1 --port 8090
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now pangolin-tool.service
echo "Готово. CLI: pangolin-tool list"
echo "Панель: только через SSH-туннель: ssh -L 8090:127.0.0.1:8090 root@<vps> , затем http://127.0.0.1:8090/?token=<ui_token из /etc/pangolin-tool/config.json>"
