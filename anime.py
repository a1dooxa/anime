"""anime: поиск сразу по всем источникам → тайтл → серия → озвучка → IINA.

Парсеры берём из anicli-api, выбор стрелками через fzf, видео открываем в IINA.
"""
import contextlib
import importlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import logging

import httpx

import kproxy
from types import SimpleNamespace
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import urlparse

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

logging.disable(logging.WARNING)  # anicli_api шумит «try again» на каждый повтор запроса

SOURCES = {  # модуль anicli_api.source → короткое имя для списка
    "yummy_anime": "yummy", "animego": "animego", "anilibria": "libria", "dreamcast": "dreamcast",
    "animevost": "vost", "sameband": "sameband", "anidub": "anidub",
}
OWN = {"anidub"}  # свои парсеры рядом со скриптом, остальные из anicli_api  # anilibme (26с на поиск) и hdrezka (не отвечает) выключены 2026-11-22
SEARCH = ("yummy_anime", "animego")  # быстрые агрегаторы: в них почти все озвучки
STUDIOS = [m for m in SOURCES if m not in SEARCH]  # сайты студий: ищем тайтл в них, только когда он уже выбран
HISTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.json")
EVENTS = os.path.join(os.path.dirname(HISTORY), "events.jsonl")
RUN = None  # coverage.py ставит "coverage", чтобы замер не смешивался с просмотрами
POOL = ThreadPoolExecutor(48)
DUB_ALIASES = {"aniliberty": "anilibria", "студийнаябанда": "studioband", "kansaistudio": "kansai", "jamclub": "jam",
               "anidubonline": "anidub", "freedubstudio": "freedub", "samebandstudio": "sameband"}  # одна студия — одна строка
# rich сам выключает цвет и анимацию при NO_COLOR, TERM=dumb и выводе не в терминал
con = Console(stderr=True, highlight=False)
PINK, VIOLET = "#ff79c6", "#bd93f9"
FZF_THEME = ("fg:#d4d4dc,fg+:#ffffff,bg+:#3b2d4f,hl:#ff79c6,hl+:#ff79c6:bold,info:#bd93f9,"
             "prompt:#ff79c6,pointer:#ff79c6,marker:#50fa7b,spinner:#bd93f9,header:#8be9fd,"
             "border:#5d4a7a,label:#ff79c6:bold,query:#ffffff")


# ---------- общие помощники ----------

def say(msg, icon="✗", style="red", hint=""):
    con.print(f"[{style}]{icon}[/] {msg}" + (f"  [dim]{hint}[/]" if hint else ""))


def log(**ev):
    """Строка в events.jsonl. Журнал не должен ронять просмотр, поэтому ошибки записи глотаем."""
    with contextlib.suppress(OSError):
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": round(time.time(), 3), **({"run": RUN} if RUN else {}), **ev}, ensure_ascii=False) + "\n")


def cause(exc):
    """Класс провала для журнала (на экран идёт why)."""
    t = f"{exc}".lower()
    if type(exc) is LookupError:  # KeyError/IndexError тоже LookupError, но это поломка парсера
        return "нет тайтла"
    if isinstance(exc, (httpx.HTTPError, OSError)) or "403" in t or "timed out" in t:
        return "сеть или 403"
    return "ошибка парсера"


def why(exc):
    """Короткая человеческая причина вместо трейсбэка."""
    t = f"{type(exc).__name__} {exc}".lower()
    if "ddos" in t or "403" in t or "cloudflare" in t:
        return "блокирует (403)"
    if "timeout" in t or "timed out" in t:
        return "не отвечает"
    if "connect" in t:
        return "нет соединения"
    if isinstance(exc, LookupError):
        return "нет тайтла"
    return "сломался парсер"


class Board:
    """Живая табличка: спиннер на каждый источник, общая полоса, секундомер."""

    def __init__(self, title, labels):
        self.title, self.labels = title, labels
        self.state = [None] * len(labels)  # None → в работе; (ok, текст)
        self.t0 = time.monotonic()
        self.spin = Spinner("dots", style=PINK)

    def __rich__(self):
        done = sum(s is not None for s in self.state)
        bar = ProgressBar(total=len(self.labels), completed=done, width=28,
                          complete_style=PINK, finished_style="green", style="grey23")
        # полоса в одной строке с заголовком: отдельной строкой ProgressBar не переносится
        head = Table.grid(padding=(0, 1))
        head.add_row(Text(self.title, f"bold {PINK}"), bar, Text(f"{done}/{len(self.labels)}", VIOLET),
                     Text(f"{time.monotonic() - self.t0:.1f}с", "dim"))
        grid = Table.grid(padding=(0, 1))
        for label, st in zip(self.labels, self.state):
            if st is None:
                grid.add_row(" ", self.spin, Text(label, "bold"), Text("ищу…", "dim"))
            else:
                grid.add_row(" ", Text("✓", "green") if st[0] else Text("✗", "red"),
                             Text(label, "bold" if st[0] else "dim"), Text(st[1], "" if st[0] else "dim red"))
        return Group(head, grid)

    def elapsed(self):
        return time.monotonic() - self.t0


def gather(fn, items, timeout, title, label, ok_text, grace=4, ev=None):
    """Запускает fn(item) параллельно и показывает это живой табличкой.
    Что упало или не успело — пропускаем. После первого удачного ответа остальных
    ждём не дольше grace секунд: заблокированные источники иначе тормозят всё до timeout.
    Возвращает ([(item, result)], board) — board для итоговой строки."""
    items = list(items)
    board = Board(title, [label(it) for it in items])
    futs = {POOL.submit(fn, it): i for i, it in enumerate(items)}
    deadline = time.monotonic() + timeout
    pending = set(futs)
    graced = False
    took = {}
    with Live(board, console=con, transient=True, refresh_per_second=15):
        while pending and (left := deadline - time.monotonic()) > 0:
            done, pending = wait(pending, left, return_when=FIRST_COMPLETED)
            for f in done:
                e = f.exception()
                took[f] = time.monotonic() - board.t0
                board.state[futs[f]] = (True, ok_text(f.result())) if e is None else (False, why(e))
                if e is None and time.monotonic() + grace < deadline:
                    deadline, graced = time.monotonic() + grace, True
        for f in pending:
            board.state[futs[f]] = (False, "не дождался")
    if ev is not None:
        for f, i in futs.items():
            e = None if f in pending else f.exception()
            log(**ev, src=board.labels[i], sec=round(took.get(f, board.elapsed()), 2),
                result="ok" if f not in pending and e is None else
                       ("не дождался (grace)" if graced else "не дождался (таймаут)") if f in pending else cause(e),
                **({"info": board.state[i][1]} if f not in pending and e is None else {}),
                **({"exc": type(e).__name__, "msg": str(e)[:300]} if e is not None else {}))
    return [(items[i], f.result()) for f, i in futs.items() if f.done() and f.exception() is None], board


def summary(icon, text, board):
    bad = [l for l, s in zip(board.labels, board.state) if not s[0]]
    ok = len(board.labels) - len(bad)
    tail = f"  [dim]· {ok}/{len(board.labels)} ист. · {board.elapsed():.1f}с[/]"
    if bad:
        tail += f"  [dim red]✗ {', '.join(bad)}[/]"
    con.print(f"[green]✓[/] {icon} {text}{tail}")


def plural(n, one, few, many):
    n10, n100 = n % 10, n % 100
    w = one if n10 == 1 and n100 != 11 else few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many
    return f"{n} {w}"


def ago(ts):
    d = time.time() - ts
    for sec, name in ((86400, ("день", "дня", "дней")), (3600, ("час", "часа", "часов")), (60, ("минуту", "минуты", "минут"))):
        if d >= sec:
            return plural(int(d // sec), *name) + " назад"
    return "только что"


def pick(rows, label, header="", pos=1, prompt="❯ "):
    """fzf-меню в нашей теме. Возвращает индекс строки или None (Esc)."""
    if not rows:
        return None
    lines = "\n".join(f"{i}\t{r}" for i, r in enumerate(rows))
    keys = "\033[2m↑↓ выбрать · Enter ок · Esc назад · печатай для фильтра\033[0m"
    cmd = ["fzf", "--height=~85%", "--layout=reverse", "--ansi", "--no-sort", "--cycle",
           "--delimiter=\t", "--with-nth=2..", f"--prompt={prompt}", "--pointer=▶", "--marker=✓",
           "--border=rounded", f"--border-label= {label} ", "--border-label-pos=3",
           "--info=inline-right", "--padding=0,1", "--margin=0,1", "--highlight-line",
           f"--color={FZF_THEME}", f"--bind=load:pos({pos})",
           "--header=" + (f"{header}\n" if header else "") + keys, "--header-first"]
    r = subprocess.run(cmd, input=lines, stdout=subprocess.PIPE, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return int(r.stdout.split("\t", 1)[0])


def tag(name):
    return f"\033[38;5;141m{name}\033[0m"


def dim(t):
    return f"\033[2m{t}\033[0m"


def pink(t):
    return f"\033[38;2;255;121;198m{t}\033[0m"


def kind_icon(info):
    i = info.lower()
    return "🎬" if "фильм" in i else "💿" if any(k in i for k in ("ova", "ona", "спешл", "спэшл")) else "📺"


def title_key(t):
    t = t.split(" / ")[0]
    t = re.sub(r"\[.*?\]", "", t)  # animevost: «[1-28 из 28]»
    return re.sub(r"[^0-9a-zа-я]", "", t.lower().replace("ё", "е"))


def title_words(t):
    """Как title_key, но без порядка слов: sameband пишет «Фрирен: провожающая в последний путь»."""
    t = re.sub(r"\[.*?\]", "", t.split(" / ")[0]).lower().replace("ё", "е")
    return sorted(re.findall(r"[0-9a-zа-я]+", t))


def dub_name(t):
    return re.sub(r"^\s*озвучка\s+", "", t.strip(), flags=re.I) or "?"


def dub_key(t):
    k = re.sub(r"[^0-9a-zа-я]", "", dub_name(t).lower().replace("ё", "е"))
    return DUB_ALIASES.get(k, k)


def ep_label(e):
    name = re.sub(r"\s*\b\d+\b\s*", " ", e.title or "").strip()
    return "" if re.fullmatch(r"(episode|серия|эпизод|serie)?", name, re.I) else name


def load_history():
    try:
        with open(HISTORY, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_history(h):
    tmp = HISTORY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=1)
    os.replace(tmp, HISTORY)


# ---------- шаги ----------

def extractor(mod):
    return importlib.import_module(mod if mod in OWN else f"anicli_api.source.{mod}").Extractor()

def search(query):
    def one(mod):
        return extractor(mod).search(query)

    got, board = gather(one, SEARCH, 10, f"🔎 Ищу «{query}»", lambda m: SOURCES[m],
                        lambda r: plural(len(r), "тайтл", "тайтла", "тайтлов"), ev={"step": "search", "query": query})
    groups = {}  # title_key → {"title", "score", "info", "items": [(src, search)]}
    for mod, results in got:
        for rank, r in enumerate(results):
            g = groups.setdefault(title_key(r.title), {"title": r.title.split(" / ")[0].strip(), "score": 0, "info": "", "items": []})
            if any(s == mod for s, _ in g["items"]):
                continue
            g["items"].append((mod, r))
            g["score"] += 1 / (rank + 1)  # высоко в нескольких источниках → выше в списке
            g["info"] = g["info"] or title_info(r)
    summary("🔎", f"«{query}»: [bold]{plural(len(groups), 'тайтл', 'тайтла', 'тайтлов')}[/]", board)
    return sorted(groups.values(), key=lambda g: -g["score"])


def title_info(r):
    """Год/тип/серии, если источник их отдаёт (yummy — год и тип, animevost — «[1-170 из 170]»)."""
    s = str(r)
    year = re.search(r"'year': (\d{4})", s)
    kind = re.search(r"'type': '([^']+)'", s)
    eps = re.search(r"\[(?:\d+-)?(\d+) из (\d+)\]", s)
    kind = kind and kind[1].replace("Полнометражный фильм", "Фильм")
    parts = [year and year[1], kind, eps and f"{eps[2]} сер."]
    return " · ".join(p for p in parts if p)


def find_in(mod, group, query):
    """Тот же тайтл на сайте студии: ищем по запросу и по названию, сверяем title_key."""
    ex, key = extractor(mod), title_words(group["title"])
    for q in dict.fromkeys((query, group["title"])):
        for r in ex.search(q):
            if title_words(r.title) == key:
                return r
    raise LookupError(group["title"])


def episodes(group, query):
    have = {m for m, _ in group["items"]}
    items = group["items"] + [(m, None) for m in STUDIOS if m not in have]
    got, board = gather(lambda it: (it[1] or find_in(it[0], group, query)).get_anime().get_episodes(), items, 20,
                        "📺 Загружаю серии", lambda it: SOURCES[it[0]],
                        lambda lst: plural(len(lst), "серия", "серии", "серий"),
                        ev={"step": "episodes", "title": group["title"], "query": query})
    eps = {}  # номер → [(src, episode)]
    names = {}
    for (mod, _), lst in got:
        for i, e in enumerate(lst):
            try:
                n = int(e.num)
            except (TypeError, ValueError):
                n = i + 1
            eps.setdefault(n, []).append((mod, e))
            if not names.get(n):
                names[n] = ep_label(e)
    summary("📺", f"{group['title']}: [bold]{plural(len(eps), 'серия', 'серии', 'серий')}[/]", board)
    return eps, names


def dubs(ep_items, ep):
    got, board = gather(lambda it: it[1].get_sources(), ep_items, 15, f"🎙  Озвучки для серии {ep}",
                        lambda it: SOURCES[it[0]], lambda lst: plural(len(lst), "плеер", "плеера", "плееров"),
                        ev={"step": "dubs", "ep": ep})
    out = {}  # dub_key → {"name", "items": [(src, source)]}
    for (mod, _), lst in got:
        for s in lst:
            d = out.setdefault(dub_key(s.title), {"name": dub_name(s.title), "items": []})
            d["items"].append((mod, s))
    summary("🎙 ", f"серия {ep}: [bold]{plural(len(out), 'озвучка', 'озвучки', 'озвучек')}[/]", board)
    return out, bool(got)  # got без озвучек — серия анонсирована, но видео ещё нет


def host(src):
    mod, s = src
    h = urlparse(s.url).netloc.removeprefix("www.").split(".")[0] if s.url.startswith("http") else ""
    return f"{SOURCES[mod]} · {h}" if h and h not in SOURCES[mod] else SOURCES[mod]


def best_video(dub, skip=()):
    """Лучшее рабочее видео озвучки. skip — исходные адреса, которые уже не запустились в IINA."""
    def text(vids):
        if not vids:  # animego иногда отдаёт пустой список; max() от него ронял весь CLI
            return "нет видео"
        top = max(vids, key=lambda v: v.quality or 0)
        return f"{top.quality}p {top.type}"

    got, board = gather(lambda it: it[1].get_videos(), dub["items"], 20, f"🎬 Достаю видео · {dub['name']}",
                        host, text, ev={"step": "video", "dub": dub["name"]})
    found = [(v, src) for src, vids in got for v in vids if v.url not in skip]
    # выше качество лучше; при равном не-kodik и HLS/mp4 надёжнее; хосты, отдавшие мусор, в конец
    found.sort(key=lambda x: (urlparse(x[0].url).netloc not in JUNK, x[0].quality or 0,
                              "kodik" not in host(x[1]), x[0].type != "mpd"), reverse=True)
    skipped = []
    with con.status(f"[{PINK}]Проверяю, что видео отдаётся…[/]", spinner="dots"):
        for v, src in found:
            # kodik проверяем и играем через локальный прокси (kproxy.py): к его узлам отсюда теряется ~1/3
            # соединений и mpv на этом виснет. Остальным прокси не нужен: AniDUB через него стартовал
            # в ~4 раза медленнее (прокси отдаёт сегмент целиком), aniboom притормаживал на старте
            kod = "kodik" in host(src) and start_proxy()
            url = kproxy.local(v.url, v.headers) if kod else v.url
            ok, reason, final = alive(SimpleNamespace(url=url, type=v.type, headers={} if kod else v.headers))
            if ok and not kod and v.type == "m3u8" and start_proxy():
                url = kproxy.local(v.url, v.headers, "m")  # без лишних качеств mpv стартует в разы быстрее
            log(step="probe", dub=dub["name"], src=host(src), host=urlparse(v.url).netloc,
                node=final and urlparse(final).netloc, quality=v.quality, type=v.type, result="ok" if ok else reason)
            if ok:
                v = SimpleNamespace(url=url, quality=v.quality, type=v.type, headers={} if kod else v.headers,
                                    src_url=v.url)
                break
            skipped.append(f"{host(src)} {v.quality}p: {reason}")
        else:
            v = None
    for s in skipped:
        say(f"пропустил {s}", icon="↷", style="yellow")
    if not v:
        return None, None
    summary("🎬", f"лучшее: [bold]{v.quality}p[/] через {host(src)}", board)
    return v, host(src)


JUNK = set()  # хосты, которые вместо видео отдали не то (old.tut-video.net: код 203 и мусор вместо mpd)
BROWSER_UA = kproxy.UA  # тот же UA, что у прокси: AniDUB привязывает ссылку к нему


def alive(v):
    """Отдаёт ли ссылка видео. Возвращает (ok, причина, адрес для плеера).
    kodik (cloud2.solodcdn.com) на каждый запрос перенаправляет на случайный узел, часть узлов отсюда
    не отвечает. Поэтому при таймауте пробуем ещё раз, а плееру отдаём адрес узла, который ответил:
    сегменты в плейлисте относительные и пойдут с него же."""
    for _ in range(3):
        ok, reason, final = probe(v, v.url)
        if ok or reason != "не отвечает":
            break
    return ok, reason, final


def probe(v, url):
    """Первые байты: похоже ли на m3u8/mpd/mp4. У мастер-m3u8 проверяем ещё плейлист качества.
    Мусор запоминаем по хосту в JUNK; таймаут не запоминаем, он бывает разовым."""
    head = b""
    try:
        with httpx.stream("GET", url, headers={"User-Agent": BROWSER_UA, **(v.headers or {})},
                          timeout=4, follow_redirects=True) as r:
            for chunk in r.iter_bytes():
                head += chunk
                if len(head) >= 4096:
                    break
    except httpx.HTTPError as e:
        return False, why(e), None
    h = head.lstrip()[:4096]
    sig = {"m3u8": h.startswith(b"#EXTM3U"), "mpd": b"<MPD" in h, "mp4": b"ftyp" in h[:64]}.get(v.type, bool(h))
    if r.status_code in (200, 206) and sig:
        sub = re.search(rb"#EXT-X-STREAM-INF[^\n]*\n\s*(\S+)", h) if v.type == "m3u8" and url == v.url else None
        if sub:
            ok, reason, _ = probe(v, str(r.url.join(sub[1].decode())))
            return ok, reason, str(r.url) if ok else None
        return True, "", str(r.url)
    JUNK.add(urlparse(url).netloc)
    return False, (f"HTTP {r.status_code}" if r.status_code not in (200, 206) else f"не {v.type}"), None


SOCK = f"/tmp/anime-iina-{os.getuid()}.sock"  # короткий путь: у unix-сокета лимит ~104 символа


def start_proxy():
    """Поднимает kproxy в фоне, если он ещё не слушает. False — не поднялся, тогда играем напрямую."""
    base = f"http://127.0.0.1:{kproxy.PORT}"
    ping = lambda: httpx.get(base + "/ping", timeout=0.5).text
    with contextlib.suppress(httpx.HTTPError):
        if ping() == kproxy.VERSION:
            return True
        httpx.get(base + "/quit", timeout=0.5)  # работает прокси со старым кодом — меняем на новый
        time.sleep(0.3)
    with open(f"/tmp/anime-kproxy-{os.getuid()}.log", "a") as logf:
        subprocess.Popen([sys.executable, os.path.join(os.path.dirname(HISTORY), "kproxy.py")],
                         stdout=subprocess.DEVNULL, stderr=logf, start_new_session=True)
    for _ in range(30):
        time.sleep(0.1)
        with contextlib.suppress(httpx.HTTPError):
            if ping() == kproxy.VERSION:
                return True
    return False


def prop(name):
    """Свойство mpv из окна IINA; None — окна нет или свойства пока нет."""
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(2)
            s.connect(SOCK)
            f = s.makefile("rwb")
            f.write(json.dumps({"command": ["get_property", name], "request_id": 7}).encode() + b"\n")
            f.flush()
            for line in f:  # в сокет сыплются и события mpv, ждём именно наш ответ
                r = json.loads(line)
                if r.get("request_id") == 7:
                    return r.get("data")
    except (OSError, ValueError):
        return None


def started(wait=15):
    """Пошло ли видео в IINA: позиция появилась и растёт."""
    t0, first = time.monotonic(), None
    with con.status(f"[{PINK}]Жду, пока IINA начнёт играть…[/]", spinner="dots"):
        while time.monotonic() - t0 < wait:
            time.sleep(0.5)
            pos = prop("time-pos")
            if isinstance(pos, (int, float)):
                if first is None:
                    first = pos
                elif pos > first:
                    return True
    return False


def ipc(*cmds):
    """Шлёт команды в mpv внутри открытого окна IINA. False — окна нет (закрыто)."""
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(3)
            s.connect(SOCK)
            f = s.makefile("rwb")
            for c in cmds:
                f.write(json.dumps({"command": c}).encode() + b"\n")
                f.flush()
                if json.loads(f.readline()).get("error") != "success":
                    return False
        return True
    except (OSError, ValueError):
        return False


# без таймаута mpv вечно ждёт соединение, которое потерялось; с ним — переподключается и повторяет сегмент
MPV_NET = {"network-timeout": "5", "stream-lavf-o": "reconnect=1,reconnect_on_network_error=1,reconnect_delay_max=2",
           "demuxer-lavf-o": "seg_max_retry=10"}


def play(video, title):
    log(step="play", title=title, host=urlparse(video.url).netloc, quality=video.quality, type=video.type)
    headers = [f"{k}: {v}" for k, v in (video.headers or {}).items()]
    # окно уже открыто → грузим серию в него же, а не плодим копии IINA
    if ipc(["change-list", "http-header-fields", "clr", ""],
           *[["change-list", "http-header-fields", "append", h] for h in headers],
           ["set_property", "force-media-title", title],
           ["set_property", "user-agent", BROWSER_UA],  # AniDUB привязывает ссылку к User-Agent запросившего
           *[["set_property", k, v] for k, v in MPV_NET.items()],
           ["set_property", "pause", False],  # пауза переживает смену файла, а новая серия должна идти
           ["loadfile", video.url, "replace"]):
        return
    with contextlib.suppress(FileNotFoundError):
        os.remove(SOCK)
    args = ["iina", "--keep-running", "--no-stdin", video.url, "--",
            f"--input-ipc-server={SOCK}", f"--force-media-title={title}", f"--user-agent={BROWSER_UA}",
            *[f"--{k}={v}" for k, v in MPV_NET.items()]]
    # по флагу на заголовок: в общем списке mpv режет по запятым, а они бывают в User-Agent
    args += [f"--http-header-fields-append={h}" for h in headers]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


# ---------- главный цикл ----------

LOGO = ["▄▀█ █▄ █ █ █▀▄▀█ █▀▀", "█▀█ █ ▀█ █ █ ▀ █ ██▄"]


def banner():
    grad = ["#ff79c6", "#f37ad1", "#e67cdc", "#d881e6", "#ca88ef", "#bd93f9"]
    for line in LOGO:
        t = Text(" ")
        for i, ch in enumerate(line):
            t.append(ch, f"bold {grad[i * len(grad) // len(line)]}")
        con.print(t)
    con.print(f" [dim]аниме с русской озвучкой · {len(SOURCES)} источников → IINA[/]\n")


def now_playing(group, ep, total, name, dub, video, via):
    body = Text.assemble((group["title"], "bold white"), "\n",
                         ("📺 ", ""), (f"Серия {ep}", f"bold {PINK}"), (f" из {total}", "dim"),
                         (f"  {name}" if name.startswith("«") else f"  «{name}»" if name else "", "italic"), "\n",
                         ("🎙  ", ""), (dub, "bold"), ("   🎬 ", ""), (f"{video.quality}p", "bold green"),
                         ("   🌐 ", ""), (via, "dim"))
    con.print(Panel(body, title="[bold]▶ сейчас в IINA[/]", title_align="left",
                    border_style=PINK, padding=(0, 2), expand=False))


def ask_query():
    try:
        return con.input(f"[bold {PINK}]🔎 Что ищем[/] [dim](рус/eng)[/] [bold {PINK}]❯[/] ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def choose_title(hist):
    """Возвращает (group, query, resume) или None; resume=True — продолжаем из истории."""
    query = " ".join(sys.argv[1:]).strip()
    resume = None
    if not query and hist:
        recent = sorted(hist.values(), key=lambda h: -h["ts"])[:15]
        rows = [pink("🔎 Новый поиск")] + [
            f"🍿 \033[1m{h['title']}\033[0m  {pink('серия ' + str(h['ep']))}  {dim('🎙 ' + h['dub'] + ' · ' + ago(h['ts']))}"
            for h in recent]
        i = pick(rows, "🌸 anime", header="Продолжить или найти новое")
        if i is None:
            return None
        if i > 0:
            resume = recent[i - 1]
            query = resume["query"]
    while True:
        if not query:
            query = ask_query()
            if not query:
                return None
        groups = search(query)
        if resume:
            g = next((g for g in groups if title_key(g["title"]) == resume["key"]), None)
            if g:
                return g, query, True
            say(f"«{resume['title']}» сейчас не нашёлся", hint="выбери из списка вручную")
            resume = None
        if not groups:
            say("Ничего не нашёл", hint="попробуй иначе: по-русски, по-английски или короче")
            query = ""
            continue
        w = max(len(g["title"]) for g in groups[:30]) + 2
        rows = [f"{kind_icon(g['info'])} {g['title']:<{w}}\033[33m{g['info']:<26}\033[0m"
                f"{' '.join(tag(SOURCES[m]) for m, _ in g['items'])}" for g in groups]
        i = pick(rows, "🎬 Тайтлы", header=f"Найдено по «{query}» · справа год, тип и источники")
        if i is None:
            query = ""
            continue
        return groups[i], query, False


def watch(group, query, resume, hist):
    key = title_key(group["title"])
    eps, names = episodes(group, query)
    if not eps:
        say("Серий не нашлось ни в одном источнике", hint="попробуй другой тайтл из списка")
        return "search"
    nums = sorted(eps)
    h = hist.get(key, {})
    ep = None  # список серий, курсор на следующей после просмотренной
    dk = h.get("dub_key") if resume else None  # продолжаем — та же озвучка без вопроса; None → спросить
    while True:
        if ep is None:
            seen = h.get("ep", 0)
            nxt = next((i for i, n in enumerate(nums) if n > seen), 0)
            rows = []
            for i, n in enumerate(nums):
                mark = "\033[32m✓\033[0m" if n <= seen else pink("● дальше") if i == nxt and seen else " "
                rows.append(f"{pink(f'{n:>4}')}  {names.get(n, '') or dim('серия ' + str(n))}  {mark}")
            i = pick(rows, f"📺 {group['title']}", header=f"{plural(len(nums), 'серия', 'серии', 'серий')}"
                     + (f" · смотрел до {seen}" if seen else ""), pos=nxt + 1)
            if i is None:
                return "search"
            ep = nums[i]
        ds, answered = dubs(eps[ep], ep)
        if not ds:
            if answered:
                say(f"Серия {ep} ещё не вышла: в источнике есть только анонс, видео нет", hint="выбери другую серию или зайди позже")
            else:
                say("Для этой серии источники не ответили", hint="выбери другую серию или попробуй позже")
            ep = None
            continue
        if dk not in ds:
            # сначала озвучка, которую смотрел в этом тайтле, потом где больше источников
            order = sorted(ds, key=lambda k: (k != h.get("dub_key"), "субтитр" in k, -len(ds[k]["items"]), ds[k]["name"].lower()))
            w = max(len(ds[k]["name"]) for k in order) + 2
            rows = []
            for k in order:
                d = ds[k]
                icon = "⭐" if k == h.get("dub_key") else "💬" if "субтитр" in k else "🎙 "
                srcs = sorted({SOURCES[m] for m, _ in d["items"]})
                rows.append(f"{icon} {d['name']:<{w}}{pink('●' * len(srcs))}{dim('○' * (3 - len(srcs)) if len(srcs) < 3 else '')}  "
                            f"{' '.join(tag(x) for x in srcs)}")
            i = pick(rows, f"🎙  Озвучка · серия {ep}", header="⭐ твоя прошлая · ● сколько источников (больше — надёжнее)")
            if i is None:
                ep = None
                continue
            dk = order[i]
        title = f"{group['title']} — {ep} · {ds[dk]['name']}"
        failed = set()  # плееры, которые не пошли в IINA: берём следующий
        while (got := best_video(ds[dk], failed))[0]:
            video, via = got
            play(video, title)
            if started():
                break
            say(f"{via} не пошёл в IINA за 15с", icon="↷", style="yellow", hint="ставлю следующий плеер")
            log(step="watchdog", title=title, src=via, result="не стартовал")
            failed.add(video.src_url)
        else:
            say(f"{ds[dk]['name']} не отдала рабочего видео", hint="выбери другую озвучку")
            dk = None
            continue
        h = hist[key] = {"title": group["title"], "key": key, "query": query, "ep": ep,
                         "dub": ds[dk]["name"], "dub_key": dk, "ts": time.time()}
        save_history(hist)
        now_playing(group, ep, len(nums), names.get(ep, ""), ds[dk]["name"], video, via)
        later = [n for n in nums if n > ep]
        actions = ([("next", f"⏭  Следующая серия {pink(later[0])}  {dim(names.get(later[0], ''))}")] if later else []) + [
            ("pick", "📋 Выбрать серию"), ("dub", "🎙  Другая озвучка"),
            ("search", "🔎 Новый поиск"), ("exit", "👋 Выход")]
        i = pick([t for _, t in actions], "⚡ Дальше", header=title)
        choice = actions[i][0] if i is not None else "exit"
        if choice == "next":
            ep = later[0]  # та же озвучка; если её нет у следующей серии, спросим
        elif choice == "pick":
            ep, dk = None, None
        elif choice == "dub":
            dk = None
        else:
            return choice


def selftest():
    assert title_key("Чёрный клевер / Black Clover [1-170 из 170]") == title_key("Черный Клевер")
    assert title_key("Чёрный Клевер (Спэшл)") != title_key("Чёрный клевер")
    assert dub_key("Озвучка AniLibria") == dub_key("Aniliberty") == "anilibria"
    assert dub_key("Dream Cast") == dub_key("Dreamcast")
    assert dub_key("Студийная Банда") == dub_key("StudioBand") and dub_key("AniDub Online") == dub_key("AniDUB")
    assert title_words("Фрирен: провожающая в последний путь") == title_words("Провожающая в последний путь Фрирен")
    assert title_words("Фрирен, провожающая в последний путь 2") != title_words("Провожающая в последний путь Фрирен")
    pl = kproxy.rewrite("#EXTM3U\n#EXTINF:6,\n./720.mp4:hls:seg-1-v1-a1.ts\n", "https://cdn.x/a/720.mp4:hls:manifest.m3u8", "")
    seg = pl.splitlines()[2]
    assert seg.startswith("/s/") and seg.endswith("/x.ts"), seg
    assert kproxy.base64.urlsafe_b64decode(seg.split("/")[2]).decode() == "https://cdn.x/a/720.mp4:hls:seg-1-v1-a1.ts"
    assert kproxy.rewrite("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhi.m3u8\n", "https://cdn.x/m.m3u8", "").splitlines()[2].startswith("/p/")
    assert 'URI="/p/' in kproxy.rewrite('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,URI="a.m3u8"\n', "https://cdn.x/m.m3u8", "")
    m = kproxy.slim('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="au.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=5,AUDIO="a"\nlo.m3u8\n'
                    '#EXT-X-STREAM-INF:BANDWIDTH=9,AUDIO="a"\nhi.m3u8\n', "https://cdn.x/v/m.m3u8")
    assert "https://cdn.x/v/hi.m3u8" in m and "lo.m3u8" not in m and 'URI="https://cdn.x/v/au.m3u8"' in m, m
    print("ok")


HELP = """[bold]anime[/] — аниме с русской озвучкой из терминала в IINA

[bold]Использование[/]
  anime                 продолжить просмотр или найти новое
  anime [i]название[/]        сразу искать (рус или eng)

[bold]Как это работает[/]
  🔎 ищет в yummy и animego, серии и озвучки собирает со всех {n} источников → 🎬 тайтл → 📺 серия → 🎙  озвучка → ▶ IINA
  Серии идут в одно окно IINA, история в history.json рядом со скриптом.

[bold]Клавиши[/]  ↑↓ выбрать · Enter ок · Esc назад · печатай для фильтра
[bold]Цвет[/]     выключается через NO_COLOR=1"""


def main():
    if sys.argv[1:] == ["--selftest"]:
        return selftest()
    if sys.argv[1:2] in (["-h"], ["--help"]):
        return con.print(Panel(HELP.format(n=len(SOURCES)), border_style=PINK, expand=False))
    banner()
    hist = load_history()
    while True:
        got = choose_title(hist)
        if not got or watch(*got, hist) == "exit":
            break
        sys.argv[1:] = []  # «Новый поиск» → спросить заново
    con.print(f"\n[bold {PINK}]👋 Приятного просмотра![/]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    sys.stdout.flush()
    os._exit(0)  # не ждём зависшие запросы к недоступным источникам
