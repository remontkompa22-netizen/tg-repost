"""Чтение публичного Telegram-канала через его веб-версию (t.me/s/<канал>).

Аккаунт, симка и Telethon не нужны: страница отдаётся всем.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"


@dataclass
class Post:
    id: int
    url: str
    text: str                      # текст поста, переносы строк сохранены
    links: list[str] = field(default_factory=list)   # все ссылки из поста
    photos: list[str] = field(default_factory=list)  # прямые URL картинок
    videos: list[str] = field(default_factory=list)  # прямые URL видео
    date: str = ""


def _node_to_text(node: Tag) -> tuple[str, list[str]]:
    """Превращает блок сообщения в текст, попутно собирая ссылки.

    <br> -> перенос строки, <a href=X>Y</a> -> Y (плюс X в список ссылок).
    """
    parts: list[str] = []
    links: list[str] = []

    def walk(el) -> None:
        if isinstance(el, NavigableString):
            parts.append(str(el))
            return
        if not isinstance(el, Tag):
            return
        if el.name == "br":
            parts.append("\n")
            return
        if el.name == "a":
            href = el.get("href", "")
            label = el.get_text()
            if href:
                links.append(href)
            # если подпись ссылки не совпадает с адресом — это скрытая ссылка,
            # сохраняем адрес рядом, чтобы его можно было заменить своим
            if href and label.strip() and href.rstrip("/") != label.strip().rstrip("/"):
                parts.append(f"{label} ({href})")
            else:
                parts.append(label or href)
            return
        if el.name in ("tg-emoji", "i", "b", "s", "u", "span", "code", "pre", "tg-spoiler"):
            for child in el.children:
                walk(child)
            return
        for child in el.children:
            walk(child)

    for child in node.children:
        walk(child)

    text = "".join(parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), links


def _bg_url(style: str) -> str | None:
    m = re.search(r"background-image:\s*url\(['\"]?(.*?)['\"]?\)", style or "")
    return m.group(1) if m else None


def fetch_posts(channel: str, timeout: int = 30) -> list[Post]:
    """Возвращает последние посты канала, от старых к новым."""
    url = f"https://t.me/s/{channel}"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    resp.raise_for_status()
    return parse_html(resp.text, channel)


def parse_html(html: str, channel: str) -> list[Post]:
    soup = BeautifulSoup(html, "html.parser")
    posts: list[Post] = []

    for wrap in soup.select("div.tgme_widget_message"):
        data_post = wrap.get("data-post", "")
        m = re.search(r"/(\d+)$", data_post)
        if not m:
            continue
        post_id = int(m.group(1))

        text, links = "", []
        text_node = wrap.select_one("div.tgme_widget_message_text")
        if text_node:
            text, links = _node_to_text(text_node)

        photos = []
        for a in wrap.select("a.tgme_widget_message_photo_wrap"):
            u = _bg_url(a.get("style", ""))
            if u:
                photos.append(u)
        # превью видео тоже лежит фоном — отдельный класс
        videos = []
        for v in wrap.select("video.tgme_widget_message_video"):
            src = v.get("src")
            if src:
                videos.append(src)

        date = ""
        t = wrap.select_one("time")
        if t and t.get("datetime"):
            date = t["datetime"]

        # служебные обёртки без текста и медиа пропускаем
        if not text and not photos and not videos:
            continue

        posts.append(Post(
            id=post_id,
            url=f"https://t.me/{channel}/{post_id}",
            text=text,
            links=links,
            photos=photos,
            videos=videos,
            date=date,
        ))

    # на странице возможны дубли (репосты) — оставляем по одному id
    unique: dict[int, Post] = {}
    for p in posts:
        unique[p.id] = p
    return [unique[k] for k in sorted(unique)]
