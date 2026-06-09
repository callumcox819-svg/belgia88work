"""Региональные настройки бота (Бельгия / 2dehands.be)."""

from __future__ import annotations

import os

BOT_DISPLAY_NAME = "Belgium Bot"
MARKETPLACE_NAME = "2dehands.be"
TEAM_NAME = "Narkologia"

# Коды service в GOO Network API: 2dehands_be, bpost_be
AQUA_DEFAULT_SERVICE = (os.getenv("AQUA_DEFAULT_SERVICE", "2dehands_be") or "2dehands_be").strip()

HTML_DATA_DIR = "HTMLbe"

DEFAULT_VALIDATION_DOMAINS: tuple[str, ...] = (
    "gmail.com",
    "hotmail.com",
    "outlook.com",
    "yahoo.com",
    "icloud.com",
    "live.be",
    "skynet.be",
    "telenet.be",
    "me.com",
)


def format_item_price(price: str) -> str:
    """Цена в письмах/HTML: EUR, если валюта не указана."""
    p = (price or "").strip()
    if not p:
        return ""
    upper = p.upper()
    if upper.startswith("EUR") or "€" in p:
        return p
    if any(upper.startswith(c) for c in ("CHF", "USD", "GBP", "NOK", "SEK", "DKK")):
        return p
    return f"{p} €"
