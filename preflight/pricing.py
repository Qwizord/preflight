"""Цены Claude API и курс доллара.

Цены — с platform.claude.com/docs/en/about-claude/pricing (проверено 13.08.2026).
Курс тянется у ЦБ РФ и кэшируется на сутки; без сети берётся последний известный.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date, datetime

# $ за миллион токенов
MODELS = [
    {"id": "claude-opus-5",  "name": "Claude Opus 5",
     "in": 5.0, "out": 25.0, "cache_read": 0.50,
     "note": "самый сильный, для сложных сверок"},
    {"id": "claude-sonnet-5", "name": "Claude Sonnet 5",
     "in": 2.0, "out": 10.0, "cache_read": 0.20,
     "note": "рабочая лошадка, качество близко к Opus"},
    {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5",
     "in": 1.0, "out": 5.0, "cache_read": 0.10,
     "note": "самый дешёвый, для простых проверок"},
]

# Расход на одну проверку сделки. Оценка по реальным файлам Medali Store:
# свод правил и инструкции кэшируются, картинки и тексты идут заново.
PROFILE = {
    "cached_in": 8_000,     # свод правил + инструкции, читается из кэша
    "fresh_in": 17_000,     # 3 рендера (~4700 токенов каждый) + тексты файлов
    "out": 1_500,           # отчёт с находками
}

_rate_cache: dict = {"day": None, "value": 82.9977, "source": "последнее известное"}


def usd_rub() -> dict:
    """Курс ЦБ РФ. При недоступности — последнее значение."""
    today = date.today().isoformat()
    if _rate_cache["day"] == today:
        return _rate_cache
    try:
        with urllib.request.urlopen(
                "https://www.cbr-xml-daily.ru/daily_json.js", timeout=6) as r:
            d = json.load(r)
        _rate_cache.update(
            day=today, value=round(float(d["Valute"]["USD"]["Value"]), 4),
            source=f"ЦБ РФ, {datetime.fromisoformat(d['Date']).strftime('%d.%m.%Y')}")
    except Exception:
        _rate_cache["day"] = today       # не долбим сеть весь день
    return _rate_cache


def cost_per_check(model: dict, profile: dict = PROFILE) -> float:
    """Стоимость одной проверки в долларах."""
    return (
        profile["fresh_in"] * model["in"] / 1_000_000
        + profile["cached_in"] * model["cache_read"] / 1_000_000
        + profile["out"] * model["out"] / 1_000_000
    )


def table() -> list[dict]:
    out = []
    for m in MODELS:
        out.append(m | {"per_check": round(cost_per_check(m), 4)})
    return out
