# pangolin-tcp-udp-tool

Тулза на сервере с Pangolin, которая сама делает все 4 шага для сырого
TCP/UDP порта — вместо ручной возни с панелью, Gerbil и Traefik.

Один публичный порт = одна нода. Пример: много компов с RDP —
`29378 → комп-1:3389`, `29379 → комп-2:3389`, и так далее.
RDP по имени хоста не различается, только по порту, поэтому домен тут
не нужен — достаточно `IP VPS:порт`.

## Что делает `add`

1. Создаёт ресурс Raw TCP/UDP в Pangolin через Integration API
   (`mode=tcp|udp`, `proxyPort`, + target site → `127.0.0.1:порт ноды`).
2. Добавляет порт в Gerbil в `docker-compose.yml` (`29378:29378`,
   для UDP — `29378:29378/udp`). С бэкапом.
3. Добавляет entrypoint `tcp-29378` / `udp-29378` в
   `config/traefik/traefik_config.yml`. Имя именно такое — оно
   обязательно для динамической конфигурации Pangolin.
4. Пересоздаёт Gerbil + Traefik и проверяет, что порт слушается
   **внутри Gerbil** (`docker exec gerbil ss`). Внешний `nc` не
   считается — docker-proxy принимает TCP сам.

`delete` делает всё в обратную сторону и проверяет, что порт закрылся.
`check` сверяет слои: есть ли ресурс в Pangolin и слушается ли порт.
`sync` забирает в состояние правила, созданные руками в панели.
`list` / `sites` — что есть. `serve` — маленькая веб-панель.

## Требования на сервере

- Pangolin 1.26+ (проверено на 1.26.0), `allow_raw_resources: true`
  (тулза сама проверит и включит флаг, если его нет).
- Integration API включён: в `config/config.yml`
  `flags: enable_integration_api: true`, и в `docker-compose.yml`
  у сервиса `pangolin` проброс `127.0.0.1:3003:3003`.
  API слушает только локально, ключ не светится наружу.
- Root API key Integration API: Server Admin → API Keys → Create,
  права: `listOrgs, createSite, getSite, listSites, createResource,
  getResource, listResources, deleteResource, createTarget,
  listTargets, deleteTarget`.

## Установка

```bash
git clone <этот репозиторий>
cd pangolin-tcp-udp-tool
sudo bash install.sh
# заполнить /etc/pangolin-tool/config.json: org_id, api_key, ui_token
```

## Примеры

```bash
pangolin-tool sites
pangolin-tool site-create --name node-1
# выдаст готовую команду для ноды — один раз вставить в терминал ноды,
# и Newt поднимется, никаких файлов перекидывать не надо:
# docker run -d --name newt --network host --cap-add NET_ADMIN \
#   -e PANGOLIN_ENDPOINT=https://tun.emae.su \
#   -e NEWT_ID=... -e NEWT_SECRET=... fosrl/newt:latest
# ВНИМАНИЕ: в команде есть секрет, не кидай её в чат и в репозиторий.
# Запасной вариант — /etc/pangolin-tool/sites/node-1.env (600) на сервере,
# с ним: docker run ... --env-file /etc/pangolin-tool/sites/node-1.env ...
pangolin-tool add --name rdp-pc1 --protocol tcp --public-port 29378 \
  --site node-1 --target-ip 127.0.0.1 --target-port 3389
pangolin-tool add --name rdp-pc1-udp --protocol udp --public-port 29378 \
  --site wsl --target-ip 127.0.0.1 --target-port 3389
pangolin-tool add --name minecraft --protocol tcp --public-port 25565 \
  --site wsl --target-ip 127.0.0.1 --target-port 25565
pangolin-tool list
pangolin-tool check --name rdp-pc1
pangolin-tool delete --name rdp-pc1
pangolin-tool add --name test --protocol tcp --public-port 29380 \
  --site wsl --target-ip 127.0.0.1 --target-port 3389 --dry-run
```

## Рестарт и пакетный режим

Каждый `add`/`delete` по умолчанию пересоздаёт Gerbil и Traefik: entrypoint
Traefik и порт Gerbil — статическая конфигурация, без пересоздания она не
подхватится. На время рестарта (обычно 10–30 секунд) недоступны панель,
все HTTP-маршруты и все raw-порты, а туннели Newt переподключаются сами.
Это нормально, не баг.

Чтобы не ронять стек на каждое правило, делай правки пачкой:

```bash
pangolin-tool add --name rdp-pc1 --protocol tcp --public-port 29378 \
  --site wsl --target-ip 127.0.0.1 --target-port 3389 --no-restart
pangolin-tool add --name rdp-pc1-udp --protocol udp --public-port 29378 \
  --site wsl --target-ip 127.0.0.1 --target-port 3389 --no-restart
pangolin-tool delete --name старое-правило --no-restart
pangolin-tool apply   # один рестарт на все правки + проверка слушателей
```

Пока не вызван `apply`, новые правила уже есть в Pangolin, но порты ещё не
слушаются, а удалённые — ещё слушаются.

## Нода в WSL (важно)

Если Newt крутится в WSL, а сервис — на самой Windows (например RDP на 3389),
`--target-ip 127.0.0.1` НЕ сработает: глазами WSL `127.0.0.1` — это сам WSL,
а не Windows. Бери IP Windows из WSL и ставь его целью:

```bash
grep nameserver /etc/resolv.conf   # IP Windows, обычно 172.x.x.1
nc -vz <этот-IP> 3389              # проверка: должен пройти
pangolin-tool add --name rdp-pc1 --protocol tcp --public-port 29378 \
  --site wsl --target-ip <этот-IP> --target-port 3389
```

Нюанс: этот IP может поменяться после перезагрузки Windows/WSL — если RDP
вдруг перестал работать, проверь `resolv.conf` заново и пересоздай правило
с новым IP. Чистый вариант без этого — запустить Newt прямо на Windows
(файл `newt.exe` из релизов fosrl/newt, с теми же переменными
`PANGOLIN_ENDPOINT`/`NEWT_ID`/`NEWT_SECRET`): тогда `127.0.0.1` — это
Windows, и цель `127.0.0.1:3389` работает всегда. Перед этим останови
Docker-Newt, два агента с одним сайтом одновременно не запускай.

Веб-панель — только через SSH-туннель, см. вывод `install.sh`.

## Важно

- Raw TCP/UDP в Pangolin — труба без логина Pangolin. Безопасность —
  на самом сервисе (RDP с паролем, Minecraft с online-mode и т.п.).
  RCON и админ-порты так не прокидывать.
- Файлы `config.json` / `rules.json` с ключами — только на сервере,
  в репозиторий не попадают (см. `.gitignore`).
- Тесты: `python3 -m pytest tests/ -q` или `python3 -m unittest discover -s tests`.
