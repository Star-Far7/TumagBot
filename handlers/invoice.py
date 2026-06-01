"""
Основной FSM-поток обработки накладных.

Состояния:
  reviewing  — идёт процесс подтверждения позиций одна за одной
  searching  — пользователь вводит текст для ручного поиска товара
"""
import logging
import os
import re as _re
import tempfile
from typing import Dict, List, Optional, Tuple

from rapidfuzz import fuzz

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import (
    ALLOWED_USERS,
    AUTO_MATCH_THRESHOLD,
    MAX_SUGGESTIONS,
    SUGGEST_THRESHOLD,
)
from db.database import Database
from keyboards.kb import (
    AddNewCb,
    ExportCb,
    MatchCb,
    PackSizeCb,
    SearchCancelCb,
    SearchCb,
    SkipBarcodeCb,
    SkipCb,
    StartReviewCb,
    WeightModeCb,
    barcode_skip_keyboard,
    collecting_keyboard,
    container_pack_keyboard,
    export_keyboard,
    item_keyboard,
    pack_size_keyboard,
    search_cancel_keyboard,
    weight_mode_keyboard,
)
from services.excel_exporter import create_invoice_excel
from services.gemini_ocr import extract_barcode, extract_invoice_items
from services.product_matcher import ProductMatcher

# Паттерн маркера товара из инлайн-результата
_PID_RE = _re.compile(r"🔖(\d+)")

# Контейнерные единицы: цена в накладной — за упаковку, нужен вопрос о штучности
_CONTAINER_UNITS = {
    "кор", "кор.", "уп", "уп.", "упак", "упак.", "упаковка",
    "ящ", "ящ.", "ящик", "коробка", "корзина", "box",
}

def _is_container(unit: str) -> bool:
    return unit.lower().rstrip(".").rstrip() in _CONTAINER_UNITS

logger = logging.getLogger(__name__)
router = Router()


class InvoiceState(StatesGroup):
    collecting    = State()   # накопление страниц накладной
    reviewing     = State()   # подтверждение позиций одна за одной
    searching     = State()   # ручной поиск товара
    weight_input  = State()   # ввод количества штук для весового товара
    barcode_photo = State()   # ожидание фото штрихкода нового товара


def _allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


# ── Нечёткий поиск по алиасам ────────────────────────────────────────────────

_ALIAS_THRESHOLD = 0.80   # ≥80% — авто-совпадение по алиасу
_NUM_PENALTY     = 0.28   # штраф за каждый несовпавший числовой маркер (%, объём…)
_ALL_NUMS_RE     = _re.compile(r'\d+[.,]?\d*')

def _find_best_alias(
    query: str,
    barcode: Optional[str],
    aliases: List[Dict],
) -> Tuple[Optional[Dict], float, str]:
    """
    Найти лучший алиас для названия из накладной.

    Порядок поиска:
      1. Точное совпадение имени → score=1.0
      2. Точное совпадение по штрихкоду поставщика → score=1.0
      3. Нечёткое сравнение: token_sort_ratio − числовой штраф

    Числовой штраф (−0.28 за каждый маркер): «2,5%» vs «6%» = −0.28,
    что превращает 97% в 69% → не пройдёт порог 80%.

    Возвращает (alias_dict, score, how) или (None, 0, "").
    how: "exact" / "barcode" / "fuzzy"
    """
    if not aliases:
        return None, 0.0, ""

    q_lower = query.lower().strip()

    # ── Приоритет 1: точное совпадение имени ─────────────────────────────
    for a in aliases:
        if a["supplier_name"].lower().strip() == q_lower:
            return a, 1.0, "exact"

    # ── Приоритет 2: штрихкод поставщика ─────────────────────────────────
    if barcode:
        for a in aliases:
            if a.get("supplier_barcode") and a["supplier_barcode"] == barcode:
                return a, 1.0, "barcode"

    # ── Приоритет 3: нечёткое сравнение с числовым штрафом ───────────────
    # Извлекаем ВСЕ числа из запроса (включая без единиц: «925м» → «925»).
    # Штраф за каждое число которого НЕТ в другой строке.
    # «2.5%» vs «6%»: числа 2.5 и 6 не совпадают → штраф 0.56 → не пройдёт.
    # «925м» vs «925мл»: число 925 есть с обеих сторон → без штрафа.
    q_all_nums = {m.group().replace(',', '.') for m in _ALL_NUMS_RE.finditer(q_lower)}

    best_alias: Optional[Dict] = None
    best_score = 0.0

    for a in aliases:
        a_name = a["supplier_name"].lower()
        text_score = fuzz.token_sort_ratio(q_lower, a_name) / 100.0

        a_all_nums = {m.group().replace(',', '.') for m in _ALL_NUMS_RE.finditer(a_name)}
        hard_diff  = q_all_nums.symmetric_difference(a_all_nums)
        penalty    = len(hard_diff) * _NUM_PENALTY

        score = max(0.0, text_score - penalty)

        if score > best_score:
            best_score = score
            best_alias = a

    if best_alias and best_score >= _ALIAS_THRESHOLD:
        return best_alias, best_score, "fuzzy"

    return None, best_score, ""


# ── Приём фото ────────────────────────────────────────────────────────────────

@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext, db: Database):
    if not _allowed(message.from_user.id):
        return

    current = await state.get_state()

    # Специальный режим: ждём фото штрихкода нового товара
    if current == InvoiceState.barcode_photo:
        await _handle_barcode_photo(message, state, db)
        return

    is_next_page = (current == InvoiceState.collecting)

    if current in (InvoiceState.reviewing, InvoiceState.searching, InvoiceState.weight_input):
        await message.answer(
            "⚠️ Уже идёт проверка накладной. Завершите её или отправьте /cancel для отмены."
        )
        return
    if current is not None and not is_next_page:
        await message.answer(
            "⚠️ Сейчас активен другой режим. Введите /cancel и попробуйте снова."
        )
        return

    # Определяем номер страницы для статус-сообщения
    if is_next_page:
        prev_data   = await state.get_data()
        page_number = prev_data.get("page_count", 1) + 1
        status = await message.answer(f"🔍 Читаю страницу {page_number} через Gemini AI…")
    else:
        page_number = 1
        status = await message.answer("🔍 Читаю накладную через Gemini AI…")

    photo = message.photo[-1]
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await message.bot.download(photo, destination=tmp_path)

        await status.edit_text("🤖 Распознаю товары…")
        try:
            raw_items = await extract_invoice_items(tmp_path)
        except ValueError as e:
            await status.edit_text(str(e), parse_mode="HTML")
            return
        except Exception as e:
            logger.exception("Неожиданная ошибка OCR")
            await status.edit_text(
                f"❌ Непредвиденная ошибка:\n<code>{type(e).__name__}: {str(e)[:200]}</code>",
                parse_mode="HTML",
            )
            return

        if not raw_items:
            await status.edit_text(
                "❌ Товары не найдены на изображении. "
                "Попробуйте сфотографировать чётче или с другого угла."
            )
            return

        products = await db.get_all_products()
        if not products:
            await status.edit_text(
                "❌ База товаров пуста.\n"
                "Загрузите каталог командой /upload и попробуйте снова."
            )
            return

        matcher = ProductMatcher(products)

        # ── Загрузить контекст (новая сессия или продолжение) ─────────────────
        if is_next_page:
            session_id   = prev_data["session_id"]
            all_items    = list(prev_data["items"])
            results      = dict(prev_data["results"])
            review_queue = list(prev_data["review_queue"])
        else:
            session_id   = await db.create_session(message.from_user.id)
            all_items    = []
            results      = {}
            review_queue = []

        page_offset = len(all_items)   # глобальный индекс первого элемента этой страницы

        await status.edit_text(
            f"⚙️ Стр. {page_number}: найдено <b>{len(raw_items)}</b> поз., ищу в базе…",
            parse_mode="HTML",
        )

        # ── Загрузить все алиасы один раз для нечёткого поиска ──────────────
        all_aliases = await db.get_all_aliases()

        # ── Матчинг новых позиций ─────────────────────────────────────────────
        review_set = set(review_queue)   # для быстрой проверки

        for idx, item in enumerate(raw_items):
            gidx = page_offset + idx   # глобальный индекс

            # ── Шаг 1: нечёткий поиск по алиасам ────────────────────────────
            alias, alias_score, alias_how = _find_best_alias(
                item["name"], item.get("barcode"), all_aliases,
            )

            if alias and alias_score >= 0.80:
                needs_review = (
                    item.get("is_weight") or
                    _is_container(item.get("unit", "")) or
                    item.get("price", 0) == 0
                )
                results[gidx] = {
                    "product_id":   alias["product_id"],
                    "product_name": alias["product_name"],
                    "old_price":    alias.get("purchase_price"),
                    "match_type":   "alias",
                }
                if needs_review:
                    review_queue.append(gidx)
                    review_set.add(gidx)
                logger.info(
                    "  [%02d] %-45s → alias(%s) %.0f%%  '%s'  qty=%g  price=%.2f%s",
                    gidx + 1, item["name"][:45], alias_how,
                    alias_score * 100, alias["product_name"][:30],
                    item.get("quantity", 0), item.get("price", 0),
                    "  ⚠️ review" if needs_review else "",
                )
                continue

            # ── Шаг 2: поиск по штрихкоду / названию в базе товаров ─────────
            match_type, matches = matcher.find_matches(
                query=item["name"],
                barcode=item.get("barcode"),
                article=item.get("article"),
                top_n=MAX_SUGGESTIONS,
                auto_threshold=AUTO_MATCH_THRESHOLD,
                suggest_threshold=SUGGEST_THRESHOLD,
            )

            if match_type == "exact_barcode":
                best = matches[0]
                needs_review = (
                    item.get("is_weight") or
                    _is_container(item.get("unit", "")) or
                    item.get("price", 0) == 0
                )
                results[gidx] = {
                    "product_id":   best["product"]["id"],
                    "product_name": best["product"]["name"],
                    "old_price":    best["product"].get("purchase_price"),
                    "match_type":   "exact_barcode",
                }
                if needs_review:
                    review_queue.append(gidx)
                    review_set.add(gidx)
                logger.info(
                    "  [%02d] %-45s → barcode '%s'  qty=%g  price=%.2f%s",
                    gidx + 1, item["name"][:45], best["product"]["name"][:35],
                    item.get("quantity", 0), item.get("price", 0),
                    "  ⚠️ review" if needs_review else "",
                )
                continue

            results[gidx] = {
                "product_id":   matches[0]["product"]["id"] if match_type == "auto" else None,
                "product_name": matches[0]["product"]["name"] if match_type == "auto" else None,
                "old_price":    matches[0]["product"].get("purchase_price") if match_type == "auto" else None,
                "match_type":   match_type,
                "suggestions":  matches,
            }
            review_queue.append(gidx)
            review_set.add(gidx)

            top_name  = matches[0]["product"]["name"][:35] if matches else "—"
            top_score = f"{matches[0]['score']:.2f}" if matches else "—"
            logger.info(
                "  [%02d] %-45s → %-10s '%s'  score=%s  qty=%g  price=%.2f",
                gidx + 1, item["name"][:45], match_type,
                top_name, top_score,
                item.get("quantity", 0), item.get("price", 0),
            )

        all_items = all_items + raw_items   # добавить позиции этой страницы

        # ── Статистика по новой странице ──────────────────────────────────────
        page_r        = range(page_offset, page_offset + len(raw_items))
        page_instant  = sum(1 for i in page_r
                            if results[i]["match_type"] in ("alias", "exact_barcode")
                            and i not in review_set)
        page_alias    = sum(1 for i in page_r if results[i]["match_type"] == "alias"
                            and i not in review_set)
        page_barcode  = sum(1 for i in page_r if results[i]["match_type"] == "exact_barcode"
                            and i not in review_set)
        page_auto     = sum(1 for i in page_r if results[i]["match_type"] == "auto")
        page_suggest  = sum(1 for i in page_r if results[i]["match_type"] == "suggest")
        page_notfound = sum(1 for i in page_r if results[i]["match_type"] == "not_found")
        page_warn     = sum(1 for i in page_r
                            if results[i]["match_type"] in ("alias", "exact_barcode")
                            and i in review_set)

        logger.info(
            "Стр.%d итог: %d поз. | авто=%d  alias=%d  bc=%d  auto=%d  suggest=%d  notfound=%d  warn=%d",
            page_number, len(raw_items),
            page_instant, page_alias, page_barcode,
            page_auto, page_suggest, page_notfound, page_warn,
        )

        # ── Сохранить состояние сбора ─────────────────────────────────────────
        await state.set_state(InvoiceState.collecting)
        await state.update_data(
            session_id=session_id,
            items=all_items,
            results=results,
            review_queue=review_queue,
            review_pos=0,
            page_count=page_number,
        )

        # ── Показать итог страницы + предложение добавить ещё ─────────────────
        total_items = len(all_items)
        need_review = len(review_queue)

        # Формируем строку разбивки
        _auto_detail = []
        if page_alias:   _auto_detail.append(f"псевд.:{page_alias}")
        if page_barcode: _auto_detail.append(f"ШК:{page_barcode}")
        _auto_str = f"  ✅ Авто: <b>{page_instant}</b>" + (
            f" ({', '.join(_auto_detail)})" if _auto_detail else ""
        )
        _review_detail = []
        if page_auto:     _review_detail.append(f"предложено:{page_auto}")
        if page_suggest:  _review_detail.append(f"варианты:{page_suggest}")
        if page_notfound: _review_detail.append(f"не найдено:{page_notfound}")
        if page_warn:     _review_detail.append(f"⚠цена=0:{page_warn}")
        _need_str = f"  👆 Нужен выбор: <b>{need_review}</b>" + (
            f" ({', '.join(_review_detail)})" if _review_detail else ""
        )

        lines = [
            f"📄 <b>Страница {page_number}</b>: {len(raw_items)} позиций",
            _auto_str,
            _need_str,
        ]
        if page_number > 1:
            lines.append(f"\n📚 Итого за {page_number} стр.: <b>{total_items}</b> позиций")
        lines.append("\n📷 Отправьте следующую страницу или нажмите кнопку:")

        await status.edit_text(
            "\n".join(lines),
            reply_markup=collecting_keyboard(session_id, total_items, page_number),
            parse_mode="HTML",
        )

    finally:
        os.unlink(tmp_path)


# ── Переход к проверке после накопления страниц ───────────────────────────────

@router.callback_query(StartReviewCb.filter(), InvoiceState.collecting)
async def on_start_review(
    cb: CallbackQuery,
    state: FSMContext,
    db: Database,
):
    data         = await state.get_data()
    review_queue = data["review_queue"]
    page_count   = data.get("page_count", 1)
    total_items  = len(data["items"])

    await state.set_state(InvoiceState.reviewing)
    await state.update_data(review_pos=0)

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await cb.answer()
    await cb.message.answer(
        f"🔍 Начинаю проверку: <b>{total_items}</b> позиций с <b>{page_count}</b> стр.\n"
        f"Нужно подтвердить: <b>{len(review_queue)}</b>",
        parse_mode="HTML",
    )

    if review_queue:
        await _show_item(cb.message, state, db)
    else:
        await _finalize(cb.message, state, db)


# ── Показ позиции для подтверждения ──────────────────────────────────────────

async def _show_item(
    target: Message | CallbackQuery,
    state: FSMContext,
    db: Database,
):
    data = await state.get_data()
    items:        List[Dict] = data["items"]
    results:      Dict       = data["results"]
    review_queue: List[int]  = data["review_queue"]
    review_pos:   int        = data["review_pos"]

    item_idx = review_queue[review_pos]
    item     = items[item_idx]
    result   = results[item_idx]

    pos_label = f"{review_pos + 1}/{len(review_queue)}"

    # Показываем итоговые единицы; если pack_size>1 — разворачиваем структуру
    qty       = item['quantity']
    pack      = item.get('pack_size', 1) or 1
    unit_str  = item.get('unit', 'шт') or 'шт'
    qty_label = (f"{int(qty/pack)} уп × {pack} шт = {int(qty)} шт"
                 if pack > 1 else f"{qty} {unit_str}")
    # Единица цены: для контейнерных "кор./уп." показываем её, а не "шт"
    price_unit = unit_str if _is_container(unit_str) else "шт"

    match_type  = result.get("match_type")
    suggestions = result.get("suggestions", [])

    # Уведомление о штрихкоде, который есть в накладной, но не нашёлся в базе
    barcode     = item.get("barcode")
    barcode_note = ""
    if barcode and match_type not in ("exact_barcode", "alias"):
        barcode_note = f"\n⚠️ Штрихкод <code>{barcode}</code> не найден в базе"

    header = (
        f"📦 Позиция <b>{pos_label}</b>\n"
        f"Из накладной: <b>{item['name']}</b>\n"
        f"Кол-во: {qty_label} × {item['price']:.2f}/{price_unit}"
        f"{barcode_note}"
    )

    # Весовой товар, уже сопоставленный (alias / barcode) — сразу спросить режим
    if item.get("is_weight") and match_type in ("alias", "exact_barcode"):
        text = (
            f"{header}\n\n"
            f"⚖️ Товар: <b>{result['product_name']}</b>\n"
            f"Как учитывать весовой товар?"
        )
        kb = weight_mode_keyboard(item_idx)
    # Контейнерный товар, уже сопоставленный (alias / barcode) — подтвердить и задать штучность
    elif _is_container(unit_str) and match_type in ("alias", "exact_barcode"):
        text = (
            f"{header}\n\n"
            f"⚡ Товар: <b>{result['product_name']}</b>\n"
            f"Единица «{unit_str}» — уточните кол-во штук в упаковке:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"✅ {result['product_name'][:48]}",
                callback_data=MatchCb(item_idx=item_idx, product_id=result["product_id"]).pack(),
            )],
            [InlineKeyboardButton(
                text="⏭ Пропустить",
                callback_data=SkipCb(item_idx=item_idx).pack(),
            )],
        ])
    # Уже сопоставленный товар (alias/barcode), но цена = 0 — OCR не считал данные
    elif match_type in ("alias", "exact_barcode") and result.get("product_id"):
        prod_name   = result.get("product_name", "?")
        match_label = "псевдоним" if match_type == "alias" else "штрихкод"
        text = (
            f"{header}\n\n"
            f"⚠️ <b>OCR не смог считать цену — проверьте данные в Excel</b>\n\n"
            f"✅ Товар: <b>{prod_name}</b> ({match_label})"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"✅ {prod_name[:48]}",
                callback_data=MatchCb(item_idx=item_idx, product_id=result["product_id"]).pack(),
            )],
            [
                InlineKeyboardButton(
                    text="🔄 Другой товар",
                    callback_data=SearchCb(item_idx=item_idx).pack(),
                ),
                InlineKeyboardButton(
                    text="⏭ Пропустить",
                    callback_data=SkipCb(item_idx=item_idx).pack(),
                ),
            ],
        ])
    elif match_type == "auto" and suggestions:
        best      = suggestions[0]
        old_price = best["product"].get("purchase_price") or 0
        price_note = ""
        if old_price and item["price"] and abs(item["price"] - old_price) > 0.001:
            pct = (item["price"] - old_price) / old_price * 100
            price_note = f"\n💰 Цена: {old_price:.2f} → {item['price']:.2f} ({pct:+.1f}%)"

        text = f"{header}\n\n🤖 Авто-совпадение:{price_note}"
        kb   = item_keyboard(item_idx, suggestions, auto_match=best)
    elif suggestions:
        text = f"{header}\n\n❓ Выберите из базы:"
        kb   = item_keyboard(item_idx, suggestions)
    else:
        text = f"{header}\n\n🔎 Товар не найден. Найдите вручную:"
        kb   = item_keyboard(item_idx, [])

    send = (
        target.answer if isinstance(target, Message)
        else target.message.answer
    )
    await send(text, reply_markup=kb, parse_mode="HTML")


# ── Callbacks: Match / Skip / Search ─────────────────────────────────────────

@router.callback_query(MatchCb.filter())
async def on_match(cb: CallbackQuery, callback_data: MatchCb, state: FSMContext, db: Database):
    current = await state.get_state()
    if current not in (InvoiceState.reviewing, InvoiceState.searching):
        await cb.answer("Нет активной сессии.", show_alert=True)
        return

    item_idx   = callback_data.item_idx
    product_id = callback_data.product_id

    data    = await state.get_data()
    items   = data["items"]
    results = data["results"]
    item    = items[item_idx]
    product = await db.get_product_by_id(product_id)

    # Сохранить alias при любом подтверждении пользователем:
    # suggest / not_found / searching / pending — ручной выбор
    # auto — пользователь нажал ✅ на авто-совпадение (тоже подтверждение!)
    # alias / exact_barcode — перезаписываем псевдоним (обновляет use_count)
    old_match = results[item_idx].get("match_type", "pending")
    await db.save_alias(
        supplier_name=item["name"],
        product_id=product_id,
        supplier_barcode=item.get("barcode"),
        confirmed_by=cb.from_user.id,
    )
    final_match = "manual" if old_match not in ("alias", "exact_barcode") else old_match

    results[item_idx] = {
        "product_id":   product_id,
        "product_name": product["name"],
        "old_price":    product.get("purchase_price"),
        "match_type":   final_match,
    }

    await state.set_state(InvoiceState.reviewing)
    await _advance(cb, state, db, results, data)


@router.callback_query(SkipCb.filter())
async def on_skip(cb: CallbackQuery, callback_data: SkipCb, state: FSMContext, db: Database):
    current = await state.get_state()
    if current not in (InvoiceState.reviewing, InvoiceState.searching):
        await cb.answer("Нет активной сессии.", show_alert=True)
        return

    # Если пропускаем из поиска — перейти в reviewing перед advance
    await state.set_state(InvoiceState.reviewing)

    data    = await state.get_data()
    results = data["results"]

    results[callback_data.item_idx] = {
        "product_id": None,
        "match_type": "skipped",
    }
    await cb.answer("⏭ Пропущено")
    await _advance(cb, state, db, results, data)


@router.callback_query(SearchCb.filter(), InvoiceState.reviewing)
async def on_search_start(cb: CallbackQuery, callback_data: SearchCb, state: FSMContext):
    await state.set_state(InvoiceState.searching)
    await state.update_data(searching_item_idx=callback_data.item_idx)

    bot_info = await cb.message.bot.get_me()
    username = bot_info.username

    await cb.message.answer(
        "🔍 <b>Найти товар вручную</b>\n\n"
        "<b>Способ 1 — набрать текст прямо здесь:</b>\n"
        "Введите название или часть названия:\n\n"
        f"<b>Способ 2 — инлайн-поиск (рекомендуется):</b>\n"
        f"Введите в строке ввода: <code>@{username} запрос</code>\n"
        "Выберите товар из выпадающего списка — он вставится сюда автоматически.",
        reply_markup=search_cancel_keyboard(callback_data.item_idx),
        parse_mode="HTML",
    )
    await cb.answer()


@router.callback_query(SearchCancelCb.filter(), InvoiceState.searching)
async def on_search_cancel(cb: CallbackQuery, callback_data: SearchCancelCb, state: FSMContext, db: Database):
    await state.set_state(InvoiceState.reviewing)
    await cb.answer("Поиск отменён")
    await _show_item(cb, state, db)


@router.message(InvoiceState.searching, F.text)
async def on_search_query(message: Message, state: FSMContext, db: Database):
    text     = message.text.strip()
    data     = await state.get_data()
    item_idx = data["searching_item_idx"]

    # ── Детект инлайн-карточки товара (содержит маркер 🔖ID) ─────────────────
    pid_match = _PID_RE.search(text)
    if pid_match:
        product_id = int(pid_match.group(1))
        product    = await db.get_product_by_id(product_id)
        if product:
            items   = data["items"]
            results = data["results"]
            item    = items[item_idx]

            # Сохранить alias
            await db.save_alias(
                supplier_name    = item["name"],
                product_id       = product_id,
                supplier_barcode = item.get("barcode"),
                confirmed_by     = message.from_user.id,
            )
            results[item_idx] = {
                "product_id":   product_id,
                "product_name": product["name"],
                "old_price":    product.get("purchase_price"),
                "match_type":   "manual",
            }
            await state.set_state(InvoiceState.reviewing)
            await message.answer(f"✅ Выбрано: <b>{product['name']}</b>", parse_mode="HTML")

            # pack_size — пропускаем для весовых товаров
            if not item.get("is_weight"):
                pack_msg = await _maybe_ask_pack_size(message, item, product, item_idx)
                if pack_msg:
                    await state.update_data(results=results)
                    return   # ждём ответа о pack_size

            # Продолжить (вес будет проверен внутри _advance_from_message)
            await state.update_data(results=results)
            await _advance_from_message(message, state, db, results, data)
            return
        else:
            await message.answer("❌ Товар с таким ID не найден в базе.")
            return

    # ── Обычный текстовый поиск ───────────────────────────────────────────────
    if len(text) < 2:
        await message.answer("Введите не менее 2 символов.")
        return

    products = await db.get_all_products()
    matcher  = ProductMatcher(products)
    _, matches = matcher.find_matches(text, top_n=8, suggest_threshold=0.08)

    if not matches:
        await message.answer(
            f"❌ По запросу <b>«{text}»</b> ничего не найдено.\n\n"
            "Попробуйте другое название, часть бренда или категорию — "
            "или воспользуйтесь кнопками ниже:",
            reply_markup=search_cancel_keyboard(item_idx),
            parse_mode="HTML",
        )
        return

    btns = [
        [InlineKeyboardButton(
            text=f"{m['product']['name'][:48]}",
            callback_data=MatchCb(item_idx=item_idx, product_id=m["product"]["id"]).pack(),
        )]
        for m in matches
    ]
    # Дополнительный ряд: искать снова / пропустить / добавить
    btns.append([
        InlineKeyboardButton(
            text="🔍 Искать снова",
            callback_data=SearchCb(item_idx=item_idx).pack(),
        ),
        InlineKeyboardButton(
            text="⏭ Пропустить",
            callback_data=SkipCb(item_idx=item_idx).pack(),
        ),
    ])
    btns.append([
        InlineKeyboardButton(
            text="➕ Добавить в Excel",
            callback_data=AddNewCb(item_idx=item_idx).pack(),
        ),
    ])

    await state.set_state(InvoiceState.reviewing)
    await message.answer(
        f"🔎 Результаты по <b>«{text}»</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
        parse_mode="HTML",
    )


# ── /cancel ───────────────────────────────────────────────────────────────────

@router.message(F.text == "/cancel")
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    if current in (InvoiceState.collecting, InvoiceState.reviewing, InvoiceState.searching, InvoiceState.weight_input, InvoiceState.barcode_photo):
        await state.clear()
        await message.answer("🚫 Обработка накладной отменена.")
    else:
        await message.answer("Нет активной сессии.")


# ── Pack size ─────────────────────────────────────────────────────────────────

@router.callback_query(PackSizeCb.filter(), InvoiceState.reviewing)
async def on_pack_size(cb: CallbackQuery, callback_data: PackSizeCb, state: FSMContext, db: Database):
    """Пользователь ответил на вопрос о размере упаковки."""
    item_idx  = callback_data.item_idx
    pack_size = callback_data.pack_size

    data    = await state.get_data()
    items   = data["items"]
    results = data["results"]
    item    = items[item_idx]
    result  = results[item_idx]

    orig_unit = item.get("unit", "шт") or "шт"

    if pack_size > 1:
        # Сохранить pack_size в БД и пересчитать unit_price
        await db.update_pack_size(result["product_id"], pack_size)
        unit_price = item["price"] / pack_size
        results[item_idx]["pack_size"]  = pack_size
        results[item_idx]["unit_price"] = unit_price
        # Для контейнерных единиц: разворачиваем quantity → штуки
        if _is_container(orig_unit):
            items[item_idx]["quantity"] = item["quantity"] * pack_size
            items[item_idx]["unit"]     = "шт"
            await state.update_data(items=items)
        await cb.answer(f"📦 {pack_size} шт/уп, цена/шт={unit_price:.2f}")
    else:
        results[item_idx]["pack_size"] = 1
        # pack_size=1 для контейнера означает «каждая упаковка = 1 шт»
        if _is_container(orig_unit):
            items[item_idx]["unit"] = "шт"
            await state.update_data(items=items)
        await cb.answer("✅ Цена записана как есть")

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await _advance_from_message(cb.message, state, db, results, data)


# ── Экспорт в Excel ───────────────────────────────────────────────────────────

@router.callback_query(ExportCb.filter())
async def on_export(cb: CallbackQuery, callback_data: ExportCb, db: Database):
    await cb.answer("Генерирую файл…")

    items = await db.get_session_items(callback_data.session_id)
    if not items:
        await cb.message.answer("❌ Данные сессии не найдены.")
        return

    excel_bytes = create_invoice_excel(items)

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(excel_bytes)
        tmp_path = tmp.name

    try:
        from datetime import datetime
        filename = f"invoice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        await cb.message.answer_document(
            FSInputFile(tmp_path, filename=filename),
            caption="📊 Готово! Загрузите этот файл в Umag.",
        )
    finally:
        os.unlink(tmp_path)


# ── Вспомогательные функции ───────────────────────────────────────────────────

async def _advance(
    cb: CallbackQuery,
    state: FSMContext,
    db: Database,
    results: Dict,
    data: Dict,
):
    """Переход к следующей позиции (из callback) или завершение сессии."""
    # Проверить pack_size перед тем как двигаться дальше
    item_idx = data["review_queue"][data["review_pos"]]
    item     = data["items"][item_idx]
    result   = results.get(item_idx, {})

    # Упаковка: пропускаем для весовых товаров (другой режим учёта)
    if result.get("product_id") and "pack_size" not in result and not item.get("is_weight"):
        product = await db.get_product_by_id(result["product_id"])
        pack_msg = await _maybe_ask_pack_size(cb.message, item, product, item_idx)
        if pack_msg:
            await state.update_data(results=results)
            return   # ждём ответа на вопрос о pack_size

    # Вес: если товар весовой и режим ещё не выбран — задать вопрос
    if result.get("product_id") and item.get("is_weight") and "weight_mode" not in result:
        await _ask_weight_mode(cb.message, item, item_idx)
        await state.update_data(results=results)
        return   # ждём ответа на вопрос о весовом режиме

    review_queue: List[int] = data["review_queue"]
    review_pos:   int       = data["review_pos"]
    next_pos = review_pos + 1

    await state.update_data(results=results, review_pos=next_pos)

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if next_pos < len(review_queue):
        await _show_item(cb, state, db)
    else:
        await _finalize(cb.message, state, db)


async def _advance_from_message(
    message: Message,
    state: FSMContext,
    db: Database,
    results: Dict,
    data: Dict,
):
    """Переход к следующей позиции (из Message) — после ручного выбора / pack_size / weight."""
    review_queue: List[int] = data["review_queue"]
    review_pos:   int       = data["review_pos"]
    item_idx = review_queue[review_pos]
    item     = data["items"][item_idx]
    result   = results.get(item_idx, {})

    # Вес: если товар весовой и режим ещё не выбран — задать вопрос
    if result.get("product_id") and item.get("is_weight") and "weight_mode" not in result:
        await _ask_weight_mode(message, item, item_idx)
        await state.update_data(results=results)
        return   # ждём ответа на вопрос о весовом режиме

    next_pos = review_pos + 1
    await state.update_data(results=results, review_pos=next_pos)

    if next_pos < len(review_queue):
        await _show_item(message, state, db)
    else:
        await _finalize(message, state, db)


def _detect_pack_anomaly(
    invoice_price: float,
    known_price: float,
    known_pack: int = 1,
) -> List[int]:
    """
    Возвращает список вероятных размеров упаковки если цена выглядит аномально.
    Пустой список = цена нормальная.
    """
    if not (invoice_price > 0 and known_price > 0):
        return []

    effective = invoice_price / max(known_pack, 1)
    ratio     = effective / known_price

    # Цена в пределах ±30% — норма
    if 0.70 <= ratio <= 1.30:
        return []

    # Цена ниже ожидаемой — скидка, не трогаем
    if ratio < 0.70:
        return []

    # Цена значительно выше — ищем делители близкие к ratio
    candidates = []
    for pack in [2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 24, 30, 36, 48]:
        unit_p = effective / pack
        if 0.75 <= unit_p / known_price <= 1.30:
            candidates.append(pack)

    return candidates[:3]   # не больше 3 вариантов


async def _maybe_ask_pack_size(
    message: Message,
    item: Dict,
    product: Optional[Dict],
    item_idx: int,
) -> bool:
    """
    Спрашивает про размер упаковки в двух случаях:
    1. Единица — контейнерная (кор./уп./ящик и т.п.) — ВСЕГДА, независимо от цены в базе.
    2. Цена подозрительно выше известной цены/шт — аномалия упаковки.
    Возвращает True если вопрос был задан (нужно ждать ответа).
    """
    inv_price = item.get("price") or 0
    unit      = item.get("unit", "") or ""

    if not inv_price:
        return False

    # ── Случай 1: контейнерная единица (кор./уп./ящик) ───────────────────────
    if _is_container(unit):
        prod_name = product["name"] if product else item["name"]
        await message.answer(
            f"📦 <b>Единица поставки: {unit}</b>\n\n"
            f"Товар: <b>{prod_name}</b>\n"
            f"Цена за {unit}: <b>{inv_price:.2f}</b>\n\n"
            "Сколько штук в одной упаковке?",
            reply_markup=container_pack_keyboard(item_idx, inv_price),
            parse_mode="HTML",
        )
        return True

    # ── Случай 2: аномалия цены относительно известной ────────────────────────
    if not product:
        return False

    known_price = product.get("purchase_price") or 0
    known_pack  = product.get("pack_size") or 1

    if not known_price:
        return False

    suggestions = _detect_pack_anomaly(inv_price, known_price, known_pack)
    if not suggestions:
        return False

    await message.answer(
        f"💡 <b>Возможно, это упаковка?</b>\n\n"
        f"Товар: <b>{product['name']}</b>\n"
        f"Известная цена/шт: <b>{known_price:.2f}</b>\n"
        f"Цена в накладной: <b>{inv_price:.2f}</b>\n"
        f"Это в <b>~{inv_price/known_price:.0f}×</b> выше ожидаемого\n\n"
        f"Выберите размер упаковки или подтвердите что цена верная:",
        reply_markup=pack_size_keyboard(item_idx, suggestions, inv_price, known_price),
        parse_mode="HTML",
    )
    return True


# ── Добавить новый товар в Excel (без записи в базу) ─────────────────────────

@router.callback_query(AddNewCb.filter())
async def on_add_new(
    cb: CallbackQuery,
    callback_data: AddNewCb,
    state: FSMContext,
    db: Database,
):
    """Пользователь хочет добавить товар в Excel «как есть», без сопоставления с базой."""
    current = await state.get_state()
    if current not in (InvoiceState.reviewing, InvoiceState.searching):
        await cb.answer("Нет активной сессии.", show_alert=True)
        return

    item_idx = callback_data.item_idx
    data     = await state.get_data()
    items    = data["items"]
    results  = data["results"]
    item     = items[item_idx]

    results[item_idx] = {
        "product_id":   None,
        "product_name": None,
        "match_type":   "new_product",
    }

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await state.set_state(InvoiceState.reviewing)

    if item.get("barcode"):
        # Штрихкод уже есть — сразу добавляем
        await cb.answer("➕ Добавлено в Excel")
        await state.update_data(results=results)
        await _advance_from_message(cb.message, state, db, results, data)
    else:
        # Штрихкода нет — просим сфотографировать
        await state.update_data(results=results, barcode_item_idx=item_idx)
        await state.set_state(InvoiceState.barcode_photo)
        await cb.answer()
        await cb.message.answer(
            f"📷 <b>Сфотографируйте штрихкод</b>\n\n"
            f"Товар: <b>{item['name']}</b>\n\n"
            "Сделайте чёткое фото штрихкода (EAN-13 / Code 128).\n"
            "Gemini распознает его автоматически.\n\n"
            "<i>Или пропустите если штрихкода нет:</i>",
            reply_markup=barcode_skip_keyboard(item_idx),
            parse_mode="HTML",
        )


@router.message(InvoiceState.barcode_photo, F.text, ~F.text.startswith("/"))
async def barcode_photo_text_hint(message: Message):
    """Пользователь написал текст вместо фото — показываем подсказку."""
    await message.answer("📷 Пришлите <b>фотографию</b> штрихкода, или нажмите кнопку «Добавить без штрихкода».", parse_mode="HTML")


async def _handle_barcode_photo(message: Message, state: FSMContext, db: Database):
    """Обработать фото штрихкода (вызывается из handle_photo при state==barcode_photo)."""
    data     = await state.get_data()
    item_idx = data["barcode_item_idx"]
    items    = data["items"]
    results  = data["results"]
    item     = items[item_idx]

    status = await message.answer("🔍 Считываю штрихкод через Gemini…")

    photo = message.photo[-1]
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await message.bot.download(photo, destination=tmp_path)
        barcode = await extract_barcode(tmp_path)
    except Exception:
        logger.exception("Ошибка при считывании штрихкода")
        await status.edit_text("❌ Ошибка при обработке фото. Попробуйте ещё раз.")
        return
    finally:
        os.unlink(tmp_path)

    if barcode:
        items[item_idx]["barcode"] = barcode
        await state.set_state(InvoiceState.reviewing)
        await state.update_data(items=items)
        await status.edit_text(
            f"✅ Штрихкод: <code>{barcode}</code>\n"
            f"Товар <b>{item['name']}</b> добавлен в Excel.",
            parse_mode="HTML",
        )
        await _advance_from_message(message, state, db, results, data)
    else:
        await status.edit_text(
            "❌ Не удалось считать штрихкод.\n\n"
            "Попробуйте снова:\n"
            "• Штрихкод должен полностью попасть в кадр\n"
            "• Достаточно освещения, без засветок\n"
            "• Камера в фокусе\n\n"
            "Или пропустите штрихкод:",
            reply_markup=barcode_skip_keyboard(item_idx),
        )


@router.callback_query(SkipBarcodeCb.filter(), InvoiceState.barcode_photo)
async def on_skip_barcode(
    cb: CallbackQuery,
    callback_data: SkipBarcodeCb,
    state: FSMContext,
    db: Database,
):
    """Пользователь отказался сканировать штрихкод."""
    data    = await state.get_data()
    results = data["results"]

    await state.set_state(InvoiceState.reviewing)
    await cb.answer("⏩ Добавлено без штрихкода")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await _advance_from_message(cb.message, state, db, results, data)


async def _ask_weight_mode(message: Message, item: Dict, item_idx: int) -> None:
    """Спросить пользователя, как учитывать весовой товар: по кг или поштучно."""
    await message.answer(
        f"⚖️ <b>Весовой товар</b>\n\n"
        f"<b>{item['name']}</b>\n"
        f"Количество в накладной: <b>{item['quantity']} {item['unit']}</b>\n\n"
        "Как учитывать этот товар?",
        reply_markup=weight_mode_keyboard(item_idx),
        parse_mode="HTML",
    )


# ── Weight mode ───────────────────────────────────────────────────────────────

@router.callback_query(WeightModeCb.filter(), InvoiceState.reviewing)
async def on_weight_mode(
    cb: CallbackQuery,
    callback_data: WeightModeCb,
    state: FSMContext,
    db: Database,
):
    """Пользователь выбрал режим учёта весового товара."""
    item_idx = callback_data.item_idx
    mode     = callback_data.mode   # "kg" or "pieces"

    data    = await state.get_data()
    items   = data["items"]
    results = data["results"]
    item    = items[item_idx]

    results[item_idx]["weight_mode"] = mode

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if mode == "kg":
        await cb.answer("⚖️ Учитываем по весу (кг)")
        await _advance_from_message(cb.message, state, db, results, data)
    else:
        # Нужно узнать количество штук
        await state.update_data(results=results, weight_item_idx=item_idx)
        await state.set_state(InvoiceState.weight_input)
        await cb.answer("📦 Введите количество штук")
        await cb.message.answer(
            f"📦 <b>Поштучный учёт</b>\n\n"
            f"Товар: <b>{item['name']}</b>\n"
            f"Вес в накладной: <b>{item['quantity']} {item['unit']}</b>\n"
            f"Итоговая сумма: <b>{item.get('total_price') or item['price'] * item['quantity']:.2f}</b>\n\n"
            "Введите количество штук (например: <code>12</code>):\n"
            "<i>(или /cancel для отмены)</i>",
            parse_mode="HTML",
        )


@router.message(InvoiceState.weight_input, F.text, ~F.text.startswith("/"))
async def on_weight_count(message: Message, state: FSMContext, db: Database):
    """Пользователь ввёл количество штук для весового товара."""
    data     = await state.get_data()
    item_idx = data["weight_item_idx"]
    items    = data["items"]
    results  = data["results"]
    item     = items[item_idx]

    text = message.text.strip().replace(",", ".")
    try:
        count = float(text)
        if count <= 0:
            raise ValueError("non-positive")
    except ValueError:
        await message.answer("❌ Введите количество штук — число больше нуля (например: <code>12</code>)", parse_mode="HTML")
        return

    total_price = item.get("total_price") or 0
    if not total_price:
        total_price = item["price"] * item["quantity"]
    unit_price = total_price / count if count > 0 else 0

    items[item_idx]["quantity"]    = count
    items[item_idx]["unit"]        = "шт"
    items[item_idx]["price"]       = unit_price

    await state.set_state(InvoiceState.reviewing)
    await state.update_data(items=items)

    await message.answer(
        f"✅ <b>Пересчитано:</b> {count:.0f} шт × {unit_price:.2f}/шт",
        parse_mode="HTML",
    )

    await _advance_from_message(message, state, db, results, data)


async def _finalize(message: Message, state: FSMContext, db: Database):
    """Сохранить все позиции, обновить цены, показать итог."""
    data       = await state.get_data()
    session_id = data["session_id"]
    items      = data["items"]
    results    = data["results"]

    confirmed     = 0
    skipped       = 0
    added_new     = 0
    price_changes = 0

    for idx, item in enumerate(items):
        result = results.get(idx) or {"product_id": None, "match_type": "skipped"}
        product_id = result.get("product_id")
        old_price: Optional[float] = None

        if product_id:
            product   = await db.get_product_by_id(product_id)
            old_price = product.get("purchase_price") if product else None

            # pack_size: приоритет явный (не через or-chain, т.к. 1 — truthy)
            #   1. Пользователь выбрал в этой сессии (result["pack_size"])
            #   2. Gemini явно увидел формат N×M (gemini_pack > 1)
            #   3. Ранее подтверждено человеком и сохранено в БД (db_pack > 1)
            #   4. По умолчанию — 1
            gemini_pack     = item.get("pack_size") or 1
            db_pack         = (product.get("pack_size") if product else 1) or 1
            result_pack     = result.get("pack_size")  # может быть None или int
            if result_pack and result_pack > 1:
                pack_size = result_pack   # только что подтверждено пользователем
            elif gemini_pack > 1:
                pack_size = gemini_pack   # Gemini явно распознал N×M
            elif db_pack > 1:
                pack_size = db_pack       # ранее сохранено в базе
            else:
                pack_size = 1

            # unit_price: приоритет — скорректированная пользователем цена (on_pack_size),
            # затем — пересчёт для контейнерных единиц (кор./уп.) с pack_size,
            # иначе берём Gemini (price = уже цена за единицу по Правилу №1)
            if result.get("unit_price") and result["unit_price"] > 0:
                unit_price = result["unit_price"]           # пользователь уточнил
            elif _is_container(item.get("unit", "")) and pack_size > 1:
                unit_price = item["price"] / pack_size      # цена за кор. ÷ штук в кор.
            else:
                unit_price = item["price"]                  # Gemini: total / quantity

            # Если pack_size изменился — сохранить в БД
            if pack_size > 1 and db_pack != pack_size:
                await db.update_pack_size(product_id, pack_size)

            if unit_price > 0:
                await db.update_product_price(product_id, unit_price)
                if old_price and abs(unit_price - old_price) > 0.001:
                    price_changes += 1

            confirmed += 1
        elif result.get("match_type") == "new_product":
            # Новый товар: добавляем в Excel данными из накладной, в базу не пишем
            unit_price = item["price"]
            pack_size  = item.get("pack_size") or 1
            added_new += 1
        else:
            unit_price = item["price"]
            pack_size  = item.get("pack_size") or 1
            skipped   += 1

        await db.add_invoice_item(
            session_id=session_id,
            product_id=product_id,
            supplier_name=item["name"],
            supplier_barcode=item.get("barcode"),
            quantity=item["quantity"],          # уже итоговые единицы от Gemini
            price=unit_price,
            old_price=old_price,
            unit=item["unit"],
            match_type=result.get("match_type", "skipped"),
        )

    await db.complete_session(session_id, confirmed)
    await state.clear()

    price_note = f"\n💰 Изменений цен: <b>{price_changes}</b>" if price_changes else ""
    new_note   = f"\n➕ Новых товаров: <b>{added_new}</b>" if added_new else ""

    await message.answer(
        f"✅ <b>Накладная обработана!</b>\n\n"
        f"📦 Принято позиций: <b>{confirmed}</b>\n"
        f"⏭ Пропущено: <b>{skipped}</b>"
        f"{new_note}"
        f"{price_note}\n\n"
        f"Нажмите кнопку для получения Excel-файла:",
        reply_markup=export_keyboard(session_id),
        parse_mode="HTML",
    )
