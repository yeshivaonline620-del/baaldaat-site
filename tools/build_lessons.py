#!/usr/bin/env python3
"""
Автоматически собирает раздел «Записи уроков» из YouTube-канала.

Что делает:
  1. Читает ленту канала (RSS, ключ не нужен). Если задан YOUTUBE_API_KEY —
     дополнительно подтягивает ВЕСЬ архив канала через YouTube Data API.
  2. Накапливает уроки в data/lessons.json (архив не теряется, даже если
     в ленте остаются только последние 15 видео).
  3. Создаёт отдельную страницу на каждый урок: site/zapisi/<название>-<id>.html
     (с видео, описанием, разметкой VideoObject для Google).
  4. Обновляет карточки в site/uroki.html и адреса в site/sitemap.xml.

Только стандартная библиотека Python, ничего устанавливать не нужно.

Запуск:        python tools/build_lessons.py
Проверка без интернета:  python tools/build_lessons.py --feed-file пример.xml
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# ----------------------------- НАСТРОЙКИ ------------------------------------
CHANNEL_ID = "UCFg8kRZa7c_ioeS5gWaIc2w"
CHANNEL_URL = "https://www.youtube.com/@ShalomNahshon"
SITE_URL = "https://baaldaat.org"
BRAND = "Ешива Онлайн — דעת"
OUT_DIR = "zapisi"          # папка со страницами уроков
MIN_SECONDS = 90            # короче — считаем Shorts и не показываем (если длительность известна)
MAX_ON_UROKI = 60           # сколько карточек показывать на странице «Уроки»
RELATED = 6                 # сколько «Других уроков» под видео
# -----------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
DATA_FILE = ROOT / "data" / "lessons.json"
HIDDEN_FILE = ROOT / "data" / "hidden.txt"

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
UA = {"User-Agent": "Mozilla/5.0 (compatible; baaldaat-lessons-bot/1.0)"}

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
TRANSLIT = dict(zip("абвгдезийклмнопрстуфхцчшщыэюя",
                    ["a", "b", "v", "g", "d", "e", "z", "i", "y", "k", "l", "m", "n", "o", "p",
                     "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "y", "e", "yu", "ya"]))
TRANSLIT.update({"ё": "e", "ж": "zh", "ъ": "", "ь": ""})


# ------------------------------ получение данных -----------------------------
def http_get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Не удалось загрузить {url}: {last}")


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for e in root.findall("a:entry", NS):
        vid = (e.findtext("yt:videoId", default="", namespaces=NS) or "").strip()
        if not vid:
            continue
        desc = e.findtext("media:group/media:description", default="", namespaces=NS) or ""
        items.append({
            "id": vid,
            "title": (e.findtext("a:title", default="", namespaces=NS) or "").strip(),
            "description": desc.strip(),
            "published": (e.findtext("a:published", default="", namespaces=NS) or "").strip(),
        })
    return items


def api_get(path, params):
    params = dict(params, key=os.environ["YOUTUBE_API_KEY"])
    url = "https://www.googleapis.com/youtube/v3/" + path + "?" + urllib.parse.urlencode(params)
    return json.loads(http_get(url))


def iso_duration_seconds(iso):
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return None
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def fetch_api_archive():
    """Весь архив канала (нужен бесплатный ключ YOUTUBE_API_KEY)."""
    playlist = "UU" + CHANNEL_ID[2:]
    items, token = [], None
    while True:
        p = {"part": "snippet,contentDetails", "playlistId": playlist, "maxResults": 50}
        if token:
            p["pageToken"] = token
        d = api_get("playlistItems", p)
        for it in d.get("items", []):
            sn = it["snippet"]
            if sn.get("title") in ("Private video", "Deleted video"):
                continue
            items.append({
                "id": sn["resourceId"]["videoId"],
                "title": sn["title"].strip(),
                "description": (sn.get("description") or "").strip(),
                "published": it["contentDetails"].get("videoPublishedAt") or sn.get("publishedAt", ""),
            })
        token = d.get("nextPageToken")
        if not token:
            break
    # длительности
    for i in range(0, len(items), 50):
        chunk = items[i:i + 50]
        d = api_get("videos", {"part": "contentDetails", "id": ",".join(x["id"] for x in chunk)})
        dur = {v["id"]: v["contentDetails"]["duration"] for v in d.get("items", [])}
        for x in chunk:
            if x["id"] in dur:
                x["duration"] = dur[x["id"]]
    return items


# ------------------------------ хранение -------------------------------------
def slugify(title, vid):
    t = "".join(TRANSLIT.get(ch, ch) for ch in title.lower())
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:55].strip("-") or "urok"
    return f"{t}-{vid}"


def load_data():
    if DATA_FILE.exists():
        return {x["id"]: x for x in json.loads(DATA_FILE.read_text(encoding="utf-8"))}
    return {}


def merge(data, fresh):
    for it in fresh:
        old = data.get(it["id"], {})
        new = dict(old)
        new.update({k: v for k, v in it.items() if v})
        new["slug"] = old.get("slug") or slugify(it["title"], it["id"])  # адрес не меняется
        data[it["id"]] = new
    return data


def hidden_ids():
    if not HIDDEN_FILE.exists():
        return set()
    out = set()
    for line in HIDDEN_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.add(line)
    return out


def visible_lessons(data):
    hid = hidden_ids()
    res = []
    for x in data.values():
        if x["id"] in hid:
            continue
        sec = iso_duration_seconds(x.get("duration", ""))
        if sec is not None and sec < MIN_SECONDS:
            continue
        res.append(x)
    res.sort(key=lambda x: x.get("published", ""), reverse=True)
    return res


# ------------------------------ вспомогательное ------------------------------
def esc(s):
    return html.escape(s, quote=True)


def date_ru(iso):
    try:
        d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return f"{d.day} {MONTHS[d.month - 1]} {d.year}"
    except Exception:  # noqa: BLE001
        return ""


def date_ymd(iso):
    return (iso or "")[:10]


def short_desc(text, limit=155):
    t = re.sub(r"https?://\S+", "", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return "Запись урока по Каббале и Хасидизму на русском — Ешива Онлайн, דעת."
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0].rstrip(",;:—-– ")
    return cut + "…"


def description_html(text):
    text = (text or "").strip()[:4000]
    if not text:
        return ""
    paras = []
    for block in re.split(r"\n\s*\n", text):
        s = esc(block.strip())
        s = re.sub(r"(https?://[^\s<]+)",
                   r'<a href="\1" target="_blank" rel="nofollow noopener ugc">\1</a>', s)
        s = s.replace("\n", "<br>")
        if s:
            paras.append(f"<p>{s}</p>")
    return "\n".join(paras)


def jsonld(obj):
    return ('<script type="application/ld+json">'
            + json.dumps(obj, ensure_ascii=False).replace("</", "<\\/") + "</script>")


def thumb(vid, q="mqdefault"):
    return f"https://i.ytimg.com/vi/{vid}/{q}.jpg"


def card(x, prefix):
    return (f'<a class="lesson-card" href="{prefix}{OUT_DIR}/{x["slug"]}.html">'
            f'<span class="lesson-thumb"><img src="{thumb(x["id"])}" alt="" width="320" height="180" loading="lazy"></span>'
            f'<h3>{esc(x["title"])}</h3><small>{esc(date_ru(x.get("published", "")))}</small></a>')


# ------------------------------ шаблон страниц -------------------------------
REL = re.compile(r'(href|src)="(?!https?:|//|/|#|mailto:|tel:|data:|javascript:)([^"]+)"')


def absolutize(fragment):
    return REL.sub(lambda m: f'{m.group(1)}="/{m.group(2)}"', fragment)


def split_template():
    src = (SITE / "uroki.html").read_text(encoding="utf-8")
    i, j = src.index("<main"), src.index("</main>") + len("</main>")
    head, tail = absolutize(src[:i]), absolutize(src[j:])
    if "</body>" in tail and "lesson.js" not in tail:
        tail = tail.replace("</body>", '<script src="/assets/lesson.js" defer></script></body>', 1)
    return head, tail


def sub1(pattern, repl, text):
    new, n = re.subn(pattern, lambda m: repl(m) if callable(repl) else repl, text, count=1, flags=re.S)
    if n != 1:
        raise RuntimeError(f"В шаблоне не найден фрагмент: {pattern}")
    return new


def build_page(x, head, tail, others):
    title = x["title"]
    page_url = f"{SITE_URL}/{OUT_DIR}/{x['slug']}.html"
    desc = short_desc(x.get("description", ""))
    full_title = f"{title} — {BRAND}"[:110]
    h = head
    h = sub1(r"<title>.*?</title>", f"<title>{esc(full_title)}</title>", h)
    h = sub1(r'(<meta name="description" content=")[^"]*"', lambda m: m.group(1) + esc(desc) + '"', h)
    h = sub1(r'(<link rel="canonical" href=")[^"]*"', lambda m: m.group(1) + page_url + '"', h)
    h = sub1(r'(<meta property="og:type" content=")[^"]*"', lambda m: m.group(1) + 'video.other"', h)
    h = sub1(r'(<meta property="og:title" content=")[^"]*"', lambda m: m.group(1) + esc(full_title) + '"', h)
    h = sub1(r'(<meta property="og:description" content=")[^"]*"', lambda m: m.group(1) + esc(desc) + '"', h)
    h = sub1(r'(<meta property="og:url" content=")[^"]*"', lambda m: m.group(1) + page_url + '"', h)
    h = sub1(r'(<meta property="og:image" content=")[^"]*"', lambda m: m.group(1) + thumb(x["id"], "hqdefault") + '"', h)

    video = {
        "@context": "https://schema.org", "@type": "VideoObject",
        "name": title, "description": desc if desc else title,
        "thumbnailUrl": [thumb(x["id"], "hqdefault"), thumb(x["id"], "mqdefault")],
        "uploadDate": x.get("published", ""),
        "embedUrl": f"https://www.youtube.com/embed/{x['id']}",
        "inLanguage": "ru",
        "publisher": {"@type": "Organization", "name": BRAND, "url": SITE_URL + "/"},
    }
    if x.get("duration"):
        video["duration"] = x["duration"]
    crumbs = {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Главная", "item": SITE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": "Уроки", "item": SITE_URL + "/uroki.html"},
            {"@type": "ListItem", "position": 3, "name": title, "item": page_url},
        ]}
    h = h.replace("</head>", jsonld(video) + jsonld(crumbs) + "</head>", 1)

    watch = f"https://www.youtube.com/watch?v={x['id']}"
    body_desc = description_html(x.get("description", ""))
    more = "".join(card(o, "/") for o in others)
    more_block = (f'<section class="band alt"><div class="wrap"><h2>Другие уроки</h2>'
                  f'<div class="lessons-grid">{more}</div></div></section>') if more else ""
    main = f'''<main id="top" tabindex="-1">
<section class="phero dark navy"><div class="wrap"><p class="crumbs"><a href="/index.html">Главная</a> › <a href="/uroki.html">Уроки</a> › Запись урока</p><h1>{esc(title)}</h1><p class="lead">{esc(date_ru(x.get("published", "")))}</p></div></section>
<section class="band"><div class="wrap">
<div class="lesson-player" data-vid="{x['id']}" data-title="{esc(title)}"><button type="button" class="lesson-play" aria-label="Смотреть урок: {esc(title)}"><img src="{thumb(x['id'], 'hqdefault')}" alt="{esc(title)}" width="480" height="360"><span class="play" aria-hidden="true"></span></button><noscript><a href="{watch}" target="_blank" rel="noopener">Смотреть на YouTube</a></noscript></div>
<div class="lesson-text">{body_desc}</div>
<div class="btnrow lesson-actions"><a class="btn" href="{watch}" target="_blank" rel="noopener">Смотреть на YouTube</a><a class="btn ghost" href="{CHANNEL_URL}?sub_confirmation=1" target="_blank" rel="noopener">Подписаться на канал</a><a class="btn ghost" href="/share.html">Поделиться</a></div>
</div></section>
{more_block}
</main>'''
    return h + main + tail


# ------------------------------ запись на диск -------------------------------
def replace_between(path, start, end, content):
    s = path.read_text(encoding="utf-8")
    a, b = s.find(start), s.find(end)
    if a == -1 or b == -1:
        raise RuntimeError(f"В {path.name} нет маркеров {start} / {end}")
    new = s[:a + len(start)] + content + s[b:]
    if new != s:
        path.write_text(new, encoding="utf-8")
        return True
    return False


def write_if_changed(path, text):
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def build_uroki_block(lessons):
    if not lessons:
        return ('<p class="lead">Первые записи появятся здесь совсем скоро. А пока приходите на живые уроки: '
                'расписание на странице «Расписание».</p><div class="btnrow"><a class="btn ghost" '
                'href="raspisanie.html">Расписание</a></div>')
    cards = "".join(card(x, "") for x in lessons[:MAX_ON_UROKI])
    return (f'<p class="lead">Записи наших уроков. Новые появляются здесь автоматически.</p>'
            f'<div class="lessons-grid">{cards}</div>'
            f'<div class="btnrow"><a class="btn" href="{CHANNEL_URL}?sub_confirmation=1" target="_blank" rel="noopener">Подписаться на канал</a>'
            f'<a class="btn ghost" href="raspisanie.html">Расписание живых уроков</a></div>')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed-file", help="взять ленту из файла (для проверки без интернета)")
    args = ap.parse_args()

    data = load_data()
    fresh = []
    if args.feed_file:
        fresh += parse_feed(Path(args.feed_file).read_bytes())
    else:
        try:
            fresh += parse_feed(http_get(f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"))
        except Exception as e:  # noqa: BLE001
            print("ВНИМАНИЕ: не удалось прочитать RSS:", e, file=sys.stderr)
            if not os.environ.get("YOUTUBE_API_KEY"):
                sys.exit(1)
        if os.environ.get("YOUTUBE_API_KEY"):
            try:
                fresh += fetch_api_archive()
            except Exception as e:  # noqa: BLE001
                print("ВНИМАНИЕ: YouTube API не ответил:", e, file=sys.stderr)
    # API-данные (длиннее описание, есть длительность) перекрывают RSS
    seen = {}
    for it in fresh:
        seen[it["id"]] = {**seen.get(it["id"], {}), **it}
    data = merge(data, list(seen.values()))

    lessons = visible_lessons(data)
    head, tail = split_template()
    out = SITE / OUT_DIR
    changed = 0
    wanted = set()
    for x in lessons:
        others = [o for o in lessons if o["id"] != x["id"]][:RELATED]
        fn = out / f"{x['slug']}.html"
        wanted.add(fn.name)
        changed += write_if_changed(fn, build_page(x, head, tail, others))
    # убрать страницы скрытых/удалённых уроков
    if out.exists():
        for f in out.glob("*.html"):
            if f.name not in wanted:
                f.unlink()
                changed += 1

    changed += replace_between(SITE / "uroki.html", "<!-- LESSONS:START -->", "<!-- LESSONS:END -->",
                               build_uroki_block(lessons))
    sm = "\n" + "".join(
        f'  <url><loc>{SITE_URL}/{OUT_DIR}/{x["slug"]}.html</loc><lastmod>{date_ymd(x.get("published"))}</lastmod></url>\n'
        for x in lessons)
    changed += replace_between(SITE / "sitemap.xml", "<!-- LESSONS:START -->", "<!-- LESSONS:END -->", sm if lessons else "\n  ")
    blob = json.dumps(sorted(data.values(), key=lambda x: x.get("published", ""), reverse=True),
                      ensure_ascii=False, indent=1) + "\n"
    changed += write_if_changed(DATA_FILE, blob)
    print(f"Уроков на сайте: {len(lessons)}. Изменено файлов: {changed}.")


if __name__ == "__main__":
    main()
