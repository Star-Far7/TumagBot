"""
Инлайн-поиск товаров: @botname <запрос> в любом чате.

Запрос может быть:
  • названием товара или его частью (≥ 2 символа)
  • штрихкодом (4-20 цифр) — точный поиск по barcode/extra_code
"""
import hashlib
import logging
import re

from aiogram import Router
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)

from db.database import Database
from services.product_matcher import ProductMatcher

logger = logging.getLogger(__name__)
router = Router()

_BARCODE_RE = re.compile(r"^\d{4,20}$")


def _looks_like_barcode(text: str) -> bool:
    cleaned = text.replace(" ", "").replace("-", "")
    return bool(_BARCODE_RE.match(cleaned))


@router.inline_query()
async def inline_search(query: InlineQuery, db: Database):
    search_text = query.query.strip()

    if len(search_text) < 2:
        await query.answer(
            [],
            is_personal=True,
            cache_time=1,
            switch_pm_text="Введите название или штрихкод",
            switch_pm_parameter="inline_hint",
        )
        return

    products = await db.get_all_products()
    matcher  = ProductMatcher(products)

    # Поиск по штрихкоду, если запрос — только цифры
    if _looks_like_barcode(search_text):
        barcode = search_text.replace(" ", "").replace("-", "")
        _, matches = matcher.find_matches(
            query="", barcode=barcode, top_n=10, suggest_threshold=0.12,
        )
    else:
        _, matches = matcher.find_matches(
            search_text, top_n=10, suggest_threshold=0.12,
        )

    results = []
    for m in matches:
        prod      = m["product"]
        score_pct = int(m["score"] * 100)

        parts = []
        if prod.get("category"):
            parts.append(prod["category"])
        if prod.get("barcode"):
            parts.append(f"ШК: {prod['barcode']}")
        if prod.get("purchase_price"):
            parts.append(f"Цена: {prod['purchase_price']:.2f}")
        description = " | ".join(parts) or "нет данных"

        uid = hashlib.md5(f"{prod['id']}:{search_text}".encode()).hexdigest()

        text_lines = [f"<b>{prod['name']}</b>"]
        if prod.get("category"):
            text_lines.append(f"Категория: {prod['category']}")
        if prod.get("barcode"):
            text_lines.append(f"Штрихкод: {prod['barcode']}")
        text_lines.append(f"Ед.изм.: {prod.get('unit') or 'шт'}")
        if prod.get("purchase_price"):
            text_lines.append(f"Цена закупа: {prod['purchase_price']:.2f}")
        # Скрытый маркер для автоопределения товара при ручном поиске
        text_lines.append(f"<code>🔖{prod['id']}</code>")

        results.append(
            InlineQueryResultArticle(
                id=uid,
                title=f"[{score_pct}%] {prod['name'][:60]}",
                description=description,
                input_message_content=InputTextMessageContent(
                    message_text="\n".join(text_lines),
                    parse_mode="HTML",
                ),
            )
        )

    await query.answer(results, is_personal=True, cache_time=10)
