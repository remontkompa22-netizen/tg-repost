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

        if not entry and action not in ("manual", "resume", "stop"):
            tg("answerCallbackQuery", callback_query_id=cq["id"],
               text="Этот черновик уже обработан")
            continue

        if action == "resume":
            state["paused"] = False
            tg("answerCallbackQuery", callback_query_id=cq["id"],
               text="Снова слежу за каналом")
            if chat_id and message_id:
                tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                   reply_markup={"inline_keyboard": [[{"text": "▶️ Бот работает",
                                                       "callback_data": "done"}]]})
            continue

        if action == "stop":
            # этот пост не публикуем и бота ставим на паузу
            state["paused"] = True
            state.get("pending", {}).pop(post_id, None)
            tg("answerCallbackQuery", callback_query_id=cq["id"],
               text="Бот остановлен")
            if chat_id and message_id:
                tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                   reply_markup={"inline_keyboard": [[{"text": "⏸ Остановлен",
                                                       "callback_data": "done"}]]})
            tg("sendMessage", chat_id=ADMIN,
               text="⏸ Бот на паузе. Новые промокоды приходить не будут — "
                    "те, что выйдут за это время, пропущу. Нажми «Продолжить», "
                    "когда снова понадоблюсь.",
               reply_markup=resume_keyboard())
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

        if action.startswith("ch") and action[2:].isdigit():
            # публикация в один выбранный канал
            chans = target_channels(cfg)
            idx = int(action[2:])
            if idx >= len(chans):
                tg("answerCallbackQuery", callback_query_id=cq["id"],
                   text="Этого канала больше нет в списке")
                continue
            one = chans[idx]
            res = send_media(one, entry["text"], entry.get("photos", []),
                             entry.get("videos", []),
                             preview=bool(cfg.get("link_preview", False)))
            if not (res or {}).get("ok"):
                tg("answerCallbackQuery", callback_query_id=cq["id"],
                   text="Не удалось опубликовать, нажми ещё раз")
                tg("sendMessage", chat_id=ADMIN,
                   text=f"⚠️ Пост #{post_id} не ушёл в {one}. "
                        f"Проверь права бота в этом канале.")
                continue
            state.setdefault("published", []).append(post_id)
            state["pending"].pop(post_id, None)
            tg("answerCallbackQuery", callback_query_id=cq["id"],
               text=f"Опубликовано в {channel_label(one, cfg.get('channel_labels'))}")
            if chat_id and message_id:
                tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                   reply_markup={"inline_keyboard": [[{"text": f"✅ Ушло в {channel_label(one, cfg.get('channel_labels'))}",
                                                       "callback_data": "done"}]]})
            time.sleep(0.4)
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
            state.setdefault("published", []).append(post_id)
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


def publish_overdue(state: dict, cfg: dict) -> int:
    """Публикует черновики, которые провисели дольше auto_after_minutes.

    Смысл в том, чтобы промокод не сгорел, пока хозяин занят: кнопки остаются,
    но если их не нажали — пост уходит сам. Пауза («⏸ Стоп») сильнее таймера.
    """
    wait = cfg.get("auto_after_minutes")
    if not wait or state.get("paused"):
        return 0

    limit = float(wait) * 60
    now = time.time()
    done = 0

    for key, entry in list((state.get("pending") or {}).items()):
        created = entry.get("created")
        if not created:
            # черновик из версии без таймера — начинаем отсчёт с этого запуска,
            # чтобы старая очередь не улетела в каналы разом
            entry["created"] = now
            continue
        if now - created < limit:
            continue

        links, failed = publish_everywhere(
            entry["text"], entry.get("photos") or [], entry.get("videos") or [], cfg)
        if not links:
            print(f"Таймер: {key} опубликовать не удалось, повторим позже",
                  file=sys.stderr)
            tg("sendMessage", chat_id=ADMIN,
               text=f"⚠️ Черновик {key} не ушёл в канал по таймеру. "
                    f"Проверь права бота, попробую снова при следующей проверке.")
            continue
        if failed:
            tg("sendMessage", chat_id=ADMIN,
               text="⚠️ По таймеру опубликовано не везде. Не приняли: "
                    + ", ".join(failed))

        state["pending"].pop(key, None)
        state.setdefault("published", []).append(key)

        if entry.get("message_id"):
            tg("editMessageReplyMarkup", chat_id=ADMIN,
               message_id=entry["message_id"],
               reply_markup={"inline_keyboard": [[{"text": "⏱ Ушло по таймеру",
                                                   "callback_data": "done"}]]})
        tg("sendMessage", chat_id=ADMIN, disable_web_page_preview=True,
           text=f"⏱ {key} опубликован по таймеру"
                + ("\n" + "\n".join(links) if links else ""))
        done += 1
        time.sleep(0.5)

    if done:
        print(f"по таймеру опубликовано: {done}")
    return done


# ───────────────────────── новые посты ─────────────────────────

def sources(cfg: dict) -> list[dict]:
    """Проекты, за которыми следим.

    Каждый источник наследует общие настройки конфига и переопределяет свои:
    канал, ссылку, шапку, промокод новичков, правила распознавания кодов.
    """
    base = {k: v for k, v in cfg.items() if k != "sources"}
    out = []
    for item in cfg.get("sources") or []:
        merged = dict(base)
        merged.update(item)
        merged.setdefault("name", str(item.get("channel", "источник")))
        out.append(merged)
    if not out:                                   # старый конфиг с одним каналом
        solo = dict(base)
        solo.setdefault("name", str(cfg.get("source_channel", "источник")))
        solo["channel"] = cfg.get("source_channel")
        out.append(solo)
    return out


def source_state(state: dict, name: str) -> dict:
    """Память по конкретному источнику: какой пост был последним."""
    box = state.setdefault("sources", {})
    if name not in box:
        # переносим старое состояние, когда источник был один
        box[name] = {"last_post_id": int(state.get("last_post_id", 0))
                     if len(box) == 0 else 0}
    return box[name]


def channel_label(channel: str, labels: dict | None = None) -> str:
    """Как назвать канал на кнопке.

    Подпись берётся из конфига: ключ — @имя канала, либо main для основного,
    который задан секретом CHANNEL_ID числовым id и своего имени не имеет.
    """
    labels = {str(k): str(v) for k, v in (labels or {}).items()}
    if channel in labels:
        return labels[channel]
    if channel == CHANNEL and "main" in labels:
        return labels["main"]
    return channel if channel.startswith("@") else "основной"


def draft_keyboard(key: str, channels: list[str] | None = None,
                   labels: dict | None = None) -> dict:
    """Кнопки под черновиком.

    Верхний ряд — опубликовать сразу везде или пропустить. Средний появляется,
    когда каналов больше одного: каждая кнопка шлёт пост только в свой канал.
    """
    channels = channels or []
    rows = [[
        {"text": "✅ Опубликовать везде" if len(channels) > 1 else "✅ Опубликовать",
         "callback_data": f"pub:{key}"},
        {"text": "⏸ Стоп", "callback_data": f"stop:{key}"},
    ]]

    if len(channels) > 1:
        row = []
        for idx, ch in enumerate(channels):
            row.append({"text": f"📢 {channel_label(ch, labels)}",
                        "callback_data": f"ch{idx}:{key}"})
            if len(row) == 2:                      # по две кнопки в ряд
                rows.append(row)
                row = []
        if row:
            rows.append(row)

    rows.append([{"text": "⚡ Дальше автоматом", "callback_data": f"auto:{key}"}])
    return {"inline_keyboard": rows}


def resume_keyboard() -> dict:
    """Кнопка, которой бота будят после паузы."""
    return {"inline_keyboard": [[
        {"text": "▶️ Продолжить", "callback_data": "resume:0"},
    ]]}


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


def check_one_source(state: dict, cfg: dict, src_cfg: dict) -> int:
    """Проверяет один канал-источник и обрабатывает его новые посты."""
    name = src_cfg["name"]
    channel = src_cfg["channel"]
    mem = source_state(state, name)

    posts: list[Post] = fetch_posts(channel)
    posts = posts[-int(src_cfg.get("posts_per_check", 20)):]
    if not posts:
        print(f"[{name}] постов не найдено — возможно, изменилась вёрстка")
        return 0

    last_seen = int(mem.get("last_post_id", 0))

    if last_seen == 0:
        mem["last_post_id"] = posts[-1].id
        if src_cfg.get("first_run", "skip") == "last":
            posts = posts[-1:]
            mem["last_post_id"] = last_seen = posts[-1].id - 1
        else:
            print(f"[{name}] первый запуск: запомнили пост #{posts[-1].id}")
            return 0

    fresh = [p for p in posts if p.id > last_seen]
    if not fresh:
        print(f"[{name}] новых постов нет")
        return 0

    if state.get("paused"):
        mem["last_post_id"] = max(last_seen, max(p.id for p in fresh))
        print(f"[{name}] бот на паузе, пропущено постов: {len(fresh)}")
        return 0

    keep_media = bool(src_cfg.get("keep_media", True))
    only_promo = bool(src_cfg.get("only_promo_posts", True))
    auto_mode = bool(state.get("auto", cfg.get("auto_publish", False)))
    sent = 0

    for post in fresh:
        if only_promo and not extract_promo(post.text, src_cfg):
            print(f"[{name}] пост #{post.id} без промокода — пропускаем")
            mem["last_post_id"] = max(mem["last_post_id"], post.id)
            continue

        text = build_post(post, src_cfg)
        if not text.strip():
            print(f"[{name}] пост #{post.id} после правок пуст — пропускаем")
            mem["last_post_id"] = max(mem["last_post_id"], post.id)
            continue

        key = f"{name}:{post.id}"                  # ключ уникален между источниками
        entry = {
            "text": text,
            "photos": post.photos if keep_media else [],
            "videos": post.videos if keep_media else [],
            "source": post.url,
            "from": name,
        }

        if auto_mode:
            links, failed = publish_everywhere(
                text, entry["photos"], entry["videos"], cfg)
            if failed and links:
                tg("sendMessage", chat_id=ADMIN,
                   text="⚠️ Опубликовано не везде. Не приняли: " + ", ".join(failed))
            if not links:
                print(f"[{name}] автопубликация #{post.id} не удалась, повторим позже",
                      file=sys.stderr)
                tg("sendMessage", chat_id=ADMIN,
                   text=f"⚠️ Не удалось опубликовать пост #{post.id} ({name}) "
                        f"автоматически. Попробую ещё раз при следующей проверке.")
                continue
            promo = extract_promo(post.text, src_cfg) or {}
            tg("sendMessage", chat_id=ADMIN, parse_mode="HTML",
               disable_web_page_preview=True,
               text=f"⚡ Опубликовано автоматически ({name}): "
                    f"<code>{promo.get('promo', '')}</code>"
                    + ("\n" + "\n".join(links) if links else ""),
               reply_markup=auto_off_keyboard())
            state.setdefault("published", []).append(post.id)
            mem["last_post_id"] = max(mem["last_post_id"], post.id)
            sent += 1
            time.sleep(0.5)
            continue

        wait = cfg.get("auto_after_minutes")
        timer_note = (f"\n⏱ не нажмёшь — опубликую сам через {int(wait)} мин"
                      if wait else "")
        draft = (f"{text}\n\n— — —\n"
                 f"📝 черновик #{post.id} · {name} · оригинал: {post.url}"
                 f"{timer_note}")
        res = send_media(ADMIN, draft, entry["photos"], entry["videos"],
                         reply_markup=draft_keyboard(key, target_channels(cfg),
                                                     cfg.get("channel_labels")),
                         preview=bool(cfg.get("link_preview", False)))
        if not (res or {}).get("ok"):
            print(f"[{name}] черновик #{post.id} не отправлен, повторим позже",
                  file=sys.stderr)
            continue

        entry["created"] = time.time()             # отсчёт для таймера автопубликации
        mid = ((res or {}).get("result") or {})
        if isinstance(mid, dict) and mid.get("message_id"):
            entry["message_id"] = mid["message_id"]

        state.setdefault("pending", {})[key] = entry
        mem["last_post_id"] = max(mem["last_post_id"], post.id)
        sent += 1
        time.sleep(0.5)

    if sent:
        print(f"[{name}] {'опубликовано' if auto_mode else 'черновиков'}: {sent}")
    return sent


def check_new_posts(state: dict, cfg: dict) -> int:
    """Обходит все источники по очереди."""
    total = 0
    for src_cfg in sources(cfg):
        if not src_cfg.get("channel"):
            continue
        try:
            total += check_one_source(state, cfg, src_cfg)
        except Exception as exc:                   # один источник не должен ронять остальные
            print(f"[{src_cfg.get('name')}] ошибка: {exc}", file=sys.stderr)
    return total


# ───────────────────────── точка входа ─────────────────────────

def main() -> int:
    if not DRY_RUN and not all([TOKEN, ADMIN, CHANNEL]):
        print("Не заданы BOT_TOKEN / ADMIN_CHAT_ID / CHANNEL_ID", file=sys.stderr)
        return 1

    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    state = load_state()

    try:
        process_callbacks(state, cfg)  # сперва исполняем решения по старым черновикам
        publish_overdue(state, cfg)    # затем то, что провисело дольше таймера
        check_new_posts(state, cfg)    # и только потом ищем новое
    finally:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
