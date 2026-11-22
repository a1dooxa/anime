"""Локальный прокси для HLS (kodik): IINA берёт поток с 127.0.0.1, а прокси качает его у источника.

Зачем: отсюда к узлам kodik теряется ~1/3 TCP-соединений, а mpv открывает новое соединение на каждый
сегмент и ждёт потерянное вечно. Прокси держит соединения открытыми, рвёт подключение через 2с и
повторяет, и заранее подкачивает следующие сегменты. Для IINA поток идёт ровно.

Запуск: anime.py поднимает его сам (`python kproxy.py`), в фоне, на PORT.
Сам выходит через IDLE секунд без запросов. Слушает только 127.0.0.1.
"""
import base64
import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlparse

import httpx

PORT = 18733
IDLE = 3 * 3600  # IINA с большим кэшем скачивает серию за минуты и замолкает; паузу и перемотку прокси должен пережить
VERSION = "anime-kproxy " + str(int(os.path.getmtime(__file__)))  # anime.py перезапускает прокси, если код поменялся
AHEAD = 4  # сколько сегментов подкачивать вперёд
TRIES = 8
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

http = httpx.Client(headers={"User-Agent": UA}, follow_redirects=True,
                    timeout=httpx.Timeout(15, connect=2),  # потерянное соединение бросаем через 2с
                    limits=httpx.Limits(max_connections=12, max_keepalive_connections=12, keepalive_expiry=120))
pool = ThreadPoolExecutor(6)
cache = OrderedDict()  # url сегмента → Future[(bytes, content-type)]
nxt = {}  # url сегмента → следующие за ним, для подкачки
lock = threading.Lock()
last = time.monotonic()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def fetch(url, headers):
    err = None
    for i in range(TRIES):
        try:
            r = http.get(url, headers=headers)
            if r.status_code in (200, 206):
                if i:
                    log(f"ok с {i + 1}-й попытки", url[-50:])
                return r.content, r.headers.get("content-type", "application/octet-stream"), str(r.url)
            err = f"HTTP {r.status_code}"
            if r.status_code in (403, 404, 410):
                break  # ссылка протухла, повтор не поможет
        except httpx.HTTPError as e:
            err = f"{type(e).__name__}"
        time.sleep(0.2)
    log("не скачал", err, url[-60:])
    raise OSError(err)


def get(url, headers):
    """Сегмент из кэша или в очередь на скачивание; одинаковые запросы не дублируются."""
    with lock:
        f = cache.get(url)
        if f is None or (f.done() and f.exception()):
            f = cache[url] = pool.submit(fetch, url, headers)
        cache.move_to_end(url)
        while len(cache) > 40:
            cache.popitem(last=False)
    return f


def b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode()


def link(kind, url, h):
    """/s/<url>/<заголовки>/x.ts — mpv/ffmpeg берут сегмент HLS, только если адрес кончается расширением."""
    ext = os.path.splitext(urlparse(url).path)[1] or (".m3u8" if kind == "p" else ".ts")
    return f"/{kind}/{b64(url)}/{h or '-'}/x{ext}"


def slim(text, base):
    """Мастер-плейлист → только лучшее качество и его дорожки, адреса абсолютные.
    Иначе mpv перед стартом качает по 2 сегмента каждого качества (aniboom: 4 качества, ~10с)."""
    lines = text.splitlines()
    if "#EXT-X-STREAM-INF" not in text:
        return "\n".join(l if not l or l.startswith("#") else urljoin(base, l.strip()) for l in lines) + "\n"
    variants = [(int((re.search(r"BANDWIDTH=(\d+)", l) or [0, 0])[1]), l, nx.strip())
                for l, nx in zip(lines, lines[1:]) if l.startswith("#EXT-X-STREAM-INF")]
    _, inf, uri = max(variants, key=lambda v: v[0])
    groups = set(re.findall(r'(?:AUDIO|SUBTITLES)="([^"]+)"', inf))
    out = []
    for l in lines:
        if l.startswith("#EXT-X-MEDIA") and (re.search(r'GROUP-ID="([^"]+)"', l) or [0, ""])[1] not in groups:
            continue
        if l.startswith("#EXT-X-STREAM-INF") or (l and not l.startswith("#")):
            continue
        out.append(re.sub(r'URI="([^"]+)"', lambda m: f'URI="{urljoin(base, m[1])}"', l))
    return "\n".join(out + [inf, urljoin(base, uri)]) + "\n"


def local(url, headers=None, kind="p"):
    """Адрес плейлиста на прокси — его отдаём IINA. kind="m" — только облегчённый мастер."""
    return f"http://127.0.0.1:{PORT}" + link(kind, url, b64(json.dumps(headers)) if headers else "")


def kind_of(url):
    return "p" if urlparse(url).path.endswith(".m3u8") else "s"


def rewrite(text, base, h):
    """Адреса в плейлисте → на прокси. В мастер-плейлисте строки — плейлисты качества, иначе сегменты."""
    master = "#EXT-X-STREAM-INF" in text
    out, segs = [], []
    for line in text.splitlines():
        if line and not line.startswith("#"):
            u = urljoin(base, line.strip())
            if master or kind_of(u) == "p":
                line = link("p", u, h)
            else:
                line = link("s", u, h)
                segs.append(u)
        elif 'URI="' in line:  # отдельная дорожка звука (плейлист), ключи шифрования, init-сегменты
            line = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{link(kind_of(urljoin(base, m[1])), urljoin(base, m[1]), h)}"', line)
        out.append(line)
    with lock:
        for i, u in enumerate(segs):
            nxt[u] = segs[i + 1:i + 1 + AHEAD]
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global last
        last = time.monotonic()
        if self.path == "/ping":
            return self.send(200, VERSION.encode(), "text/plain")
        if self.path == "/quit":
            self.send(200, b"bye", "text/plain")
            os._exit(0)
        try:
            _, path, u, h, _ = self.path.split("/", 4)
            path, h = "/" + path, "" if h == "-" else h
            url = base64.urlsafe_b64decode(u).decode()
            headers = json.loads(base64.urlsafe_b64decode(h)) if h else {}
        except ValueError:
            return self.send(404, b"", "text/plain")
        try:
            if path == "/m":  # облегчённый мастер, сегменты плеер качает сам напрямую
                body, _, final = fetch(url, headers)
                return self.send(200, slim(body.decode("utf-8", "replace"), final).encode(), "application/vnd.apple.mpegurl")
            if path == "/p":
                body, _, final = fetch(url, headers)
                text = rewrite(body.decode("utf-8", "replace"), url, h)
                return self.send(200, text.encode(), "application/vnd.apple.mpegurl")
            if path == "/s":
                body, ctype, _ = get(url, headers).result()
                for n in nxt.get(url, []):
                    get(n, headers)
                rng = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
                if rng and int(rng[1]) > 0:  # докачка с середины
                    a, b = int(rng[1]), int(rng[2] or len(body) - 1)
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {a}-{b}/{len(body)}")
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(b - a + 1))
                    self.end_headers()
                    return self.wfile.write(body[a:b + 1])
                return self.send(200, body, ctype)
            self.send(404, b"", "text/plain")
        except (OSError, ValueError) as e:
            if not isinstance(e, (BrokenPipeError, ConnectionResetError)):
                self.send(502, str(e).encode(), "text/plain")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # плеер закрыл соединение посреди ответа (перемотка, выход) — это нормально


def idle_exit():
    while time.monotonic() - last < IDLE:
        time.sleep(15)
    log("простой, выхожу")
    os._exit(0)


def main():
    try:
        srv = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        return  # уже запущен
    threading.Thread(target=idle_exit, daemon=True).start()
    log(f"слушаю 127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
