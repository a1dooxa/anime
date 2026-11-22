"""AniDUB (anidub.com): свой плеер с HLS. Интерфейс как у anicli_api: Extractor().search → get_anime →
get_episodes → get_sources → get_videos. Разметку проверили 2026-11-22."""
import html
import re
from types import SimpleNamespace
from urllib.parse import urlencode, urlparse

import httpx

SITE = "https://anidub.com"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
http = httpx.Client(headers=UA, timeout=10, follow_redirects=True)


def get(url, **kw):
    r = http.request("POST" if "data" in kw else "GET", url, **kw)
    r.raise_for_status()
    return r.text


class Source:
    title = "AniDUB"

    def __init__(self, url):
        self.url = url  # vid.php: мастер-плейлист 720p/480p, IINA открывает его сама

    def get_videos(self):
        return [SimpleNamespace(url=self.url, quality=720, type="m3u8", headers={})]


class Episode:
    def __init__(self, num, url):
        self.num, self.title, self.url = num, f"{num} серия", url

    def get_sources(self):
        return [Source(self.url)]


class Anime:
    def __init__(self, url):
        self.url = url

    def get_episodes(self):
        iframe = re.search(r'id="playeriframe" src="([^"]+)"', get(self.url))
        if not iframe:
            return []
        player = html.unescape(iframe[1])
        base = "{0.scheme}://{0.netloc}".format(urlparse(player))
        opts = re.findall(r'<option value="(/s\d+/\w+)"[^>]*>\s*(\d+)\s*серия', get(player))
        return [Episode(n, f"{base}/vid.php?{urlencode({'v': v})}") for v, n in opts]


class Result:
    def __init__(self, title, url):
        self.title, self.url = title, url

    def get_anime(self):
        return Anime(self.url)

    def __repr__(self):
        return f"anidub {self.title}"


class Extractor:
    def search(self, query):
        page = get(f"{SITE}/index.php?do=search", data={"do": "search", "subaction": "search", "story": query})
        out = []
        for item in page.split('class="th-item"')[1:]:
            url = re.search(r'class="th-in" href="([^"]+)"', item)
            title = re.search(r'class="th-title">([^<]+)', item)
            if not (url and title):
                continue
            t = re.sub(r"\s*\[.*?\]", "", html.unescape(title[1])).strip()
            season = re.search(r"<span>(\d+) сезон</span>", item)
            if season and season[1] != "1":  # у AniDUB 2 сезон назван как первый, номер только в подписи
                t = f"{t.split(' / ')[0]} {season[1]}"
            out.append(Result(t, url[1]))
        return out
