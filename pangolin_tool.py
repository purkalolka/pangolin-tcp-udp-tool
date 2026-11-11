#!/usr/bin/env python3
"""pangolin-tcp-udp-tool — автоматизация raw TCP/UDP правил для Pangolin.

Зачем: в Pangolin каждый новый сырой порт — это 4 ручных шага, и один
пропущенный шаг молча ломает проброс:
  1. ресурс Raw TCP/UDP в панели (Integration API),
  2. порт у Gerbil в docker-compose.yml,
  3. entrypoint tcp-<port> / udp-<port> в traefik_config.yml,
  4. рестарт Gerbil + Traefik и проверка слушателя ВНУТРИ Gerbil
     (docker exec gerbil ss), а не снаружи — docker-proxy принимает
     TCP сам, и внешний nc врёт.

Тулза делает все 4 шага одной командой, а удаление — в обратную сторону.
UDP поддерживается наравне с TCP: Pangolin 1.26 сам умеет raw UDP
(ресурс mode=udp + entrypoint udp-<port> + порт Gerbil .../udp).

Только стандартная библиотека Python 3. Никаких pip-зависимостей.

Использование (на сервере с Pangolin, от root):
  pangolin-tool sites
  pangolin-tool list
  pangolin-tool add --name rdp-pc1 --protocol tcp --public-port 29378 \\
      --site wsl --target-ip 127.0.0.1 --target-port 3389
  pangolin-tool add --name rdp-pc1-udp --protocol udp --public-port 29378 \\
      --site wsl --target-ip 127.0.0.1 --target-port 3389
  pangolin-tool delete --name rdp-pc1
  pangolin-tool check [--name rdp-pc1]
  pangolin-tool sync
  pangolin-tool serve --bind 127.0.0.1 --port 8090

Конфиг: /etc/pangolin-tool/config.json (создаётся из config.example.json,
API-ключ и org_id — только на сервере, никогда не коммитятся).
Состояние правил: /etc/pangolin-tool/rules.json — что именно тулза
создала, чтобы удаление точно убирало те же самые записи.
"""

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

VERSION = "1.4.0"

DEFAULT_PANGOLIN_DIR = "/opt/pangolin"
DEFAULT_CONFIG = "/etc/pangolin-tool/config.json"
DEFAULT_STATE = "/etc/pangolin-tool/rules.json"

# Порты, которые нельзя занимать правилом: сам Pangolin/Traefik/Gerbil.
RESERVED_PORTS = {22, 80, 443, 3000, 3001, 3002, 3003, 51820, 21820}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class ToolError(Exception):
    pass


# ---------------------------------------------------------------- config
def load_config(path=DEFAULT_CONFIG):
    if not os.path.exists(path):
        raise ToolError(
            f"нет конфига {path}. Скопируй config.example.json, "
            "впиши org_id и api_key (Integration API, root key) и повтори."
        )
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("org_id", "api_key"):
        if not cfg.get(key):
            raise ToolError(f"в конфиге {path} не заполнен {key}")
    cfg.setdefault("api_base", "http://127.0.0.1:3003/v1")
    cfg.setdefault("pangolin_dir", DEFAULT_PANGOLIN_DIR)
    cfg.setdefault("state_file", DEFAULT_STATE)
    cfg.setdefault("ui_token", "")
    return cfg


def load_state(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def save_state(path, rules):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rules, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- validation
def validate_rule(name, protocol, public_port, target_ip, target_port):
    if not NAME_RE.match(name or ""):
        raise ToolError(
            "имя правила: только латиница/цифры/_/-, начинать с буквы или цифры"
        )
    if protocol not in ("tcp", "udp"):
        raise ToolError("protocol: только tcp или udp")
    for label, port in (("public_port", public_port), ("target_port", target_port)):
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ToolError(f"{label}: порт 1-65535")
    if public_port in RESERVED_PORTS:
        raise ToolError(
            f"порт {public_port} занят самим Pangolin/Traefik/Gerbil, выбери другой"
        )
    try:
        ipaddress.ip_address(target_ip)
    except ValueError:
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9.-]{0,253}$", target_ip or ""):
            raise ToolError(f"target_ip: не IP и не hostname: {target_ip!r}")


# ---------------------------------------------------------------- Pangolin Integration API
class PangolinApi:
    """Bearer-ключ Integration API. База — локальный 127.0.0.1:3003/v1,
    поэтому ключ не светится в интернете. См. docs: Integration API
    включается флагом enable_integration_api, порт 3003 проброшен
    на 127.0.0.1 в docker-compose.yml Pangolin."""

    def __init__(self, api_base, api_key, org_id):
        self.base = api_base.rstrip("/")
        self.key = api_key
        self.org_id = org_id

    def _req(self, method, path, body=None):
        req = urllib.request.Request(
            self.base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise ToolError(f"Pangolin API {method} {path}: HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            raise ToolError(
                f"Pangolin API недоступен ({self.base}): {e}. "
                "Проверь, что Integration API включён и порт 3003 проброшен на 127.0.0.1."
            )
        if isinstance(payload, dict) and payload.get("error"):
            raise ToolError(f"Pangolin API {method} {path}: {payload.get('message')}")
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def list_sites(self):
        data = self._req("GET", f"/org/{self.org_id}/sites?pageSize=1000&page=1")
        return data.get("sites", []) if isinstance(data, dict) else data

    def list_resources(self):
        data = self._req("GET", f"/org/{self.org_id}/resources?pageSize=1000&page=1")
        return data.get("resources", []) if isinstance(data, dict) else data

    def create_resource(self, name, protocol, public_port):
        # Pangolin 1.26: mode tcp/udp. Старый http/protocol — fallback.
        try:
            return self._req(
                "PUT", f"/org/{self.org_id}/resource",
                {"name": name, "mode": protocol, "proxyPort": public_port},
            )
        except ToolError as e:
            if "400" not in str(e):
                raise
            return self._req(
                "PUT", f"/org/{self.org_id}/resource",
                {"name": name, "http": False, "protocol": protocol,
                 "proxyPort": public_port},
            )

    def delete_resource(self, resource_id):
        return self._req("DELETE", f"/resource/{resource_id}")

    def create_target(self, resource_id, site_id, ip, port, protocol):
        return self._req(
            "PUT", f"/resource/{resource_id}/target",
            {"siteId": site_id, "ip": ip, "port": port,
             "enabled": True, "mode": protocol},
        )

    def list_targets(self, resource_id):
        data = self._req("GET", f"/resource/{resource_id}/targets")
        return data.get("targets", []) if isinstance(data, dict) else data

    def pick_site_defaults(self):
        return self._req("GET", f"/org/{self.org_id}/pick-site-defaults")

    def create_site(self, name):
        """Минимальное тело — лишние поля (pubKey/address) дают 400.
        Секрет Newt возвращается только в pick-site-defaults, а не
        в ответе создания, поэтому его надо сохранить из defaults."""
        d = self.pick_site_defaults()
        site = self._req("PUT", f"/org/{self.org_id}/site",
                         {"name": name, "type": "newt",
                          "exitNodeId": d["exitNodeId"], "subnet": d["subnet"],
                          "newtId": d["newtId"], "secret": d["newtSecret"]})
        return site, d

    def delete_site(self, site_id):
        return self._req("DELETE", f"/site/{site_id}")


def resolve_site(api, site_ref):
    """site_ref — id (число) или имя/niceId сайта."""
    sites = api.list_sites()
    for s in sites:
        if str(s.get("siteId")) == str(site_ref) or s.get("name") == site_ref \
                or s.get("niceId") == site_ref:
            return s
    names = ", ".join(s.get("name", "?") for s in sites) or "сайтов нет вообще"
    raise ToolError(f"сайт {site_ref!r} не найден. Есть: {names}")


# ---------------------------------------------------------------- OS layer: файлы Pangolin
def _backup(path):
    bak = path + ".bak"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    ts_bak = f"{path}.{datetime.now(timezone.utc):%Y%m%d%H%M%S}.bak"
    shutil.copy2(path, ts_bak)
    return bak


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def ensure_raw_flag(pangolin_dir):
    """allow_raw_resources: true — один раз, не на каждый порт."""
    cfg_path = os.path.join(pangolin_dir, "config", "config.yml")
    text = _read(cfg_path)
    if "allow_raw_resources" in text:
        return False
    _backup(cfg_path)
    if "flags:" in text:
        text = text.replace("flags:", "flags:\n    allow_raw_resources: true", 1)
    else:
        text = text.rstrip("\n") + "\nflags:\n    allow_raw_resources: true\n"
    _write(cfg_path, text)
    return True


def gerbil_port_line(port, protocol):
    return f"      - {port}:{port}/udp" if protocol == "udp" else f"      - {port}:{port}"


def add_gerbil_port(pangolin_dir, port, protocol):
    path = os.path.join(pangolin_dir, "docker-compose.yml")
    text = _read(path)
    line = gerbil_port_line(port, protocol)
    existing = {l.strip() for l in text.splitlines()}
    if line.strip() in existing:
        return False
    _backup(path)
    lines = text.splitlines(keepends=True)
    anchor = next((i for i, l in enumerate(lines) if "443:443/udp" in l), None)
    if anchor is not None:
        lines.insert(anchor + 1, line + "\n")
    else:
        ports_idx = next((i for i, l in enumerate(lines)
                          if l.strip() == "ports:"), None)
        if ports_idx is None:
            raise ToolError("в docker-compose.yml не найден блок ports у Gerbil")
        lines.insert(ports_idx + 1, line + "\n")
    _write(path, "".join(lines))
    return True


def remove_gerbil_port(pangolin_dir, port, protocol):
    path = os.path.join(pangolin_dir, "docker-compose.yml")
    text = _read(path)
    target = gerbil_port_line(port, protocol).strip()
    lines = [l for l in text.splitlines(keepends=True)
             if l.strip() != target]
    if len(lines) == len(text.splitlines(keepends=True)):
        return False
    _backup(path)
    _write(path, "".join(lines))
    return True


def entrypoint_name(port, protocol):
    return f"{protocol}-{port}"


def add_traefik_entrypoint(pangolin_dir, port, protocol):
    path = os.path.join(pangolin_dir, "config", "traefik", "traefik_config.yml")
    text = _read(path)
    name = entrypoint_name(port, protocol)
    if f"{name}:" in text:
        return False
    _backup(path)
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines)
                  if l.startswith("entryPoints:")), None)
    if start is None:
        raise ToolError("в traefik_config.yml нет блока entryPoints:")
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() and not lines[i][0].isspace()), len(lines))
    block = f'  {name}:\n    address: ":{port}/{protocol}"\n'
    lines.insert(end, block)
    _write(path, "".join(lines))
    return True


def remove_traefik_entrypoint(pangolin_dir, port, protocol):
    path = os.path.join(pangolin_dir, "config", "traefik", "traefik_config.yml")
    text = _read(path)
    name = entrypoint_name(port, protocol)
    lines = text.splitlines(keepends=True)
    out, removed, skipping = [], False, False
    for l in lines:
        if l.startswith(f"  {name}:"):
            skipping, removed = True, True
            continue
        if skipping:
            if l.startswith("    "):
                continue
            skipping = False
        out.append(l)
    if not removed:
        return False
    _backup(path)
    _write(path, "".join(out))
    return True


def os_ports(pangolin_dir):
    """Какие сырые порты уже прописаны в Gerbil + Traefik (без API)."""
    compose = _read(os.path.join(pangolin_dir, "docker-compose.yml"))
    traefik = _read(os.path.join(pangolin_dir, "config", "traefik",
                                "traefik_config.yml"))
    found = set()
    for m in re.finditer(r"-\s+(\d+):(\d+)(?:/(tcp|udp))?", compose):
        if m.group(1) == m.group(2):
            found.add((int(m.group(1)), m.group(3) or "tcp"))
    for m in re.finditer(r"(tcp|udp)-(\d+):", traefik):
        found.add((int(m.group(2)), m.group(1)))
    return sorted(found)


def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise ToolError(f"команда провалилась: {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    return r.stdout


def restart_stack(pangolin_dir):
    """Entrypoint — статика Traefik, порт Gerbil — статика Docker.
    Без пересоздания обоих ничего не подхватится."""
    run(["docker", "compose", "up", "-d"], cwd=pangolin_dir)
    run(["docker", "compose", "up", "-d", "--force-recreate", "traefik"],
        cwd=pangolin_dir)
    for _ in range(30):
        time.sleep(2)
        try:
            out = run(["docker", "compose", "ps", "--format", "{{.Name}} {{.Status}}"],
                      cwd=pangolin_dir)
            if "traefik" in out and "Up" in out:
                return
        except ToolError:
            pass
    raise ToolError("Traefik не поднялся после рестарта, смотри docker compose logs traefik")


def verify_listener(port, protocol):
    """Главный признак: порт слушается ВНУТРИ Gerbil. Внешний nc не
    считается — docker-proxy принимает TCP сам."""
    flag = "-tlnp" if protocol == "tcp" else "-ulnp"
    out = run(["docker", "exec", "gerbil", "ss", flag])
    if f":{port} " not in out and f"*:{port}" not in out \
            and f"0.0.0.0:{port}" not in out:
        raise ToolError(
            f"внутри Gerbil порт {port}/{protocol} НЕ слушается. "
            "Смотри docker compose logs traefik и проверяй entrypoint."
        )
    return True


def firewall_hint(port, protocol):
    """ОС тут обычно ничего не блокирует (ufw как правило нет), но если
    ufw активен — открыть порт в нём же."""
    try:
        r = subprocess.run(["ufw", "status"], capture_output=True,
                           text=True, timeout=10)
        if r.returncode == 0 and "Status: active" in r.stdout:
            run(["ufw", "allow", f"{port}/{protocol}"])
            return f"ufw: открыт {port}/{protocol}"
    except (FileNotFoundError, ToolError):
        pass
    return (f"ОС-фаервол: правил нет (ufw не активен/не установлен). "
            f"Если у провайдера VPS есть свой security group — открой там "
            f"{port}/{protocol}, как открыты 80/443/51820/21820.")

# ---------------------------------------------------------------- операции
def port_provisioned(pangolin_dir, port, protocol):
    """Порт заранее заведён: entrypoint есть в Traefik И порт опубликован
    у Gerbil. Тогда Pangolin подхватывает ресурс динамически, без рестарта
    стека — проверено живьём 2026-11-11 на порту 29400."""
    compose = _read(os.path.join(pangolin_dir, "docker-compose.yml"))
    traefik = _read(os.path.join(pangolin_dir, "config", "traefik",
                                "traefik_config.yml"))
    if protocol == "tcp":
        in_compose = re.search(rf"-\s+{port}:{port}(?!/udp)\b",
                                compose) is not None
    else:
        in_compose = re.search(rf"-\s+{port}:{port}/udp\b", compose) is not None
    in_traefik = f"{entrypoint_name(port, protocol)}:" in traefik
    return in_compose and in_traefik


def do_range(cfg, start, end):
    """Заранее завести диапазон портов (один рестарт на весь диапазон),
    после чего add/delete внутри него идут без рестарта вообще."""
    if not (1 <= start <= 65535 and 1 <= end <= 65535 and start <= end):
        raise ToolError("range: нужны --start и --end, 1..65535, start <= end")
    if end - start + 1 > 100:
        raise ToolError("range: максимум 100 портов за раз — каждый "
                        "опубликованный порт это отдельный docker-proxy. "
                        "Разбей на несколько вызовов.")
    for port in range(start, end + 1):
        for proto in ("tcp", "udp"):
            add_gerbil_port(cfg["pangolin_dir"], port, proto)
            add_traefik_entrypoint(cfg["pangolin_dir"], port, proto)
    restart_stack(cfg["pangolin_dir"])
    verify_listener(start, "tcp")
    verify_listener(end, "tcp")
    path = cfg.get("config_path", DEFAULT_CONFIG)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    ranges = [tuple(r) for r in data.get("port_ranges", [])]
    ranges.append((start, end))
    data["port_ranges"] = [list(r) for r in sorted(set(ranges))]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.chmod(path, 0o600)
    return {"range": [start, end],
            "ports": end - start + 1,
            "note": "теперь add/delete внутри диапазона идут без рестарта"}


def do_add(cfg, name, protocol, public_port, site_ref, target_ip, target_port,
           dry_run=False, no_restart=False):
    validate_rule(name, protocol, public_port, target_ip, target_port)
    state = load_state(cfg["state_file"])
    if any(r["name"] == name for r in state):
        raise ToolError(f"правило {name!r} уже есть в состоянии тулзы (list покажет)")
    if any(r["public_port"] == public_port and r["protocol"] == protocol
           for r in state):
        raise ToolError(f"порт {public_port}/{protocol} уже занят другим правилом")
    pangolin_dir = cfg["pangolin_dir"]
    api = PangolinApi(cfg["api_base"], cfg["api_key"], cfg["org_id"])
    site = resolve_site(api, site_ref)
    # 0. Порт уже занят в ОС кем-то другим? Заранее заведённый диапазон
    # (range) — не занятость, а готовность: тогда рестарт не нужен.
    if (public_port, protocol) in os_ports(pangolin_dir) \
            and not port_provisioned(pangolin_dir, public_port, protocol):
        raise ToolError(
            f"порт {public_port}/{protocol} уже прописан в Gerbil/Traefik. "
            "Сделай sync, чтобы забрать его в состояние, или выбери другой порт."
        )
    plan = [
        f"Pangolin: ресурс {name!r} mode={protocol} proxyPort={public_port}",
        f"Pangolin: target site={site.get('name')} ({site.get('siteId')}) "
        f"-> {target_ip}:{target_port}",
        f"Gerbil: порт {gerbil_port_line(public_port, protocol).strip()}",
        f"Traefik: entrypoint {entrypoint_name(public_port, protocol)} "
        f"address :{public_port}/{protocol}",
        "Рестарт Gerbil+Traefik, проверка слушателя внутри Gerbil",
    ]
    if dry_run:
        return {"dry_run": True, "plan": plan}
    resource = api.create_resource(name, protocol, public_port)
    resource_id = resource.get("resourceId") or resource.get("id")
    if not resource_id:
        raise ToolError(f"Pangolin не вернул resourceId: {resource}")
    try:
        api.create_target(resource_id, site["siteId"], target_ip,
                          target_port, protocol)
    except ToolError:
        api.delete_resource(resource_id)
        raise
    changed_flag = ensure_raw_flag(pangolin_dir)
    add_gerbil_port(pangolin_dir, public_port, protocol)
    add_traefik_entrypoint(pangolin_dir, public_port, protocol)
    provisioned = port_provisioned(pangolin_dir, public_port, protocol)
    if no_restart and not provisioned:
        # Порт в файлах есть, но Gerbil/Traefik его ещё не подхватили.
        # Работать начнёт только после `apply` — один рестарт на все правки.
        pending = True
    else:
        if not provisioned:
            restart_stack(pangolin_dir)
        # проверка слушателя — без рестарта, просто чтение ss внутри Gerbil
        verify_listener(public_port, protocol)
        pending = False
    fw = firewall_hint(public_port, protocol)
    rule = {
        "name": name, "protocol": protocol, "public_port": public_port,
        "site_id": site["siteId"], "site_name": site.get("name"),
        "target_ip": target_ip, "target_port": target_port,
        "resource_id": resource_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    state.append(rule)
    save_state(cfg["state_file"], state)
    return {"rule": rule, "raw_flag_added": changed_flag, "firewall": fw,
            "pending_restart": pending}


def do_delete(cfg, name=None, public_port=None, protocol=None, dry_run=False,
              no_restart=False):
    state = load_state(cfg["state_file"])
    rule = None
    if name:
        rule = next((r for r in state if r["name"] == name), None)
    elif public_port:
        rule = next((r for r in state
                     if r["public_port"] == public_port
                     and (protocol is None or r["protocol"] == protocol)), None)
    if not rule:
        raise ToolError("правило не найдено в состоянии тулзы. "
                        "list — посмотреть, sync — забрать существующие.")
    pangolin_dir = cfg["pangolin_dir"]
    plan = [
        f"Pangolin: удалить ресурс id={rule['resource_id']} ({rule['name']})",
        f"Gerbil: убрать порт {rule['public_port']}",
        f"Traefik: убрать entrypoint "
        f"{entrypoint_name(rule['public_port'], rule['protocol'])}",
        "Рестарт Gerbil+Traefik, проверка что порт закрылся",
    ]
    if dry_run:
        return {"dry_run": True, "plan": plan, "rule": rule}
    api = PangolinApi(cfg["api_base"], cfg["api_key"], cfg["org_id"])
    try:
        api.delete_resource(rule["resource_id"])
    except ToolError as e:
        if "404" not in str(e):
            raise
    # Порт из заранее заведённого диапазона принадлежит диапазону, а не
    # правилу: файлы не трогаем, порт остаётся слушаться для следующих правил.
    provisioned = port_provisioned(pangolin_dir, rule["public_port"],
                                   rule["protocol"])
    if provisioned:
        save_state(cfg["state_file"],
                   [r for r in state if r["name"] != rule["name"]])
        return {"deleted": rule, "pending_restart": False,
                "note": "порт остался в диапазоне и слушается, готов к "
                        "следующему правилу без рестарта"}
    remove_gerbil_port(pangolin_dir, rule["public_port"], rule["protocol"])
    remove_traefik_entrypoint(pangolin_dir, rule["public_port"], rule["protocol"])
    if no_restart:
        save_state(cfg["state_file"],
                   [r for r in state if r["name"] != rule["name"]])
        return {"deleted": rule, "pending_restart": True}
    restart_stack(pangolin_dir)
    try:
        verify_listener(rule["public_port"], rule["protocol"])
        raise ToolError(f"порт {rule['public_port']} всё ещё слушается после удаления")
    except ToolError as e:
        if "НЕ слушается" not in str(e):
            raise
    save_state(cfg["state_file"],
               [r for r in state if r["name"] != rule["name"]])
    return {"deleted": rule, "pending_restart": False}


def do_apply(cfg):
    """Один рестарт на все накопленные правки + проверка слушателей."""
    state = load_state(cfg["state_file"])
    restart_stack(cfg["pangolin_dir"])
    results = []
    for r in state:
        try:
            verify_listener(r["public_port"], r["protocol"])
            results.append({"name": r["name"], "protocol": r["protocol"],
                            "public_port": r["public_port"], "ok": True})
        except ToolError as e:
            results.append({"name": r["name"], "protocol": r["protocol"],
                            "public_port": r["public_port"], "ok": False,
                            "error": str(e)})
    return {"applied": results,
            "all_ok": all(x["ok"] for x in results)}


def do_check(cfg, name=None):
    state = load_state(cfg["state_file"])
    rules = [r for r in state if name is None or r["name"] == name]
    if name and not rules:
        raise ToolError(f"правило {name!r} не найдено")
    api = PangolinApi(cfg["api_base"], cfg["api_key"], cfg["org_id"])
    api_resources = {r.get("resourceId"): r for r in api.list_resources()}
    results = []
    for rule in rules:
        res = api_resources.get(rule["resource_id"])
        try:
            verify_listener(rule["public_port"], rule["protocol"])
            listening = True
        except ToolError:
            listening = False
        results.append({
            "name": rule["name"], "protocol": rule["protocol"],
            "public_port": rule["public_port"],
            "target": f"{rule['target_ip']}:{rule['target_port']}",
            "site": rule.get("site_name"),
            "in_pangolin": res is not None,
            "listening_in_gerbil": listening,
            "ok": res is not None and listening,
        })
    return results


def do_sync(cfg):
    """Забрать в состояние то, что уже создано руками в панели/ОС."""
    api = PangolinApi(cfg["api_base"], cfg["api_key"], cfg["org_id"])
    state = load_state(cfg["state_file"])
    known = {(r["public_port"], r["protocol"]) for r in state}
    added = []
    for res in api.list_resources():
        proxy_port = res.get("proxyPort")
        mode = res.get("mode") or res.get("protocol")
        if not proxy_port or mode not in ("tcp", "udp"):
            continue
        if (proxy_port, mode) in known:
            continue
        targets = api.list_targets(res.get("resourceId"))
        t = targets[0] if targets else {}
        rule = {
            "name": res.get("name", f"raw-{proxy_port}"),
            "protocol": mode, "public_port": proxy_port,
            "site_id": t.get("siteId"), "site_name": None,
            "target_ip": t.get("ip", ""), "target_port": t.get("port"),
            "resource_id": res.get("resourceId"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "synced": True,
        }
        state.append(rule)
        added.append(rule)
        known.add((proxy_port, mode))
    save_state(cfg["state_file"], state)
    return {"added": added, "os_ports": os_ports(cfg["pangolin_dir"])}


# ---------------------------------------------------------------- сайты
def do_site_create(cfg, name):
    """Создать сайт Newt и выдать готовую команду для ноды: один раз
    вставил — работает, никаких файлов перекидывать не надо.
    Команда содержит секрет Newt, поэтому она печатается только
    в терминал на сервере — не в чат и не в репозиторий. .env-файл
    на сервере остаётся как запасной вариант."""
    if not NAME_RE.match(name or ""):
        raise ToolError("имя сайта: только латиница/цифры/_/-")
    api = PangolinApi(cfg["api_base"], cfg["api_key"], cfg["org_id"])
    site, d = api.create_site(name)
    sites_dir = os.path.join(os.path.dirname(cfg["state_file"]), "sites")
    os.makedirs(sites_dir, exist_ok=True)
    env_path = os.path.join(sites_dir, f"{name}.env")
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(f"PANGOLIN_ENDPOINT=https://{d.get('endpoint')}\n")
        f.write(f"NEWT_ID={d['newtId']}\n")
        f.write(f"NEWT_SECRET={d['newtSecret']}\n")
    os.chmod(env_path, 0o600)
    cmd = ("docker run -d --name newt --network host --cap-add NET_ADMIN "
           "--restart unless-stopped "
           f"-e PANGOLIN_ENDPOINT=https://{d.get('endpoint')} "
           f"-e NEWT_ID={d['newtId']} -e NEWT_SECRET={d['newtSecret']} "
           "fosrl/newt:latest")
    return {"site_id": site.get("siteId"), "name": site.get("name"),
            "subnet": site.get("subnet") or d.get("subnet"),
            "credentials_file": env_path,
            "connect_command": cmd}


def do_site_delete(cfg, name=None, site_id=None):
    api = PangolinApi(cfg["api_base"], cfg["api_key"], cfg["org_id"])
    if site_id is None:
        site = resolve_site(api, name)
        site_id = site["siteId"]
        name = site.get("name")
    api.delete_site(site_id)
    env_path = os.path.join(os.path.dirname(cfg["state_file"]), "sites",
                            f"{name}.env") if name else None
    if env_path and os.path.exists(env_path):
        os.remove(env_path)
    return {"deleted_site_id": site_id, "name": name}


# ---------------------------------------------------------------- веб-панель
HTML_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Pangolin raw TCP/UDP</title><style>
body{font-family:system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 16px}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:6px 8px;text-align:left}
input,select,button{padding:6px;margin:2px}form{margin:16px 0;padding:12px;border:1px solid #ccc}
.ok{color:green}.bad{color:red}</style></head><body>
<h1>Pangolin: raw TCP/UDP правила</h1>
<p>Один порт = одна нода. Для RDP: публичный порт → 127.0.0.1:3389 ноды.</p>
<div id="rules">Загрузка…</div>
<form id="add"><h3>Новое правило</h3>
<input name="name" placeholder="имя, напр. rdp-pc2" required>
<select name="protocol"><option value="tcp">TCP</option><option value="udp">UDP</option></select>
<input name="public_port" type="number" placeholder="публичный порт" required>
<input name="site" placeholder="сайт (имя или id)" required>
<input name="target_ip" value="127.0.0.1" required>
<input name="target_port" type="number" placeholder="порт ноды" required>
<button>Создать</button></form>
<pre id="out"></pre>
<script>
const tok = new URLSearchParams(location.search).get('token') || '';
async function api(p, o={}) {
  const r = await fetch(p + (p.includes('?')?'&':'?') + 'token=' + encodeURIComponent(tok), o);
  const j = await r.json(); if (!r.ok) throw new Error(j.error || r.status); return j;
}
async function load() {
  try {
    const d = await api('/api/rules');
    document.getElementById('rules').innerHTML =
      '<table><tr><th>Имя</th><th>Протокол</th><th>Порт</th><th>Куда</th><th>Сайт</th><th></th></tr>' +
      d.rules.map(r => `<tr><td>${r.name}</td><td>${r.protocol}</td><td>${r.public_port}</td><td>${r.target_ip}:${r.target_port}</td><td>${r.site_name||''}</td><td><button onclick="del('${r.name}')">Удалить</button></td></tr>`).join('') + '</table>';
  } catch(e) { document.getElementById('rules').textContent = 'Ошибка: ' + e.message; }
}
async function del(name) {
  if (!confirm('Удалить правило ' + name + '? Порт закроется.')) return;
  try { await api('/api/rules?name=' + encodeURIComponent(name), {method:'DELETE'}); load(); }
  catch(e) { document.getElementById('out').textContent = 'Ошибка: ' + e.message; }
}
document.getElementById('add').onsubmit = async ev => {
  ev.preventDefault();
  const f = new FormData(ev.target); const b = {};
  f.forEach((v,k) => b[k] = (k.includes('port') ? parseInt(v,10) : v));
  try { const r = await api('/api/rules', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(b)});
    document.getElementById('out').textContent = JSON.stringify(r, null, 2); load(); }
  catch(e) { document.getElementById('out').textContent = 'Ошибка: ' + e.message; }
};
load();
</script></body></html>
"""


def serve(cfg, bind, port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    token = cfg.get("ui_token", "")
    if not token:
        raise ToolError("в конфиге нет ui_token — сгенерируй и впиши, без него панель не стартует")

    class H(BaseHTTPRequestHandler):
        def _auth(self):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            return q.get("token", [""])[0] == token

        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/?"):
                return self._send(200, HTML_PAGE, "text/html; charset=utf-8")
            if not self._auth():
                return self._send(401, json.dumps({"error": "bad token"}))
            if self.path.startswith("/api/rules"):
                return self._send(200, json.dumps({"rules": load_state(cfg["state_file"])}))
            if self.path.startswith("/api/sites"):
                api = PangolinApi(cfg["api_base"], cfg["api_key"], cfg["org_id"])
                return self._send(200, json.dumps({"sites": api.list_sites()}))
            return self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            if not self._auth():
                return self._send(401, json.dumps({"error": "bad token"}))
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            try:
                res = do_add(cfg, body["name"], body["protocol"],
                             int(body["public_port"]), body["site"],
                             body.get("target_ip", "127.0.0.1"),
                             int(body["target_port"]))
                self._send(200, json.dumps(res, ensure_ascii=False))
            except ToolError as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False))

        def do_DELETE(self):
            from urllib.parse import urlparse, parse_qs
            if not self._auth():
                return self._send(401, json.dumps({"error": "bad token"}))
            name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
            try:
                res = do_delete(cfg, name=name)
                self._send(200, json.dumps(res, ensure_ascii=False))
            except ToolError as e:
                self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False))

        def log_message(self, *a):
            pass

    print(f"панель: http://{bind}:{port}/?token=... (токен только в конфиге, не в логах)")
    ThreadingHTTPServer((bind, port), H).serve_forever()


# ---------------------------------------------------------------- CLI
def main(argv=None):
    p = argparse.ArgumentParser(prog="pangolin-tool")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sites", help="список сайтов Newt")
    sub.add_parser("list", help="список правил тулзы")
    a = sub.add_parser("add", help="создать правило")
    a.add_argument("--name", required=True)
    a.add_argument("--protocol", choices=["tcp", "udp"], required=True)
    a.add_argument("--public-port", type=int, required=True)
    a.add_argument("--site", required=True, help="имя или id сайта")
    a.add_argument("--target-ip", default="127.0.0.1")
    a.add_argument("--target-port", type=int, required=True)
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--no-restart", action="store_true",
                   help="не рестартить стек сейчас, применить позже через apply")
    d = sub.add_parser("delete", help="удалить правило")
    d.add_argument("--name")
    d.add_argument("--public-port", type=int)
    d.add_argument("--protocol", choices=["tcp", "udp"])
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--no-restart", action="store_true",
                   help="не рестартить стек сейчас, применить позже через apply")
    sub.add_parser("apply", help="один рестарт стека на все правки + проверка")
    rg = sub.add_parser("range", help="заранее завести диапазон портов: один рестарт, потом add/delete без рестартов")
    rg.add_argument("--start", type=int, required=True)
    rg.add_argument("--end", type=int, required=True)
    c = sub.add_parser("check", help="проверить слои правила")
    c.add_argument("--name")
    sub.add_parser("sync", help="забрать уже созданные руками ресурсы")
    sc = sub.add_parser("site-create", help="создать сайт Newt для ноды")
    sc.add_argument("--name", required=True)
    sd = sub.add_parser("site-delete", help="удалить сайт Newt")
    sd.add_argument("--name")
    sd.add_argument("--id", type=int)
    s = sub.add_parser("serve", help="веб-панель")
    s.add_argument("--bind", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8090)

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    cfg["config_path"] = args.config
    try:
        if args.cmd == "sites":
            api = PangolinApi(cfg["api_base"], cfg["api_key"], cfg["org_id"])
            for s in api.list_sites():
                print(f"{s.get('siteId')}\t{s.get('name')}\t"
                      f"online={s.get('online')}\tsubnet={s.get('subnet')}")
        elif args.cmd == "list":
            rules = load_state(cfg["state_file"])
            if not rules:
                print("правил нет. add — создать, sync — забрать существующие.")
            for r in rules:
                print(f"{r['name']}\t{r['protocol']}\t{r['public_port']}\t"
                      f"-> {r['target_ip']}:{r['target_port']}\t"
                      f"site={r.get('site_name')}\tresource={r.get('resource_id')}")
            print(f"ОС: прописаны порты {os_ports(cfg['pangolin_dir'])}")
        elif args.cmd == "add":
            res = do_add(cfg, args.name, args.protocol, args.public_port,
                         args.site, args.target_ip, args.target_port,
                         dry_run=args.dry_run, no_restart=args.no_restart)
            print(json.dumps(res, indent=2, ensure_ascii=False))
        elif args.cmd == "delete":
            if not args.name and not args.public_port:
                raise ToolError("delete: нужен --name или --public-port")
            res = do_delete(cfg, name=args.name, public_port=args.public_port,
                            protocol=args.protocol, dry_run=args.dry_run,
                            no_restart=args.no_restart)
            print(json.dumps(res, indent=2, ensure_ascii=False))
        elif args.cmd == "apply":
            print(json.dumps(do_apply(cfg), indent=2, ensure_ascii=False))
        elif args.cmd == "range":
            print(json.dumps(do_range(cfg, args.start, args.end),
                             indent=2, ensure_ascii=False))
        elif args.cmd == "check":
            for r in do_check(cfg, args.name):
                mark = "OK" if r["ok"] else "FAIL"
                print(f"{mark}\t{r['name']}\t{r['protocol']} {r['public_port']}\t"
                      f"in_pangolin={r['in_pangolin']}\t"
                      f"listening_in_gerbil={r['listening_in_gerbil']}")
        elif args.cmd == "sync":
            print(json.dumps(do_sync(cfg), indent=2, ensure_ascii=False))
        elif args.cmd == "site-create":
            res = do_site_create(cfg, args.name)
            print(f"Сайт создан: {res['name']} (id={res['site_id']}, "
                  f"subnet={res['subnet']})")
            print()
            print("Команда для ноды — вставить один раз в терминал ноды:")
            print(res["connect_command"])
            print()
            print("ВНИМАНИЕ: в команде есть секрет Newt. Не кидай её в чат, "
                  "в репозиторий и не показывай посторонним. Она останется "
                  "в истории шелла на ноде — после запуска можно почистить: "
                  "history -d $(history 1 | cut -c1-5) в bash, или просто "
                  "не переживай: кто имеет доступ к Docker на ноде, и так "
                  "увидит секрет через docker inspect.")
            print(f"Запасной вариант: {res['credentials_file']} (600) на "
                  "сервере, с --env-file.")
        elif args.cmd == "site-delete":
            if not args.name and not args.id:
                raise ToolError("site-delete: нужен --name или --id")
            print(json.dumps(do_site_delete(cfg, name=args.name,
                                            site_id=args.id),
                             indent=2, ensure_ascii=False))
        elif args.cmd == "serve":
            serve(cfg, args.bind, args.port)
    except ToolError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
