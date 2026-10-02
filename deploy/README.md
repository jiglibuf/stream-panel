# Автозапуск пульта (systemd, unit пользователя)

Опционально: чтобы пульт переживал перезагрузку компьютера и поднимался сам,
не заходя в терминал. Это **unit пользователя** — root не нужен, правами
системы мы не распоряжаемся.

Unit лежит рядом: `deploy/stream-panel.service`.

## Установка

```bash
# 1. Скопировать unit в каталог пользовательских юнитов
mkdir -p ~/.config/systemd/user
cp deploy/stream-panel.service ~/.config/systemd/user/

# 2. Перечитать конфигурацию systemd и включить автозапуск
systemctl --user daemon-reload
systemctl --user enable --now stream-panel.service

# 3. Разрешить юниту работать после выхода из сессии (нужно для запуска при
#    загрузке без входа в графическую сессию)
loginctl enable-linger "$USER"
```

## Проверка и управление

```bash
systemctl --user status stream-panel.service
journalctl --user -u stream-panel.service -f    # живой журнал сервиса
systemctl --user restart stream-panel.service
systemctl --user stop stream-panel.service
systemctl --user disable stream-panel.service    # снять с автозапуска
```

## Замечания

- Перед первым запуском фронт должен быть собран: `npm --prefix frontend run build`.
  Сервис раздаёт `frontend/dist`; при его отсутствии отдаётся заглушка.
- Unit запускает `python -m server` из каталога репозитория — тот же запуск,
  что и `scripts/run.sh`, только под systemd. Поэтому `scripts/run.sh` и юнит
  **нельзя держать включёнными одновременно**: у них разные PID-файлы, и второй
  запуск упрётся в занятый порт.
- Раздача в tailnet (`tailscale serve`) — отдельный шаг, см. README. Автозапуск
  самого `tailscale serve` конфигурация tailnet помнит между перезапусками.
