#!/usr/bin/env python3
"""Один проход бота: забрать новые посты, прислать черновики, опубликовать одобренное.

Запускается по расписанию (GitHub Actions) или вручную.
Ничего не публикует без нажатия кнопки в личке.

Переменные окружения:
  BOT_TOKEN      — токен бота от @BotFather
  ADMIN_CHAT_ID  — твой личный chat_id (куда приходят черновики)
  CHANNEL_ID     — канал для публикации: @имя_канала или числовой id
  DRY_RUN=1      — ничего никуда не отправлять, только показать в логе
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests
import yaml

from editor import build_post, extract_promo
from reader import Post, fetch_posts

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
CONFIG_FILE = ROOT / "config.yml"

TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN = os.environ.get("ADMIN_CHAT_ID", "")
CHANNEL = os.environ.get("CHANNEL_ID", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

API = f"https://api.telegram.org/bot{TOKEN}"
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


# ───────────────────────── состояние ─────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("state.json повреждён, начинаем с чистого", file=sys.stderr)
    return {"last_post_id": 0, "update_offset": 0, "pending": {}, "published": []}


def save_state(state: dict) -> None:
    state["published"] = state.get("published", [])[-200:]
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ───────────────────────── telegram api ─────────────────────────

def tg(method: str, **params):
    if DRY_RUN:
        print(f"[DRY_RUN] {method}: "
              f"{json.dumps(params, ensure_ascii=False)[:400]}")
        return {"ok": True, "result": {"message_id": 0}}
    r = requests.post(f"{API}/{method}", json=params, timeout=40)
    data = r.json()
    if not data.get("ok"):
        print(f"Telegram вернул ошибку на {method}: {data}", file=sys.stderr)
    return data


def send_media(chat_id: str, text: str, photos: list[str], videos: list[str],
               reply_markup: dict | None = None, preview: bool = False) -> dict:
    """Отправляет пост с учётом лимитов Telegram на длину подписи.

    Разметка — HTML: так ссылка прячется под текстом шапки, а промокод
    в теге <code> копируется одним нажатием.
    """
    media = [("photo", u) for u in photos] + [("video", u) for u in videos]
    markup = {"reply_markup": reply_markup} if reply_markup else {}

    if not media:
        return tg("sendMessage", chat_id=chat_id, text=text[:TEXT_LIMIT],
                  parse_mode="HTML", disable_web_page_preview=not preview,
                  **markup)

    caption_fits = len(text) <= CAPTION_LIMIT

    if len(media) == 1:
        kind, url = media[0]
        method = "sendPhoto" if kind == "photo" else "sendVideo"
        params = {"chat_id": chat_id, kind: url, "parse_mode": "HTML"}
        if caption_fits:
            params["caption"] = text
            params.update(markup)
        res = tg(method, **params)
        if not caption_fits:
            res = tg("sendMessage", chat_id=chat_id, text=text[:TEXT_LIMIT],
                     parse_mode="HTML", disable_web_page_preview=not preview,
                     **markup)
        return res

    # несколько файлов — альбомом; у альбома не бывает кнопок, поэтому текст с кнопками уходит отдельным сообщением
    group = [{"type": k, "media": u} for k, u in media[:10]]
    if caption_fits and not reply_markup:
        group[0]["caption"] = text
        group[0]["parse_mode"] = "HTML"
        return tg("sendMediaGroup", chat_id=chat_id, media=group)
    tg("sendMediaGroup", chat_id=chat_id, media=group)
    return tg("sendMessage", chat_id=chat_id, text=text[:TEXT_LIMIT],
              parse_mode="HTML", disable_web_page_preview=not preview, **markup)


# ───────────────────────── нажатия кнопок ─────────────────────────

def process_callbacks(state: dict, cfg: dict) -> None:
    """Забирает нажатия, сделанные с прошлого запуска, и исполняет их."""
    if DRY_RUN:
        return
    resp = requests.get(
        f"{API}/getUpdates",
        params={"offset": state.get("update_offset", 0), "timeout": 0,
                "allowed_updates": json.dumps(["callback_query"])},
        timeout=40,
    ).json()
    if not resp.get("ok"):
        print(f"getUpdates не сработал: {resp}", file=sys.stderr)
        return

    for upd in resp.get("result", []):
        state["update_offset"] = upd["update_id"] + 1
        cq = upd.get("callback_query")
        if not cq:
            continue

        data = cq.get("data", "")
        action, _, post_id = data.partition(":")
        entry = state.get("pending", {}).get(post_id)
        msg = cq.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")

        if not entry:
            tg("answerCallbackQuery", callback_query_id=cq["id"],
               text="Этот черновик уже обработан")
            continue

        if action == "pub":
            send_media(CHANNEL, entry["text"], entry.get("photos", []),
                       entry.get("videos", []),
                       preview=bool(cfg.get("link_preview", False)))
            state.setdefault("published", []).append(int(post_id))
            note, alert = "✅ Опубликовано", "Пост ушёл в канал"
        elif action == "skip":
            note, alert = "🚫 Пропущено", "Пост пропущен"
        else:
            continue

        state["pending"].pop(post_id, None)
        tg("answerCallbackQuery", callback_query_id=cq["id"], text=alert)
        if chat_id and message_id:
            tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
               reply_markup={"inline_keyboard": [[{"text": note,
                                                   "callback_data": "done"}]]})
        time.sleep(0.4)


# ───────────────────────── новые посты ─────────────────────────

def draft_keyboard(post_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Опубликовать", "callback_data": f"pub:{post_id}"},
        {"text": "🚫 Пропустить", "callback_data": f"skip:{post_id}"},
    ]]}


def check_new_posts(state: dict, cfg: dict) -> int:
    posts: list[Post] = fetch_posts(cfg["source_channel"])
    posts = posts[-int(cfg.get("posts_per_check", 20)):]
    if not posts:
        print("Постов на странице не найдено — возможно, изменилась вёрстка")
        return 0

    last_seen = int(state.get("last_post_id", 0))

    if last_seen == 0:
        # первый запуск: не вываливаем в личку весь архив
        mode = cfg.get("first_run", "skip")
        state["last_post_id"] = posts[-1].id
        if mode == "last":
            posts = posts[-1:]
            state["last_post_id"] = posts[-1].id - 1
            last_seen = posts[-1].id - 1
        else:
            print(f"Первый запуск: запомнили пост #{posts[-1].id}, ничего не шлём")
            return 0

    fresh = [p for p in posts if p.id > last_seen]
    if not fresh:
        print("Новых постов нет")
        return 0

    keep_media = bool(cfg.get("keep_media", True))
    only_promo = bool(cfg.get("only_promo_posts", True))
    sent = 0
    for post in fresh:
        # посты без промокода (объявления, смена домена) пропускаем целиком
        if only_promo and not extract_promo(post.text, cfg):
            print(f"Пост #{post.id} без промокода — пропускаем")
            state["last_post_id"] = max(state["last_post_id"], post.id)
            continue

        text = build_post(post, cfg)
        if not text.strip():
            print(f"Пост #{post.id} после правок оказался пустым — пропускаем")
            state["last_post_id"] = max(state["last_post_id"], post.id)
            continue

        entry = {
            "text": text,
            "photos": post.photos if keep_media else [],
            "videos": post.videos if keep_media else [],
            "source": post.url,
        }
        draft = f"{text}\n\n— — —\n📝 черновик #{post.id} · оригинал: {post.url}"
        send_media(ADMIN, draft, entry["photos"], entry["videos"],
                   reply_markup=draft_keyboard(post.id),
                   preview=bool(cfg.get("link_preview", False)))

        state.setdefault("pending", {})[str(post.id)] = entry
        state["last_post_id"] = max(state["last_post_id"], post.id)
        sent += 1
        time.sleep(0.5)

    print(f"Отправлено черновиков: {sent}")
    return sent


# ───────────────────────── точка входа ─────────────────────────

def main() -> int:
    if not DRY_RUN and not all([TOKEN, ADMIN, CHANNEL]):
        print("Не заданы BOT_TOKEN / ADMIN_CHAT_ID / CHANNEL_ID", file=sys.stderr)
        return 1

    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    state = load_state()

    try:
        process_callbacks(state, cfg)  # сперва исполняем решения по старым черновикам
        check_new_posts(state, cfg)   # затем ищем новое
    finally:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
