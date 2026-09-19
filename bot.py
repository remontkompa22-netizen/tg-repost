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


def tg_upload(method: str, chat_id: str, kind: str, url: str, caption: str | None,
              reply_markup: dict | None) -> dict:
    """Скачивает медиа сами и отправляет файлом.

    Телеграм умеет забирать картинку по ссылке, но со своего же CDN (telesco.pe)
    у него это не получается — приходится качать и загружать вручную.
    """
    if DRY_RUN:
        print(f"[DRY_RUN] upload {method}: {url[:60]}")
        return {"ok": True, "result": {}}
    try:
        blob = requests.get(url, timeout=60)
        blob.raise_for_status()
    except Exception as exc:                       # сеть, 404, таймаут
        print(f"Не удалось скачать медиа {url[:60]}: {exc}", file=sys.stderr)
        return {"ok": False}
    ext = "mp4" if kind == "video" else "jpg"
    data = {"chat_id": chat_id, "parse_mode": "HTML"}
    if caption:
        data["caption"] = caption
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    resp = requests.post(f"{API}/{method}", data=data,
                         files={kind: (f"media.{ext}", blob.content)}, timeout=120)
    out = resp.json()
    if not out.get("ok"):
        print(f"Загрузка файлом не удалась: {out}", file=sys.stderr)
    return out


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
        if not res.get("ok"):
            # телеграм не смог забрать файл по ссылке — качаем и грузим сами
            res = tg_upload(method, chat_id, kind, url,
                            text if caption_fits else None,
                            reply_markup if caption_fits else None)
        if not res.get("ok"):
            # медиа так и не ушло — отправляем хотя бы текст
            print("Медиа отправить не вышло, шлём текстом", file=sys.stderr)
            return tg("sendMessage", chat_id=chat_id, text=text[:TEXT_LIMIT],
                      parse_mode="HTML", disable_web_page_preview=not preview,
                      **markup)
        if not caption_fits:
            res = tg("sendMessage", chat_id=chat_id, text=text[:TEXT_LIMIT],
                     parse_mode="HTML", disable_web_page_preview=not preview,
                     **markup)
        return res

    # несколько файлов — альбомом; у альбома не бывает кнопок,
    # поэтому текст с кнопками уходит отдельным сообщением
    group = [{"type": k, "media": u} for k, u in media[:10]]
    if caption_fits and not reply_markup:
        group[0]["caption"] = text
        group[0]["parse_mode"] = "HTML"
        res = tg("sendMediaGroup", chat_id=chat_id, media=group)
        if res.get("ok"):
            return res
        return tg("sendMessage", chat_id=chat_id, text=text[:TEXT_LIMIT],
                  parse_mode="HTML", disable_web_page_preview=not preview, **markup)
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

        if not entry and action != "manual":
            tg("answerCallbackQuery", callback_query_id=cq["id"],
               text="Этот черновик уже обработан")
            continue

        if action == "manual":
            # выключаем автопубликацию из уведомления
            state["auto"] = False
            tg("answerCallbackQuery", callback_query_id=cq["id"],
               text="Снова буду спрашивать перед публикацией")
            if chat_id and message_id:
                tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                   reply_markup={"inline_keyboard": [[{"text": "⏸ Подтверждение включено",
                                                       "callback_data": "done"}]]})
            continue

        if action == "auto":
            state["auto"] = True

        if action in ("pub", "auto"):
            links, failed = publish_everywhere(
                entry["text"], entry.get("photos", []), entry.get("videos", []), cfg)
            if failed and links:
                # часть каналов приняла пост, часть нет — сообщаем, но не повторяем,
                # иначе в удачные каналы улетит дубль
                tg("sendMessage", chat_id=ADMIN,
                   text="⚠️ Опубликовано не везде. Не приняли: "
                        + ", ".join(failed)
                        + ". Проверь, что бот — админ там с правом публикации.")
            if not links:
                # в канал не ушло — черновик остаётся в очереди,
                # предупреждаем в личку, потому что всплывашка могла устареть
                print(f"Пост #{post_id} опубликовать не удалось", file=sys.stderr)
                tg("answerCallbackQuery", callback_query_id=cq["id"],
                   text="Не удалось опубликовать, нажми ещё раз")
                tg("sendMessage", chat_id=ADMIN,
                   text=f"⚠️ Пост #{post_id} не ушёл в канал. "
                        f"Проверь, что бот — админ канала с правом публикации, "
                        f"и нажми «Опубликовать» ещё раз.")
                continue
            state.setdefault("published", []).append(int(post_id))
            if action == "auto":
                note = "⚡ Опубликовано, дальше автоматом"
                alert = "Готово. Следующие промокоды уйдут сами"
            else:
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
    return {"inline_keyboard": [
        [
            {"text": "✅ Опубликовать", "callback_data": f"pub:{post_id}"},
            {"text": "🚫 Пропустить", "callback_data": f"skip:{post_id}"},
        ],
        [{"text": "⚡ Дальше автоматом", "callback_data": f"auto:{post_id}"}],
    ]}


def auto_off_keyboard() -> dict:
    """Кнопка возврата к ручному подтверждению.

    Без неё выключить автопубликацию было бы негде: черновики перестают
    приходить, а вместе с ними исчезают и кнопки.
    """
    return {"inline_keyboard": [[
        {"text": "⏸ Вернуть подтверждение", "callback_data": "manual:0"},
    ]]}


def channel_post_link(res: dict, channel: str) -> str:
    """Ссылка на только что опубликованный пост, если канал публичный."""
    mid = ((res or {}).get("result") or {}).get("message_id")
    if mid and channel.startswith("@"):
        return f"https://t.me/{channel.lstrip('@')}/{mid}"
    return ""


def target_channels(cfg: dict) -> list[str]:
    """Все каналы для публикации: основной из секрета плюс список из конфига."""
    out, seen = [], set()
    for ch in [CHANNEL, *(cfg.get("extra_channels") or [])]:
        ch = str(ch or "").strip()
        if ch and ch not in seen:
            seen.add(ch)
            out.append(ch)
    return out


def publish_everywhere(text: str, photos: list[str], videos: list[str],
                       cfg: dict) -> tuple[list[str], list[str]]:
    """Рассылает пост по всем каналам.

    Возвращает ссылки на удачные публикации и список каналов, куда не ушло.
    """
    links, failed = [], []
    for ch in target_channels(cfg):
        res = send_media(ch, text, photos, videos,
                         preview=bool(cfg.get("link_preview", False)))
        if (res or {}).get("ok"):
            link = channel_post_link(res, ch)
            links.append(link or ch)
        else:
            print(f"В канал {ch} опубликовать не удалось", file=sys.stderr)
            failed.append(ch)
        time.sleep(0.3)
    return links, failed


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
    auto_mode = bool(state.get("auto", cfg.get("auto_publish", False)))
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

        if auto_mode:
            # режим «дальше автоматом»: публикуем сразу, а в личку — короткий отчёт
            links, failed = publish_everywhere(
                text, entry["photos"], entry["videos"], cfg)
            if failed and links:
                tg("sendMessage", chat_id=ADMIN,
                   text="⚠️ Опубликовано не везде. Не приняли: " + ", ".join(failed))
            if not links:
                print(f"Автопубликация поста #{post.id} не удалась, повторим позже",
                      file=sys.stderr)
                tg("sendMessage", chat_id=ADMIN,
                   text=f"⚠️ Не удалось опубликовать пост #{post.id} автоматически. "
                        f"Попробую ещё раз при следующей проверке.")
                continue
            promo = extract_promo(post.text, cfg) or {}
            tg("sendMessage", chat_id=ADMIN, parse_mode="HTML",
               disable_web_page_preview=True,
               text=f"⚡ Опубликовано автоматически: <code>{promo.get('promo', '')}</code>"
                    + ("\n" + "\n".join(links) if links else ""),
               reply_markup=auto_off_keyboard())
            state.setdefault("published", []).append(post.id)
            state["last_post_id"] = max(state["last_post_id"], post.id)
            sent += 1
            time.sleep(0.5)
            continue

        draft = f"{text}\n\n— — —\n📝 черновик #{post.id} · оригинал: {post.url}"
        res = send_media(ADMIN, draft, entry["photos"], entry["videos"],
                         reply_markup=draft_keyboard(post.id),
                         preview=bool(cfg.get("link_preview", False)))

        if not (res or {}).get("ok"):
            # черновик не дошёл — не помечаем пост обработанным,
            # попробуем ещё раз при следующем запуске
            print(f"Черновик #{post.id} не отправлен, повторим позже", file=sys.stderr)
            continue

        state.setdefault("pending", {})[str(post.id)] = entry
        state["last_post_id"] = max(state["last_post_id"], post.id)
        sent += 1
        time.sleep(0.5)

    print(f"{'Опубликовано автоматически' if auto_mode else 'Отправлено черновиков'}: {sent}")
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
