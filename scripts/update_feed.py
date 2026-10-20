#!/usr/bin/env python3
"""Собирает ленту ВК в feed.json + img/.

Токен ВК НЕ нужен: посты берутся из открытого виджета сообщества.
Запускается в GitHub Actions раз в сутки, также можно запустить вручную:

    python scripts/update_feed.py
"""

import html
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from PIL import Image

GROUP = os.environ.get("VK_GROUP", "65345262").strip()
MAX_PHOTOS = int(os.environ.get("MAX_PHOTOS", "4"))
CARD_W = int(os.environ.get("CARD_W", "760"))
GAL_W = int(os.environ.get("GAL_W", "1000"))
QUALITY = int(os.environ.get("QUALITY", "82"))

WIDGET = (
    "https://vk.com/widget_community.php"
    "?app=0&width=700&_ver=1&gid={gid}&mode=4&height=3000"
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG_DIR = os.path.join(ROOT, "img")
FEED_FILE = os.path.join(ROOT, "feed.json")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

MSK = timezone(timedelta(hours=3))

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
    "декабря": 12,
}


def log(*a):
    print(*a, flush=True)


def clean_text(raw):
    """HTML -> читаемый текст."""
    if not raw:
        return ""
    t = raw.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</p\s*>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = t.replace("\u00a0", " ").replace("\ufffd", " ")
    # служебный маркер свёрнутого текста — ВК вставляет его в любое место
    t = re.sub(r"\s*(Show more|Показать полностью|Показать целиком)\s*", " ", t, flags=re.I)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def parse_date(label, now):
    """'Yesterday at 5:56' / '18 October 2026 at 3:15' -> datetime (МСК)."""
    s = (label or "").replace("\u00a0", " ").replace("\ufffd", " ").strip()
    s = re.sub(r"\s+", " ", s)

    base = None
    low = s.lower()
    if low.startswith("today") or low.startswith("сегодня"):
        base = now
    elif low.startswith("yesterday") or low.startswith("вчера"):
        base = now - timedelta(days=1)
    else:
        m = re.match(r"(\d{1,2})\s+([A-Za-zА-Яа-я]+)\s+(\d{4})", s)
        if m:
            day, mon, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
            if mon in MONTHS:
                base = datetime(year, MONTHS[mon], day, 12, 0, tzinfo=MSK)
    if base is None:
        base = now

    tm = re.search(r"(\d{1,2}):(\d{2})", s)
    if tm:
        base = base.replace(hour=int(tm.group(1)), minute=int(tm.group(2)))
    return base


def fetch_widget():
    url = WIDGET.format(gid=GROUP)
    r = requests.get(url, headers={"User-Agent": UA}, timeout=40)
    r.raise_for_status()
    raw = r.content
    # виджет отдаёт windows-1251; ошибки декодирования не должны ломать разбор
    return raw.decode("cp1251", errors="replace")


def parse(page):
    now = datetime.now(MSK)
    # маркер даты берём как якорь: внутри блока он ровно один
    parts = re.split(r'(?=<a class="wcommunity_post_date")', page)

    posts = []
    seen = set()
    for chunk in parts[1:]:
        mid = re.search(r'href="/wall-(\d+)_(\d+)"', chunk)
        if not mid:
            continue
        pid = int(mid.group(2))
        if pid in seen:
            continue
        seen.add(pid)

        dlabel = re.search(r'class="wcommunity_post_date"[^>]*>([^<]*)<', chunk)
        dt = parse_date(dlabel.group(1) if dlabel else "", now)

        txt = re.search(r'class="wall_post_text"[^>]*>(.*?)</div>', chunk, re.S)
        text = clean_text(txt.group(1)) if txt else ""

        imgs = []
        for u in re.findall(r"background-image:\s*url\('?([^')]+)'?\)", chunk):
            u = html.unescape(u).strip().rstrip(")")
            if u and u not in imgs and ("sun9" in u or "userapi" in u or "okcdn" in u):
                imgs.append(u)
        imgs = imgs[:MAX_PHOTOS]

        if not text and not imgs:
            continue

        posts.append({"id": pid, "dt": dt, "text": text, "photos": imgs})

    # Время в виджете 12-часовое без AM/PM (17:56 выглядит как 5:56), поэтому
    # по часам сортировать нельзя. У постов ВК id растут строго по времени —
    # это и есть надёжная хронология.
    posts.sort(key=lambda p: p["id"], reverse=True)
    return posts


def download(url, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=45)
            if r.status_code == 200 and len(r.content) > 500:
                return r.content
        except requests.RequestException:
            pass
        time.sleep(2 + i * 2)
    return None


def save_image(raw, path, max_w):
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return False
    if im.width > max_w:
        im = im.resize((max_w, max(1, round(im.height * max_w / im.width))), Image.LANCZOS)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path, "JPEG", quality=QUALITY, optimize=True, progressive=True)
    return True


def build(posts):
    os.makedirs(IMG_DIR, exist_ok=True)
    used, entries = set(), []

    for post in posts:  # уже отсортированы по id — это и есть хронология ВК
        pid, imgs = post["id"], []
        for n, url in enumerate(post["photos"]):
            name = f"{pid}_{n}.jpg"
            path = os.path.join(IMG_DIR, name)
            if not os.path.exists(path):
                raw = download(url)
                if raw is None or not save_image(raw, path, CARD_W if n == 0 else GAL_W):
                    continue
                log(f"  + {name} ({os.path.getsize(path) // 1024} KB)")
                time.sleep(0.3)
            imgs.append(f"img/{name}")
            used.add(name)

        entries.append({
            "id": pid,
            "date": post["dt"].strftime("%Y-%m-%d"),
            "ts": int(post["dt"].timestamp()),
            "text": post["text"],
            "img": imgs,
            "url": f"https://vk.com/wall-{GROUP}_{pid}",
        })

    removed = 0
    for f in os.listdir(IMG_DIR):
        if f.endswith(".jpg") and f not in used:
            os.remove(os.path.join(IMG_DIR, f))
            removed += 1

    feed = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "group": GROUP,
        "count": len(entries),
        "items": entries,
    }
    with open(FEED_FILE, "w", encoding="utf-8") as fh:
        json.dump(feed, fh, ensure_ascii=False, indent=1)

    log(f"\nготово: {len(entries)} постов, {len(used)} фото, "
        f"удалено старых {removed}, feed.json {os.path.getsize(FEED_FILE) // 1024} KB")


def main():
    log(f"читаю виджет сообщества {GROUP}…")
    page = fetch_widget()
    posts = parse(page)
    log(f"постов найдено: {len(posts)}")
    if not posts:
        raise SystemExit(
            "Не удалось разобрать ни одного поста.\n"
            "Скорее всего ВК изменил разметку виджета или сообщество закрыто."
        )
    build(posts)


if __name__ == "__main__":
    main()
