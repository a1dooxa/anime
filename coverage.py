"""Замер: доводит ли anime до вышедшей серии в нужной озвучке, и почему нет.

Запуск: .venv/bin/python coverage.py  →  reports/ГГГГ-ММ-ДД.json (все записи) и .md (сводка).
Набор: тайтлы из history.json + 5 популярных онгоингов Shikimori, последние 3 вышедшие серии каждого.
«Играет» значит, что ffmpeg декодировал 5 секунд видео с заголовками этого видео.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

import anime

ROOT = os.path.dirname(os.path.abspath(__file__))
SHIKI = httpx.Client(base_url="https://shikimori.one/api", headers={"User-Agent": "anime-cli"}, timeout=15,
                    follow_redirects=True)
DECODE = ThreadPoolExecutor(3)  # при 8 параллельных ffmpeg kodik давал ложные таймауты
anime.RUN = "coverage"
LAST = 3  # сколько последних вышедших серий проверять у тайтла
ONGOINGS = 5
OTHER_DUBS = 3  # сколько чужих озвучек пробовать, если моя не играет


def shiki(path, **params):
    for _ in range(5):
        time.sleep(0.35)  # у Shikimori лимит ~5 запросов в секунду, при превышении отдаёт пустой ответ
        r = SHIKI.get(path, params=params)
        if r.status_code == 200 and r.content:
            return r.json()
        time.sleep(2)
    raise RuntimeError(f"shikimori {path}: {r.status_code}")


def shiki_find(title):
    for a in shiki("/animes", search=title, limit=10):
        if anime.title_words(a["russian"] or "") == anime.title_words(title):
            return a["id"]
    return None


def found_ids(text):
    """id Shikimori/MAL из ссылок или полей источника."""
    ids = set(re.findall(r"(?:shikimori|myanimelist)\.\w+/anime\w*/[a-z]*(\d+)", text))
    ids |= set(re.findall(r"'(?:shikimori_id|myanimelist_id)': (\d+)", text))
    return {int(i) for i in ids}


def decode(v):
    """ffmpeg декодирует 5 секунд видео. Возвращает (ok, кадров, ошибка, секунд)."""
    hdr = "".join(f"{k}: {val}\r\n" for k, val in (v.headers or {}).items())
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-progress", "pipe:1", "-rw_timeout", "20000000"]
    cmd += (["-headers", hdr] if hdr else []) + ["-i", v.url, "-t", "5", "-map", "0:v:0", "-f", "null", "-"]
    t = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, 0, "ffmpeg: 120с без результата", 120.0
    frames = max([int(x) for x in re.findall(r"frame=(\d+)", r.stdout)] or [0])
    err = r.stderr.strip().splitlines()[-1][:200] if r.stderr.strip() else ""
    return r.returncode == 0 and frames >= 60, frames, err, round(time.monotonic() - t, 1)


def try_source(mod, src, dub):
    """Один плеер одной озвучки: get_videos → лучшее видео → декодирование."""
    rec = {"src": anime.SOURCES[mod], "dub": dub, "player": anime.host((mod, src))}
    t = time.monotonic()
    try:
        vids = src.get_videos()
    except Exception as e:
        return {**rec, "ok": False, "stage": "get_videos", "cause": anime.cause(e),
                "err": f"{type(e).__name__}: {str(e)[:200]}", "sec_video": round(time.monotonic() - t, 1)}
    rec["sec_video"] = round(time.monotonic() - t, 1)
    if not vids:
        return {**rec, "ok": False, "stage": "get_videos", "cause": "пустой список видео", "err": ""}
    v = max(vids, key=lambda x: (x.quality or 0, x.type != "mpd"))
    ok, frames, err, sec = decode(v)
    if not ok and "timed out" in err:  # таймаут бывает разовым: один повтор
        ok, frames, err, sec2 = decode(v)
        sec += sec2
    return {**rec, "ok": ok, "stage": "decode", "quality": v.quality, "type": v.type,
            "host": httpx.URL(v.url).host, "frames": frames, "err": err, "sec_decode": sec,
            "cause": "" if ok else "ffmpeg не декодировал"}


def try_dubs(ds, keys):
    futs = [DECODE.submit(try_source, m, s, ds[k]["name"]) for k in keys for m, s in ds[k]["items"]]
    return [f.result() for f in futs]


def check_episode(n, eps, my, date):
    rec = {"ep": n, "date": date and date.date().isoformat(),
           "lag_days": date and round((dt.datetime.now(dt.timezone.utc) - date).total_seconds() / 86400, 1)}
    if n not in eps:
        return {**rec, "outcome": "не найдена", "why": "ни один источник не отдал серию с таким номером"}
    t = time.monotonic()
    ds, _ = anime.dubs(eps[n], n)
    rec["sec_dubs"] = round(time.monotonic() - t, 2)
    rec["dubs"] = sorted(ds)
    rec["ep_sources"] = sorted({anime.SOURCES[m] for m, _ in eps[n]})
    if not ds:
        return {**rec, "outcome": "найдена, видео нет", "why": "ни одного плеера"}
    mine = [k for k in my if k in ds]
    rec["tries"] = try_dubs(ds, mine)
    if mine:  # что запустит CLI: best_video по первой моей озвучке, и играет ли именно это видео
        t = time.monotonic()
        v, via = anime.best_video(ds[mine[0]])
        rec["cli"] = {"dub": ds[mine[0]]["name"], "via": via, "sec_video": round(time.monotonic() - t, 2)}
        if v:
            ok, frames, err, _ = decode(v)
            rec["cli"].update(quality=v.quality, type=v.type, ok=ok, frames=frames, err=err)
        if rec["cli"].get("ok"):
            return {**rec, "outcome": "играет в моей"}
        if any(x["ok"] for x in rec["tries"]):
            return {**rec, "outcome": "моя есть, CLI запускает неиграющий", "why": f"CLI выбрал {via}: {rec['cli'].get('err', 'видео нет')}"}
    others = sorted((k for k in ds if k not in my and "субтитр" not in k), key=lambda k: -len(ds[k]["items"]))[:OTHER_DUBS]
    more = try_dubs(ds, others)
    rec["tries"] += more
    if any(x["ok"] for x in more):
        return {**rec, "outcome": "играет только в другой",
                "why": "моей озвучки нет у серии" if not mine else "моя озвучка не декодировалась"}
    return {**rec, "outcome": "найдена, видео нет", "why": "ни одна проверенная озвучка не декодировалась"}


def audit(group, eps, query, sid, aired):
    """Совпадения по каждому источнику: найден ли тайтл и тот ли он."""
    rows = []
    used = dict(group["items"])
    for mod in anime.SOURCES:
        row = {"src": anime.SOURCES[mod]}
        nums = sorted(n for n in eps if any(m == mod for m, _ in eps[n]))
        try:
            r = used.get(mod) or (anime.find_in(mod, group, query) if mod in anime.STUDIOS else None)
        except Exception as e:
            r, row["find_err"] = None, f"{type(e).__name__}: {str(e)[:120]}"
        if r is None:
            row["found"] = False
            cands = []
            for q in dict.fromkeys((query, group["title"])):
                try:
                    cands += [x.title for x in anime.extractor(mod).search(q)[:4]]
                except Exception as e:
                    cands.append(f"поиск упал: {type(e).__name__}")
            row["candidates"] = list(dict.fromkeys(cands))[:6]
            rows.append(row)
            continue
        row.update(found=True, title=r.title, eps=len(nums), first=nums[0] if nums else None, last=nums[-1] if nums else None)
        text = repr(getattr(r, "data", "")) + repr(r)
        if mod == "anilibria":  # id лежит только на странице тайтла
            try:
                text += repr(r.get_anime())
            except Exception:
                pass
        ids = found_ids(text)
        if ids and sid:
            row["verdict"] = "id совпал" if sid in ids else f"ДРУГОЙ ТАЙТЛ: id {sorted(ids)} ≠ {sid}"
        elif aired and nums:
            row["verdict"] = "без id; серии сходятся" if nums[-1] == aired else f"без id; последняя серия {nums[-1]} при вышедших {aired}"
        else:
            row["verdict"] = "без id; сверить нечем"
        rows.append(row)
    return rows


def titles():
    hist = anime.load_history()
    out = [{"title": h["title"], "query": h["query"], "my": [h["dub_key"]], "from": "история"} for h in hist.values()]
    seen = {tuple(anime.title_words(t["title"])) for t in out}
    for a in shiki("/animes", status="ongoing", order="popularity", kind="tv", limit=15):
        if len([t for t in out if t["from"] == "онгоинг"]) >= ONGOINGS:
            break
        if a["russian"] and tuple(anime.title_words(a["russian"])) not in seen:
            out.append({"title": a["russian"], "query": a["russian"], "my": ["anilibria", "anidub"],
                        "from": "онгоинг", "shiki_id": a["id"]})
    return out


def run_title(t):
    rec = dict(t)
    t0 = time.monotonic()
    groups = anime.search(t["query"])
    rec["sec_search"] = round(time.monotonic() - t0, 2)
    group = next((g for g in groups if anime.title_key(g["title"]) == anime.title_key(t["title"])), None)
    sid = t.get("shiki_id")
    if group:
        yummy = dict(group["items"]).get("yummy_anime")
        ids = found_ids(repr(getattr(yummy, "data", ""))) if yummy else set()
        sid = sid or (min(ids) if ids else None)
    sid = sid or shiki_find(t["title"])
    rec["shiki_id"] = sid
    info = shiki(f"/animes/{sid}") if sid else {}
    # у вышедших тайтлов episodes_aired бывает меньше episodes (Фрирен: 27 при 28), верим episodes
    aired = info.get("episodes") if info.get("status") == "released" else info.get("episodes_aired") or 0
    rec.update(shiki_status=info.get("status"), aired=aired, next_episode_at=info.get("next_episode_at"))
    nxt = info.get("next_episode_at") and dt.datetime.fromisoformat(info["next_episode_at"])
    targets = list(range(max(1, aired - LAST + 1), aired + 1)) if aired else []
    if not group:
        rec["episodes"] = [{"ep": n, "outcome": "не найдена", "why": "тайтл не найден в поиске"} for n in targets]
        rec["search_titles"] = [g["title"] for g in groups[:8]]
        return rec
    rec["search_sources"] = [anime.SOURCES[m] for m, _ in group["items"]]
    t1 = time.monotonic()
    eps, _ = anime.episodes(group, t["query"])
    rec["sec_episodes"] = round(time.monotonic() - t1, 2)
    rec["found_eps"] = len(eps)
    rec["max_ep"] = max(eps) if eps else 0
    # даты серий по Shikimori есть только в виде «следующая выйдет тогда-то»; прошлые считаем шагом в неделю
    date = lambda n: nxt and nxt - dt.timedelta(days=7 * (aired + 1 - n))
    rec["episodes"] = [check_episode(n, eps, t["my"], date(n)) for n in targets]
    rec["audit"] = audit(group, eps, t["query"], sid, aired)
    return rec


def render(data):
    eps = [(t, e) for t in data["titles"] for e in t["episodes"]]
    outcomes = ["играет в моей", "моя есть, CLI запускает неиграющий", "играет только в другой", "найдена, видео нет", "не найдена"]
    L = [f"# Покрытие anime · {data['date']}", "",
         f"{len(data['titles'])} тайтлов, {len(eps)} вышедших серий. Прогон занял {data['sec_total']:.0f}с.", "",
         "## Исходы по сериям", "", "| исход | серий |", "|---|---|"]
    L += [f"| {o} | {sum(e['outcome'] == o for _, e in eps)} |" for o in outcomes]
    L += ["", "## По тайтлам", "", "| тайтл | откуда | моя | вышло (Shikimori) | нашли серий | поиск, с | серии, с | исходы |", "|---|---|---|---|---|---|---|---|"]
    for t in data["titles"]:
        oc = ", ".join(f"{e['ep']}: {e['outcome']}" for e in t["episodes"]) or "нет вышедших серий"
        L.append(f"| {t['title']} | {t['from']} | {'/'.join(t['my'])} | {t.get('aired')} ({t.get('shiki_status')}) | "
                 f"{t.get('max_ep', '—')} | {t.get('sec_search')} | {t.get('sec_episodes', '—')} | {oc} |")
    tries = [x for _, e in eps for x in e.get("tries", [])]
    L += ["", "## По источникам (каждая попытка плеера)", "", "| источник | попыток | играет | не играет | видео, с (медиана) | декод, с (медиана) |", "|---|---|---|---|---|---|"]
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else "—"
    for s in sorted({x["src"] for x in tries}):
        xs = [x for x in tries if x["src"] == s]
        L.append(f"| {s} | {len(xs)} | {sum(x['ok'] for x in xs)} | {sum(not x['ok'] for x in xs)} | "
                 f"{med([x['sec_video'] for x in xs if 'sec_video' in x])} | {med([x['sec_decode'] for x in xs if 'sec_decode' in x])} |")
    L += ["", "## Время до видео в CLI (поиск + серии + озвучки + best_video с проверкой)", ""]
    for t, e in eps:
        if e.get("cli", {}).get("ok"):
            tot = t["sec_search"] + t["sec_episodes"] + e["sec_dubs"] + e["cli"]["sec_video"]
            L.append(f"- {t['title']} · {e['ep']}: {tot:.1f}с · {e['cli']['via']} {e['cli']['quality']}p")
    L += ["", "## Каждый провал", ""]
    for t, e in eps:
        bad = [x for x in e.get("tries", []) if not x["ok"]]
        if e["outcome"] == "играет в моей" and bad:
            L.append(f"- {t['title']} · {e['ep']}: играет в моей, но не сработали плееры:")
        elif e["outcome"] != "играет в моей":
            L.append(f"- **{t['title']} · {e['ep']}** ({e.get('date') or 'дата ?'}, {e.get('lag_days', '?')} дн. назад): "
                     f"{e['outcome']} — {e.get('why', '')}; серия у: {', '.join(e.get('ep_sources', [])) or '—'}")
        for x in e.get("tries", []):
            if not x["ok"]:
                L.append(f"  - {x['dub']} @ {x['player']}: {x['stage']} · {x['cause']} · {x.get('err', '')}")
    L += ["", "## Совпадения тайтлов по источникам", ""]
    for t in data["titles"]:
        L.append(f"**{t['title']}** (shikimori {t.get('shiki_id')}, вышло {t.get('aired')})")
        if t.get("search_titles") is not None:
            L.append(f"- в поиске не нашёлся; первые результаты: {'; '.join(t['search_titles'])}")
        for r in t.get("audit", []):
            if r["found"]:
                L.append(f"- {r['src']}: «{r['title']}» · серии {r['first']}–{r['last']} ({r['eps']} шт.) · {r['verdict']}")
            else:
                L.append(f"- {r['src']}: не найден {r.get('find_err', '')} · кандидаты: {'; '.join(r['candidates']) or '—'}")
        L.append("")
    return "\n".join(L)


def main():
    t0 = time.monotonic()
    data = {"date": dt.date.today().isoformat(), "titles": []}
    for t in titles():
        print(f"→ {t['title']}", file=sys.stderr)
        try:
            data["titles"].append(run_title(t))
        except Exception as e:
            data["titles"].append({**t, "error": f"{type(e).__name__}: {e}", "episodes": []})
    data["sec_total"] = time.monotonic() - t0
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    base = os.path.join(ROOT, "reports", data["date"])
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, default=str)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(render(data))
    print(base + ".md", flush=True)  # дальше os._exit, без flush строка теряется


if __name__ == "__main__":
    main()
    os._exit(0)  # не ждём зависшие запросы к недоступным источникам
