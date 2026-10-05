"""Локальный чат-релей: читает чат Twitch и отдаёт страницу по localhost.

Зачем: встроенный браузер OBS не грузит внешние сайты, поэтому виджеты чата
показывают пустоту; локальные страницы при этом рисуются. Значит, чат читаем сами,
а браузеру OBS отдаём страничку со своего localhost.

Что делает:
  * держит соединение с IRC Twitch (TLS, анонимно, только чтение) и получает сообщения;
  * отдаёт страницу чата с прозрачным фоном;
  * сообщения раздаёт через SSE (`/events`), переподключение — на стороне страницы;
  * картинки эмодзи проксирует через себя (`/emote/<id>`) и кеширует — до CDN Twitch
    браузер OBS тоже не дотянется, а релей дотянется;
  * `/test?text=…` — вброс сообщения для проверки; `/health` — состояние.

Только стандартная библиотека: служба не зависит от venv и обновлений пакетов.

Грабли (проверены на живом эфире): молчание в чате — не обрыв, поэтому по таймауту
шлём `PING` и продолжаем читать; страница чистит себя при переподключении; цвет
текста задан явно (в браузерном источнике по умолчанию чёрный и на тёмной сцене не
виден).
"""
from __future__ import annotations

import colorsys
import html
import json
import queue
import random
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KEEP = 40                                    # сколько последних сообщений держим
IRC_HOST, IRC_PORT = "irc.chat.twitch.tv", 6697
EMOTE_URL = "https://static-cdn.jtvnw.net/emoticons/v2/{id}/default/dark/1.0"

PAGE = """<!doctype html><meta charset="utf-8">
<title>Чат</title>
<style>
  html,body{margin:0;padding:0;background:transparent;overflow:hidden;height:100%}
  #box{position:absolute;bottom:0;left:0;right:0;padding:6px 8px;
       font:15px/1.32 "DejaVu Sans",system-ui,sans-serif;
       -webkit-mask-image:linear-gradient(to bottom,transparent 0,#000 26px);}
  .m{margin:0 0 3px;word-wrap:break-word;color:#fff;
     text-shadow:0 1px 2px rgba(0,0,0,.9),0 0 6px rgba(0,0,0,.75)}
  .n{font-weight:700}
  img.e{height:20px;vertical-align:-4px;margin:0 1px}
</style>
<div id="box"></div>
<script>
const box=document.getElementById('box');
function add(nick,color,text){
  const d=document.createElement('div'); d.className='m';
  const s=document.createElement('span'); s.className='n';
  s.style.color=color||'#c8a2ff'; s.textContent=nick+': ';
  d.appendChild(s); d.insertAdjacentHTML('beforeend',text);
  box.appendChild(d);
  while(box.children.length>28) box.removeChild(box.firstChild);
}
function connect(){
  const es=new EventSource('/events');
  // При (пере)подключении чистим окно: иначе на экране залипают старые строки,
  // и чат выглядит сломанным, хотя связь уже восстановилась.
  es.onopen=()=>{box.innerHTML=''};
  es.onmessage=e=>{const m=JSON.parse(e.data);add(m.nick,m.color,m.text)};
  es.onerror=()=>{es.close();setTimeout(connect,2000)};
}
connect();
</script>"""


def render(text: str, emotes: str) -> str:
    """Собирает безопасный HTML: текст экранируем, эмодзи вставляем картинками."""
    if not emotes:
        return html.escape(text)
    spans = []
    for part in emotes.split("/"):
        if ":" not in part:
            continue
        eid, ranges = part.split(":", 1)
        for rng in ranges.split(","):
            if "-" in rng:
                a, b = rng.split("-", 1)
                try:
                    spans.append((int(a), int(b), eid))
                except ValueError:
                    continue
    if not spans:
        return html.escape(text)
    spans.sort()
    out, pos = [], 0
    for a, b, eid in spans:
        if a < pos:
            continue
        out.append(html.escape(text[pos:a]))
        out.append(f'<img class="e" src="/emote/{eid}">')
        pos = b + 1
    out.append(html.escape(text[pos:]))
    return "".join(out)


def bright_color(hex_color: str) -> str | None:
    """Поднимает светлоту цвета ника: тёмные цвета Twitch на тёмном фоне не читаются."""
    value = (hex_color or "").lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        return None
    r, g, b = (int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    if light < 0.62:
        light, sat = 0.62, max(sat, 0.45)
        r, g, b = colorsys.hls_to_rgb(hue, light, sat)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def parse_line(line: str):
    """Разбирает строку IRC. Возвращает (ник, цвет, html) или None."""
    tags, rest = {}, line
    if line.startswith("@"):
        head, _, rest = line.partition(" ")
        for kv in head[1:].split(";"):
            key, _, value = kv.partition("=")
            tags[key] = value.replace("\\s", " ")
    m = re.match(r"^:(\w+)!\S+ PRIVMSG #\S+ :(.*)$", rest, re.S)
    if not m:
        return None
    nick = tags.get("display-name") or m.group(1)
    color = bright_color(tags.get("color") or "")
    if not color:
        hue = sum(ord(c) * (i + 7) for i, c in enumerate(nick)) % 360
        color = f"hsl({hue} 80% 72%)"
    return nick, color, render(m.group(2), tags.get("emotes", ""))


class Relay:
    """Страница чата + поток сообщений из IRC. Держит состояние, не блокирует."""

    def __init__(self, channel: str, host: str = "127.0.0.1", port: int = 8097):
        if not channel:
            raise ValueError("не задан канал чата (chat.channel)")
        self.channel = channel
        self.host = host
        self.port = port
        self.messages: deque = deque(maxlen=KEEP)
        self.subs: set[queue.Queue] = set()
        self.state = {"connected": False, "last": 0.0, "error": ""}
        self.emotes: dict[str, tuple] = {}
        self.lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    # ── эмодзи ──
    def fetch_emote(self, emote_id: str):
        with self.lock:
            if emote_id in self.emotes:
                return self.emotes[emote_id]
        try:
            req = urllib.request.Request(EMOTE_URL.format(id=emote_id),
                                         headers={"User-Agent": "redzone-chat/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                got = (r.read(), r.headers.get("Content-Type", "image/png"))
            with self.lock:
                self.emotes[emote_id] = got
            return got
        except Exception:  # noqa: BLE001 - сеть, не роняем релей
            return None

    # ── IRC ──
    def irc_loop(self) -> None:
        while True:
            try:
                ctx = ssl.create_default_context()
                raw = socket.create_connection((IRC_HOST, IRC_PORT), timeout=30)
                sock = ctx.wrap_socket(raw, server_hostname=IRC_HOST)
                # Тихий чат — норма, а не обрыв: по таймауту шлём PING и читаем дальше.
                sock.settimeout(60)
                nick = f"justinfan{random.randint(10000, 99999)}"
                sock.sendall(f"NICK {nick}\r\n".encode())
                sock.sendall(b"CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")
                sock.sendall(f"JOIN #{self.channel}\r\n".encode())
                self.state.update(connected=True, error="")
                print(f"IRC: подключился к #{self.channel} как {nick}", flush=True)
                buf = b""
                while True:
                    try:
                        chunk = sock.recv(8192)
                    except socket.timeout:
                        sock.sendall(b"PING :tmi.twitch.tv\r\n")
                        continue
                    if not chunk:
                        raise ConnectionError("соединение закрыто")
                    buf += chunk
                    while b"\r\n" in buf:
                        line, _, buf = buf.partition(b"\r\n")
                        text = line.decode("utf-8", "replace")
                        if text.startswith("PING"):
                            sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                            continue
                        try:
                            parsed = parse_line(text)
                        except Exception:  # noqa: BLE001
                            parsed = None
                        if parsed:
                            self.publish({"nick": parsed[0], "color": parsed[1],
                                          "text": parsed[2]})
            except Exception as exc:  # noqa: BLE001
                self.state.update(connected=False, error=str(exc)[:200])
                print(f"IRC: обрыв ({exc}), переподключение через 10 с", flush=True)
                time.sleep(10)

    def publish(self, msg: dict) -> None:
        """Кладёт сообщение в буфер и рассылает подписчикам SSE."""
        self.messages.append(msg)
        self.state["last"] = time.time()
        with self.lock:
            dead = []
            for q in self.subs:
                try:
                    q.put_nowait(msg)
                except Exception:  # noqa: BLE001
                    dead.append(q)
            for q in dead:
                self.subs.discard(q)

    # ── HTTP ──
    def handler(self):
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: A003 - глушим лог запросов
                pass
            def _send(self, code: int, ctype: str, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 - имя задано базовым классом
                u = urlparse(self.path)
                if u.path == "/":
                    self._send(200, "text/html; charset=utf-8", PAGE.encode())
                elif u.path == "/health":
                    with relay.lock:
                        subs = len(relay.subs)
                    body = json.dumps({
                        "channel": relay.channel, "connected": relay.state["connected"],
                        "last_message_ago": round(time.time() - relay.state["last"], 1)
                        if relay.state["last"] else None,
                        "subscribers": subs, "kept": len(relay.messages),
                        "error": relay.state["error"]}, ensure_ascii=False).encode()
                    self._send(200, "application/json; charset=utf-8", body)
                elif u.path == "/test":
                    qs = parse_qs(u.query)
                    relay.publish({"nick": (qs.get("nick") or ["проверка"])[0],
                                   "color": "#ffd166",
                                   "text": html.escape((qs.get("text") or ["тест"])[0])})
                    self._send(200, "text/plain; charset=utf-8", b"ok")
                elif u.path.startswith("/emote/"):
                    eid = re.sub(r"\D", "", u.path.rsplit("/", 1)[-1])
                    got = relay.fetch_emote(eid) if eid else None
                    if not got:
                        self._send(404, "text/plain", b"no emote")
                    else:
                        self._send(200, got[1], got[0])
                elif u.path == "/events":
                    self._events()
                else:
                    self._send(404, "text/plain", b"not found")

            def _events(self) -> None:
                q: queue.Queue = queue.Queue()
                with relay.lock:
                    relay.subs.add(q)
                    backlog = list(relay.messages)[-20:]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                try:
                    for m in backlog:
                        self.wfile.write(f"data: {json.dumps(m, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        try:
                            m = q.get(timeout=20)
                            self.wfile.write(
                                f"data: {json.dumps(m, ensure_ascii=False)}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except Exception:  # noqa: BLE001 - клиент отвалился, это норма
                    pass
                finally:
                    with relay.lock:
                        relay.subs.discard(q)

        return Handler

    def serve(self) -> None:
        """Вечный цикл: поток IRC + HTTP-сервер."""
        threading.Thread(target=self.irc_loop, daemon=True).start()
        self._server = ThreadingHTTPServer((self.host, self.port), self.handler())
        self._server.daemon_threads = True
        print(f"чат-релей: страница на http://{self.host}:{self.port}/ , "
              f"канал #{self.channel}", flush=True)
        self._server.serve_forever()


def serve(cfg) -> int:
    """Поднимает релей по конфигу модуля (блокирующий вызов)."""
    if not cfg.chat_channel:
        print("не задан канал чата: укажи chat.channel в конфиге "
              "(или REDZONE_CHAT_CHANNEL)")
        return 2
    Relay(cfg.chat_channel, host=cfg.chat_host, port=cfg.chat_port).serve()
    return 0


def health(cfg) -> dict | None:
    """Состояние релея по HTTP: None, если страница не отвечает."""
    try:
        with urllib.request.urlopen(
                f"http://{cfg.chat_host}:{cfg.chat_port}/health", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
