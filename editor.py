"""Правки поста перед публикацией в своём канале.

Работает без нейросетей и без API-ключей: подмена ссылок, вычистка чужих
упоминаний, сборка поста заново по собственному шаблону и добавление
собственного блока с промокодом для новичков.

Пост собирается в HTML-разметке Telegram, поэтому ссылку можно спрятать
под текстом, а промокод сделать копируемым по нажатию.
"""
from __future__ import annotations

import html as html_lib
import re

from reader import Post


def esc(text: str) -> str:
    """Экранирует то, что пришло из чужого канала, чтобы не сломать разметку."""
    return html_lib.escape(text, quote=False)


def _strip_stop_lines(text: str, patterns: list[str], keep_link: str = "") -> str:
    """Выбрасывает строки со стоп-словами.

    Строку с твоей собственной ссылкой не трогаем никогда: иначе, если твой
    партнёрский адрес на том же домене, что и чужой, он улетит вместе с ним.
    """
    if not patterns:
        return text
    out = []
    for line in text.split("\n"):
        if keep_link and keep_link in line:
            out.append(line)
            continue
        if any(re.search(p, line, re.IGNORECASE) for p in patterns):
            continue
        out.append(line)
    return "\n".join(out)


def _replace_links(text: str, replacements: dict[str, str], default_link: str | None) -> str:
    """Меняет чужие ссылки на твою.

    Работает по целым адресам, а не по кускам строки: иначе, если твоя ссылка
    живёт на том же домене, что и чужая, к ней приклеился бы второй хвост.
    """
    reps = {k.rstrip("/"): v for k, v in (replacements or {}).items()}

    def sub_url(m: re.Match) -> str:
        url = m.group(0)
        if default_link and url.startswith(default_link):
            return url                     # это уже твоя ссылка
        if "t.me/" in url:
            return url                     # телеграм-ссылки чистятся отдельно
        for srcu, dst in reps.items():
            if url.rstrip("/").startswith(srcu):
                return dst                 # точечная замена из конфига
        return default_link or url

    return re.sub(r"https?://[^\s\)\]]+", sub_url, text)


def _strip_mentions(text: str, keep: list[str]) -> str:
    keep_lower = {k.lower().lstrip("@") for k in (keep or [])}

    def sub(m: re.Match) -> str:
        name = m.group(1)
        return m.group(0) if name.lower() in keep_lower else ""

    text = re.sub(r"@([A-Za-z0-9_]{4,})", sub, text)
    # ссылки-приглашения на чужие каналы вида (https://t.me/xxx)
    text = re.sub(r"\(https?://t\.me/[^)]+\)", "", text)
    text = re.sub(r"https?://t\.me/\S+", "", text)
    return text


def _apply_word_replacements(text: str, pairs: dict[str, str]) -> str:
    for src, dst in (pairs or {}).items():
        text = re.sub(re.escape(src), dst, text, flags=re.IGNORECASE)
    return text


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    # выбрасываем строки, от которых после вычисток остался один мусор,
    # но пустые строки между абзацами сохраняем
    lines = []
    for line in text.split("\n"):
        bare = line.strip()
        if bare and not re.search(r"[0-9A-Za-zА-Яа-яЁё]", bare):
            # строка без единой буквы и цифры: либо осколок вроде «⚠️» от пустой
            # подстановки, либо разделитель ━━━ — разделитель оставляем
            if len(set(bare)) > 1 or len(bare) < 4:
                continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_promo(text: str, cfg: dict) -> dict | None:
    """Вытаскивает из поста данные промокода. None, если это не промо-пост.

    Понимает оба формата: диапазон («от 25 до 1000 ₽», «25 - 1000 RUB»)
    и один номинал («Промокод — SBER665 50 рублей»).
    """
    code_re = cfg.get("promo_code_regex") or r"\b([A-Z]{8,14})\b"
    m = re.search(code_re, text)
    if not m:
        return None
    code = m.group(1) if m.groups() else m.group(0)

    unit = cfg.get("bonus_unit", "₽")
    data = {"promo": code, "amount": ""}

    # диапазон: «от 25 до 1000 ₽» или «25 - 1000 RUB»
    span = re.search(
        r"(?:от\s*)?(\d[\d\s]*?)\s*(?:до|[-–—])\s*(\d[\d\s]*?)\s*(?:RUB|rub|руб\w*|₽|р\.)",
        text, re.IGNORECASE)
    if not span:
        span = re.search(r"от\s*(\d[\d\s]*?)\s*(?:до|[-–—])\s*(\d[\d\s]*)", text, re.IGNORECASE)
    if span:
        data["min"] = span.group(1).strip()
        data["max"] = span.group(2).strip()
        data["amount"] = f"{data['min']} – {data['max']} {unit}"
    else:
        # один номинал: число рядом с кодом или со словом «рублей»
        one = re.search(re.escape(code) + r"\D{0,20}?(\d[\d\s]*?)\s*(?:RUB|rub|руб\w*|₽|р\.)",
                        text, re.IGNORECASE)
        if not one:
            one = re.search(r"(\d[\d\s]*?)\s*(?:RUB|rub|руб\w*|₽)", text, re.IGNORECASE)
        if one:
            data["min"] = data["max"] = one.group(1).strip()
            data["amount"] = f"{one.group(1).strip()} {unit}"

    # «Активаций: 2250» и «1500 активаций» — число бывает с обеих сторон
    acts = re.search(r"активаци\w*[:\s]*(\d[\d\s]*)", text, re.IGNORECASE)
    if not acts:
        acts = re.search(r"(\d[\d\s]*?)\s*активаци", text, re.IGNORECASE)
    if acts:
        data["count"] = acts.group(1).strip()

    # условие вроде «для тех, кто пополнял за 7 дней» — без него код не сработает
    note = ""
    for pat in cfg.get("condition_patterns") or []:
        found = next((l.strip() for l in text.split("\n")
                      if re.search(pat, l, re.IGNORECASE)), "")
        if found:
            note = found
            break
    data["note"] = note
    return data


def _header(cfg: dict, my_link: str) -> str:
    """Строка-шапка со скрытой под текстом ссылкой."""
    text = (cfg.get("header_text") or "").strip()
    if not text:
        return ""
    link = (cfg.get("header_link") or my_link or "").strip()
    if not link:
        return text
    return f'<a href="{html_lib.escape(link, quote=True)}">{text}</a>'


def _newbie_block(cfg: dict, my_link: str) -> str:
    """Собственный блок с промокодом для новых игроков."""
    tmpl = (cfg.get("newbie_block") or "").strip()
    if not tmpl:
        return ""
    return tmpl.format(
        promo=cfg.get("newbie_promo", ""),
        bonus=cfg.get("newbie_bonus", ""),
        link=my_link,
    ).strip()


def build_post(post: Post, cfg: dict) -> str:
    """Возвращает готовый текст для публикации (HTML-разметка Telegram)."""
    my_link = (cfg.get("my_link") or "").strip()

    promo = extract_promo(post.text, cfg) if cfg.get("use_promo_template", True) else None

    if promo and cfg.get("promo_template"):
        body = cfg["promo_template"].format(
            promo=esc(promo.get("promo", "")),
            min=esc(promo.get("min", "—")),
            max=esc(promo.get("max", "—")),
            amount=esc(promo.get("amount", "")),
            note=esc(promo.get("note", "")),
            count=esc(promo.get("count", "—")),
            link=my_link,
        )
    else:
        body = esc(post.text)
        body = _replace_links(body, cfg.get("link_replacements", {}), my_link or None)
        body = _strip_stop_lines(body, cfg.get("drop_lines_matching", []), my_link)
        body = _strip_mentions(body, cfg.get("keep_mentions", []))
        body = _apply_word_replacements(body, cfg.get("word_replacements", {}))

    body = _tidy(body)

    parts = [
        _header(cfg, my_link),
        body,
        _newbie_block(cfg, my_link),
        (cfg.get("signature") or "").strip(),
    ]
    return "\n\n".join(p for p in parts if p and p.strip()).strip()
