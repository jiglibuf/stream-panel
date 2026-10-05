#!/usr/bin/env python3
"""Держит звук в эфире, разделяя ИГРУ и ГОЛОСОВОЙ ЧАТ на разные источники OBS.

Зачем разделять: на вкладках с чужими матчами скрипт фокуса глушит «Звук системы»
целиком — и вместе с чужой игрой пропадали комментарии из голосового чата. Теперь
голосовой чат идёт своим источником и не глушится никогда.

Схема:

  игра (media.role=game) ──► game_mix ───петля──► наушники ──► источник OBS «Звук системы»
  Discord (WEBRTC)       ──► discord_mix ─петля─► наушники ──► источник OBS «Дискорд»
  всё остальное (браузер)─► rz_mix ──────петля──► наушники      (в эфир не идёт)

`rz_mix` остаётся выходом ПО УМОЛЧАНИЮ и OBS его не читает — именно поэтому удаётся
читать мониторы game_mix и discord_mix: OBS блокирует захват только того устройства,
которое совпадает с устройством мониторинга («По умолчанию»).

Сторож следит, чтобы выходы, петли, перенос потоков и значение по умолчанию не
пропали (перезагрузка, перезапуск PipeWire, случайное переключение выхода).

Параметры берутся из окружения (их ставит модуль RedZone):
  REDZONE_STATE_DIR, REDZONE_AUDIO_DEVICE_FILE, REDZONE_AUDIO_ALL_SINK,
  REDZONE_AUDIO_GAME_SINK, REDZONE_AUDIO_DISCORD_SINK, REDZONE_AUDIO_HEADPHONES_HINT
"""
import json
import os
import re
import subprocess
import sys
import time

SINK = os.environ.get("REDZONE_AUDIO_ALL_SINK", "rz_mix")
GAME_SINK = os.environ.get("REDZONE_AUDIO_GAME_SINK", "game_mix")
DISCORD_SINK = os.environ.get("REDZONE_AUDIO_DISCORD_SINK", "discord_mix")
ALL_SINKS = (SINK, GAME_SINK, DISCORD_SINK)
DESCRIPTIONS = {SINK: "RZ_Mix", GAME_SINK: "RZ_Игра", DISCORD_SINK: "RZ_Discord"}

# Гарнитура в беспроводном режиме и в режиме зарядки по USB видна под РАЗНЫМИ
# именами, поэтому точное имя не зашиваем: ищем по образцу, а выбор владельца
# (файл-строка) важнее любых догадок.
HEADPHONES_HINT = os.environ.get("REDZONE_AUDIO_HEADPHONES_HINT", "")
STATE_DIR = os.environ.get("REDZONE_STATE_DIR", os.path.expanduser("~/.local/state/stream-panel/redzone"))
DEVICE_FILE = os.environ.get("REDZONE_AUDIO_DEVICE_FILE", os.path.join(STATE_DIR, "device"))
POLL = int(os.environ.get("REDZONE_AUDIO_POLL", "5"))
ENV = {**os.environ, "LANG": "C"}   # окружение наследуем: pactl ищет сокет через XDG_RUNTIME_DIR


def run(*args, check=False):
    r = subprocess.run(args, capture_output=True, text=True, env=ENV)
    if check and r.returncode:
        print(f"  !! {' '.join(args)}: {r.stderr.strip()}", flush=True)
    return r.stdout.strip()


def sinks():
    return [line.split("\t")[1] for line in run("pactl", "list", "short", "sinks").splitlines()
            if "\t" in line]


def modules():
    return run("pactl", "list", "short", "modules").splitlines()


VIRTUAL_SINKS = set(ALL_SINKS) | {"easyeffects_sink"}   # виртуальные: петля в них звука не даёт


def chosen_device():
    """Устройство мониторинга, выбранное в пульте: одна строка в файле device."""
    try:
        with open(DEVICE_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def headphones_sink():
    """Устройство для петель, по убыванию доверия: выбор владельца, гарнитура по образцу,
    любой физический выход. Виртуальные выходы (наши и EasyEffects) не берём."""
    names = sinks()
    chosen = chosen_device()
    if chosen and chosen in names and chosen not in VIRTUAL_SINKS and "monitor" not in chosen:
        return chosen
    if HEADPHONES_HINT:
        for name in names:
            if HEADPHONES_HINT in name and name not in VIRTUAL_SINKS and "monitor" not in name:
                return name
    for name in names:
        if name not in VIRTUAL_SINKS and "monitor" not in name:
            return name
    return ""


def pw_dump():
    """Разбирает pw-dump: узлы, порты, связи. Нужен, чтобы отличать «модуль петли есть»
    от «выход петли реально подключён к наушникам» — это разные вещи."""
    try:
        data = json.loads(run("pw-dump") or "[]")
    except ValueError:
        return {}, {}, []
    nodes, ports, links = {}, {}, []
    for obj in data:
        kind = obj.get("type")
        info = obj.get("info") or {}
        props = info.get("props") or {}
        if kind == "PipeWire:Interface:Node":
            nodes[obj["id"]] = props.get("node.name", "")
        elif kind == "PipeWire:Interface:Port":
            ports[obj["id"]] = props.get("port.name", "")
        elif kind == "PipeWire:Interface:Link":
            links.append(info)
    return nodes, ports, links


def drop_stray_links(nodes, links, phones_id):
    """Петля не должна играть никуда, кроме наушников.

    Если наушники отключались, выход петли успевал привязаться к другому физическому
    выходу (колонкам) и после возврата наушников звук идёт сразу в два места. Снимаем
    такие связи: `pactl` при этом показывает поток привязанным к одному устройству и врёт,
    правда — на уровне связей портов.
    """
    virtual = {nid for nid, name in nodes.items()
               if name in VIRTUAL_SINKS or name.startswith("easyeffects")}
    loopbacks = {nid for nid, name in nodes.items() if name.startswith("output.loopback")}
    dropped = 0
    for link in links:
        out, inp = link.get("output-node-id"), link.get("input-node-id")
        if out not in loopbacks or inp == phones_id or inp in virtual:
            continue
        target = nodes.get(inp, "")
        if not target or "monitor" in target:
            continue
        subprocess.run(["pw-link", "-d", str(out), str(inp)],
                       capture_output=True, text=True, env=ENV)
        dropped += 1
    if dropped:
        print(f"  снято связей петель со сторонним выходом: {dropped}", flush=True)


def repair_loop_links():
    """После переподключения наушников (или перезапуска PipeWire) выходы петель остаются
    привязанными к уничтоженному устройству: модуль и поток живы, а выход никуда не идёт —
    в наушниках тишина, хотя снаружи схема выглядит рабочей."""
    phones = headphones_sink()
    if not phones:
        return
    nodes, _, links = pw_dump()
    phones_id = next((nid for nid, name in nodes.items() if name == phones), None)
    if phones_id is None:
        return
    loopbacks = {nid: name for nid, name in nodes.items() if name.startswith("output.loopback")}
    connected = {link.get("output-node-id") for link in links
                 if link.get("input-node-id") == phones_id}
    for nid, name in sorted(loopbacks.items(), key=lambda kv: kv[1]):
        if nid in connected:
            continue
        made = 0
        for ch in ("FL", "FR"):
            link_cmd = subprocess.run(["pw-link", f"{name}:output_{ch}", f"{phones}:playback_{ch}"],
                                      capture_output=True, text=True, env=ENV)
            made += link_cmd.returncode == 0
        if made:
            print(f"  петля {name} была отключена от наушников — подключил ({made} канала)",
                  flush=True)
    drop_stray_links(nodes, links, phones_id)


def inputs():
    """Разбирает `pactl list sink-inputs` в список словарей: id, sink, свойства."""
    txt = run("pactl", "list", "sink-inputs")
    out = []
    for blk in txt.split("Sink Input #")[1:]:
        def prop(key):
            m = re.search(rf'^\s*{key} = "?([^"\n]+)"?', blk, re.M)
            return m.group(1).strip() if m else ""
        sid = re.search(r"^\s*Sink: (\S+)", blk, re.M)
        out.append({
            "id": blk.split()[0],
            "sink": sid.group(1) if sid else "",
            "app": prop("application.name"),
            "role": prop("media.role"),
            "node": prop("node.name"),
        })
    return out


def target_sink_for(stream):
    """Куда должен идти поток. Петли (output.loopback) не трогаем — иначе звук зациклится."""
    node = (stream["node"] or "").lower()
    if "output.loopback" in node or stream["app"].lower().startswith("loopback"):
        return None
    app = (stream["app"] or "").lower()
    if stream["role"] == "game" or "dota" in app or app.startswith("sdl"):
        return GAME_SINK
    if "webrtc voiceengine" in app or "discord" in app:
        return DISCORD_SINK
    return None


def ensure():
    # 1. все три виртуальных выхода
    for name in ALL_SINKS:
        if name not in sinks():
            run("pactl", "load-module", "module-null-sink", f"sink_name={name}",
                f"sink_properties=device.description={DESCRIPTIONS[name]}", check=True)
            time.sleep(0.7)
            print(f"создан выход {name}" if name in sinks() else f"!! не удалось создать {name}",
                  flush=True)

    # 2. петля из каждого выхода в наушники (иначе игра/чат не слышны в ушах)
    for name in ALL_SINKS:
        probe = f"source={name}.monitor"
        if not any("module-loopback" in line and probe in line for line in modules()):
            phones = headphones_sink()
            if not phones:
                continue
            run("pactl", "load-module", "module-loopback", probe, f"sink={phones}",
                "latency_msec=30", "source_dont_move=true", "sink_dont_move=true", check=True)
            ok = any("module-loopback" in line and probe in line for line in modules())
            print(f"создана петля {name} → наушники" if ok
                  else f"!! не удалось создать петлю {name}", flush=True)
        elif not run("pactl", "list", "sink-inputs").count("output.loopback"):
            for line in modules():
                if "module-loopback" in line and probe in line:
                    run("pactl", "unload-module", line.split("\t")[0])
            print(f"поток петли {name} пропал — пересоздаю", flush=True)

    # 2б. модуль петли может быть жив, а её выход — на уничтоженном устройстве
    repair_loop_links()

    # 3. раскидываем потоки: игра → game_mix, голосовой чат → discord_mix
    for stream in inputs():
        want = target_sink_for(stream)
        if want and stream["sink"] != want and want in sinks():
            run("pactl", "move-sink-input", stream["id"], want, check=True)
            print(f"  {stream['app'] or stream['node'] or 'поток #' + stream['id']} → {want}",
                  flush=True)

    # 4. выход по умолчанию обязан быть rz_mix: иначе OBS заблокирует захват мониторов
    if run("pactl", "get-default-sink") != SINK:
        run("pactl", "set-default-sink", SINK, check=True)
        now = run("pactl", "get-default-sink")
        print(f"выход по умолчанию → {now}" if now == SINK
              else f"!! выход по умолчанию остался {now}", flush=True)


def main():
    print(f"сторож звука запущен, наушники: {headphones_sink() or 'не найдены'}", flush=True)
    while True:
        try:
            ensure()
        except Exception as exc:               # сторож не должен падать
            print(f"  !! ошибка сторожа: {exc}", file=sys.stderr, flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
