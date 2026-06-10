"""Подстановка названия товара (OFFER) в текст письма."""

from __future__ import annotations


def apply_offer_to_text(text: str, offer_title: str) -> str:
    """OFFER / {{OFFER}} / \"OFFER\" → название объявления."""
    txt = text or ""
    title = (offer_title or "").strip()
    if not title:
        return txt
    for needle in ('{{OFFER}}', '"OFFER"', "'OFFER'", "«OFFER»", "OFFER"):
        txt = txt.replace(needle, title)
    return txt


def ensure_item_title_in_body(body: str, offer_title: str) -> str:
    """
    Если в теле нет названия товара (а в теме есть) — дописать строку.
    Иначе Gmail видит «массовый шаблон»: subject = товар, body = «uw artikel».
    """
    b = (body or "").strip()
    t = (offer_title or "").strip()
    if not b or not t or len(t) < 3:
        return body
    if t.lower() in b.lower():
        return body
    return f"{b}\n\nIk heb het over uw advertentie: {t}."


def finalize_mailing_body(body: str, offer_title: str) -> str:
    """
    OFFER из пресета → название товара; если в тексте его нет — дописать.
    (Раньше trim_trailing_offer_title срезал OFFER с конца — из-за этого в спам уходило
    «uw artikel» без названия при теме = товар.)
    """
    out = apply_offer_to_text(body or "", offer_title or "")
    return ensure_item_title_in_body(out, offer_title or "")
