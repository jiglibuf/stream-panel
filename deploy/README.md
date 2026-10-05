# deploy — разворачивание

Всё, что нужно, чтобы поднять систему на своей машине: юниты systemd, пример
конфигурации хаба MediaMTX и правила маршрутизации звука PipeWire.

## Состав

| Каталог | Что это |
|---------|---------|
| `systemd/` | Юниты пользователя: пульт, чат-релей, сторож звука, модуль аналитики, обновление турнирной сетки, релей каста. |
| `mediamtx/` | Пример конфигурации приёмного хаба (`mediamtx.example.yml`) и `compose.yaml`. |
| `pipewire/` | Примеры правил маршрутизации звука по приложениям. |

## Юниты systemd

`deploy/systemd/` — юниты **пользователя** (`systemd --user`): root не нужен, правами
системы они не распоряжаются.

| Юнит | Что запускает |
|------|----------------|
| `stream-panel.service` | Пульт стрима (ядро, порт 8090). Единственный, кто говорит с OBS. |
| `rz-chat.service` | Чат-релей: читает чат Twitch и отдаёт страницу для OBS. |
| `rz-mix.service` | Сторож звука: держит виртуальные миксы и петли в наушники. |
| `dota-analytics.service` | Модуль аналитики (отдельный репозиторий dota-analytics). |
| `rz-bracket.service` + `rz-bracket.timer` | Обновление турнирной сетки в эфире раз в 5 минут. |
| `rz-cocast-relay.service` | Релей общего эфира из хаба в Twitch (совместный каст). |

**По умолчанию ничего не включено.** Юниты только копируются установщиком;
стрим-стек поднимается вручную (`tools/panel.sh up`) тогда, когда владелец
стримит, — и так же гасится (`tools/game-mode.sh on`) перед игрой. Пути берутся
из `%h`, каталоги модулей — из репозитория; настроечные переменные (например
`CHAT_CHANNEL`, `BRACKET_URL`) читаются из `~/.config/stream-panel/env`.

Установка (то же делает `tools/install.sh`):

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/*.service deploy/systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
# запуск вручную (без автозапуска):
systemctl --user start stream-panel.service
```

Автозапуск — осознанно, по желанию:

```bash
systemctl --user enable stream-panel.service     # и остальные по надобности
loginctl enable-linger "$USER"                   # старт без входа в графическую сессию
```

Проверка и журнал:

```bash
systemctl --user status stream-panel.service
journalctl --user -u stream-panel.service -f
```

Юнит и `scripts/run.sh` делают одно и то же (один порт 8090) — держать оба
одновременно не нужно.

## Хаб MediaMTX

`mediamtx/compose.yaml` поднимает контейнер `bluenviron/mediamtx` в host-сети.
Конфигурация — `mediamtx/mediamtx.example.yml`: скопировать рядом под именем
`mediamtx.yml` и заменить заглушки `<...>` на свои ключи.

```bash
cd ~/mediamtx
cp .../deploy/mediamtx/mediamtx.example.yml mediamtx.yml   # затем вписать свои ключи
docker compose up -d
```

Как устроен доступ: `program` — готовый эфир, `overridePublisher: false` (чужой
публикатор не подменит картинку в прямом эфире); `card1..cardN` — карты друзей,
у каждой свой ключ, и он работает ровно на свою карту; отдельный ключ зрителя даёт
чтение только `program`. Подробности — комментариями в самом примере.

## Звук (PipeWire)

Примеры правил маршрутизации: `pipewire/10-mix-sinks.conf` создаёт виртуальные
выходы-миксы (`game_mix`, `discord_mix`, `rz_mix`), `pipewire/20-app-routing.conf`
раскладывает потоки приложений по этим выходам.

```bash
mkdir -p ~/.config/pipewire/pipewire-pulse.conf.d ~/.config/pipewire/pipewire.conf.d
cp pipewire/10-mix-sinks.conf   ~/.config/pipewire/pipewire-pulse.conf.d/
cp pipewire/20-app-routing.conf ~/.config/pipewire/pipewire.conf.d/
systemctl --user restart pipewire pipewire-pulse
```

Декларативные правила — только «первый проход»: после переподключения наушников
связи петель рвутся, и чинит их сторож (`rz-mix.service`). Если сторож работает,
`20-app-routing.conf` можно и не ставить — про это написано в его комментарии.
