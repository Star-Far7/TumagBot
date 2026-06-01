"""
Режим редактирования товаров в базе данных.
Команда /edit — поиск товара и редактирование его полей.
"""
import logging
import re as _re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import ALLOWED_USERS
from db.database import Database
from services.product_matcher import ProductMatcher

logger = logging.getLogger(__name__)
router = Router()

_PID_RE = _re.compile(r"🔖(\d+)")

# ── Редактируемые поля ────────────────────────────────────────────────────────

_FIELDS: dict[str, str] = {
    "name":           "✏️ Название",
    "barcode":        "🔖 Штрихкод",
    "extra_code":     "🔖² Доп. код",
    "category":       "📁 Категория",
    "unit":           "📦 Ед.изм.",
    "purchase_price": "💰 Цена закупа",
    "pack_size":      "🔢 Pack size",
}

_FIELD_TYPE = {
    "name":           str,
    "barcode":        str,
    "extra_code":     str,
    "category":       str,
    "unit":           str,
    "purchase_price": float,
    "pack_size":      int,
}

_FIELD_HINT = {
    "purchase_price": " (число, напр.: 125.50)",
    "pack_size":      " (целое число ≥ 1, напр.: 12)",
    "barcode":        " (цифры EAN, или «-» чтобы очистить)",
    "extra_code":     " (цифры, или «-» чтобы очистить)",
    "category":       " (или «-» чтобы очистить)",
}

# ── FSM States ────────────────────────────────────────────────────────────────

class EditState(StatesGroup):
    searching     = State()   # ждём поисковый запрос
    editing_field = State()   # ждём новое значение поля


# ── Callback data ─────────────────────────────────────────────────────────────

class EditSelectCb(CallbackData, prefix="editsel"):
    product_id: int

class EditFieldCb(CallbackData, prefix="editf"):
    product_id: int
    field: str

class EditAliasCb(CallbackData, prefix="editali"):
    product_id: int

class DeleteAliasCb(CallbackData, prefix="delali"):
    alias_id:   int
    product_id: int

class BackToCardCb(CallbackData, prefix="backcard"):
    product_id: int

class EditDoneCb(CallbackData, prefix="editdone"):
    product_id: int


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


def _card_text(product: dict, aliases: list) -> str:
    lines = [
        f"📋 <b>{product['name']}</b>",
        "",
        f"🔖 Штрихкод:    <code>{product.get('barcode') or '—'}</code>",
        f"🔖² Доп. код:   <code>{product.get('extra_code') or '—'}</code>",
        f"📁 Категория:   {product.get('category') or '—'}",
        f"📦 Ед.изм.:     {product.get('unit') or 'шт'}",
        f"💰 Цена закупа: <b>{(product.get('purchase_price') or 0):.2f}</b>",
        f"🔢 Pack size:   <b>{product.get('pack_size') or 1}</b>",
    ]
    if aliases:
        lines += ["", f"🏷 <b>Алиасы поставщиков ({len(aliases)}):</b>"]
        for a in aliases[:12]:
            lines.append(f"  • {a['supplier_name']}  <i>(×{a['use_count']})</i>")
        if len(aliases) > 12:
            lines.append(f"  <i>… и ещё {len(aliases) - 12}</i>")
    return "\n".join(lines)


def _card_kb(product_id: int, alias_count: int) -> InlineKeyboardMarkup:
    btns = [
        [
            InlineKeyboardButton(text="✏️ Название",    callback_data=EditFieldCb(product_id=product_id, field="name").pack()),
            InlineKeyboardButton(text="📁 Категория",   callback_data=EditFieldCb(product_id=product_id, field="category").pack()),
        ],
        [
            InlineKeyboardButton(text="🔖 Штрихкод",    callback_data=EditFieldCb(product_id=product_id, field="barcode").pack()),
            InlineKeyboardButton(text="🔖² Доп. код",   callback_data=EditFieldCb(product_id=product_id, field="extra_code").pack()),
        ],
        [
            InlineKeyboardButton(text="📦 Ед.изм.",     callback_data=EditFieldCb(product_id=product_id, field="unit").pack()),
            InlineKeyboardButton(text="💰 Цена",        callback_data=EditFieldCb(product_id=product_id, field="purchase_price").pack()),
            InlineKeyboardButton(text="🔢 Pack size",   callback_data=EditFieldCb(product_id=product_id, field="pack_size").pack()),
        ],
    ]
    if alias_count:
        btns.append([
            InlineKeyboardButton(
                text=f"🏷 Алиасы ({alias_count})",
                callback_data=EditAliasCb(product_id=product_id).pack(),
            )
        ])
    btns.append([
        InlineKeyboardButton(text="✅ Готово", callback_data=EditDoneCb(product_id=product_id).pack()),
    ])
    return InlineKeyboardMarkup(inline_keyboard=btns)


def _alias_text_and_kb(product: dict, aliases: list) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"🏷 <b>Алиасы: {product['name']}</b>\n\n"
        f"Нажмите <b>❌</b> рядом с алиасом чтобы удалить его:"
    )
    btns = [
        [InlineKeyboardButton(
            text=f"❌ {a['supplier_name'][:52]}  (×{a['use_count']})",
            callback_data=DeleteAliasCb(alias_id=a["id"], product_id=product["id"]).pack(),
        )]
        for a in aliases
    ]
    btns.append([
        InlineKeyboardButton(
            text="◀️ Назад к карточке",
            callback_data=BackToCardCb(product_id=product["id"]).pack(),
        )
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=btns)


async def _show_card(
    target: Message | CallbackQuery,
    product_id: int,
    db: Database,
):
    """
    Показать или обновить карточку товара.
    Message  → отправить новое сообщение.
    CallbackQuery → отредактировать текущее сообщение.
    """
    product = await db.get_product_by_id(product_id)
    if not product:
        err = "❌ Товар не найден в базе."
        if isinstance(target, CallbackQuery):
            await target.answer(err, show_alert=True)
        else:
            await target.answer(err)
        return None

    aliases = await db.get_product_aliases(product_id)
    text    = _card_text(product, aliases)
    kb      = _card_kb(product_id, len(aliases))

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await target.answer()
        return target.message
    else:
        return await target.answer(text, reply_markup=kb, parse_mode="HTML")


# ── Команда /edit ──────────────────────────────────────────────────────────────

@router.message(Command("edit"))
async def cmd_edit(message: Message, state: FSMContext):
    if not _allowed(message.from_user.id):
        return

    await state.clear()
    await state.set_state(EditState.searching)

    bot_info = await message.bot.get_me()
    await message.answer(
        "🔍 <b>Режим редактирования товара</b>\n\n"
        "Введите название или часть названия:\n\n"
        "<b>Или используйте инлайн-поиск:</b>\n"
        f"<code>@{bot_info.username} запрос</code> — выберите товар из списка\n\n"
        "/cancel — выйти из режима редактирования",
        parse_mode="HTML",
    )


# ── Поиск ────────────────────────────────────────────────────────────────────

@router.message(EditState.searching, F.text, ~F.text.startswith("/"))
async def edit_search(message: Message, state: FSMContext, db: Database):
    text = message.text.strip()

    # Инлайн-карточка (маркер 🔖ID) — мгновенный выбор
    m = _PID_RE.search(text)
    if m:
        product_id = int(m.group(1))
        await state.clear()
        await _show_card(message, product_id, db)
        return

    if len(text) < 2:
        await message.answer("Введите минимум 2 символа.")
        return

    products = await db.get_all_products()
    matcher  = ProductMatcher(products)
    _, matches = matcher.find_matches(text, top_n=8, suggest_threshold=0.08)

    if not matches:
        await message.answer(
            "❌ Ничего не найдено. Попробуйте другой запрос.",
        )
        return

    btns = [
        [InlineKeyboardButton(
            text=m["product"]["name"][:56],
            callback_data=EditSelectCb(product_id=m["product"]["id"]).pack(),
        )]
        for m in matches
    ]
    await message.answer(
        f"🔎 Найдено по «{text}»:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
    )


# ── Выбор товара из списка ────────────────────────────────────────────────────

@router.callback_query(EditSelectCb.filter())
async def edit_select(cb: CallbackQuery, callback_data: EditSelectCb, state: FSMContext, db: Database):
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _show_card(cb.message, callback_data.product_id, db)


# ── Нажата кнопка поля → запрос нового значения ──────────────────────────────

@router.callback_query(EditFieldCb.filter())
async def edit_field_start(cb: CallbackQuery, callback_data: EditFieldCb, state: FSMContext, db: Database):
    product_id = callback_data.product_id
    field      = callback_data.field
    label      = _FIELDS[field]

    product = await db.get_product_by_id(product_id)
    if not product:
        await cb.answer("❌ Товар не найден.", show_alert=True)
        return

    cur     = product.get(field)
    cur_str = str(cur) if cur is not None else "не задано"
    hint    = _FIELD_HINT.get(field, "")

    await state.set_state(EditState.editing_field)
    await state.update_data(
        editing_product_id=product_id,
        editing_field=field,
        card_msg_id=cb.message.message_id,
    )

    await cb.message.answer(
        f"✏️ <b>{label}</b>\n"
        f"Текущее значение: <code>{cur_str}</code>\n\n"
        f"Введите новое значение{hint}:\n"
        f"<i>(или /cancel для отмены)</i>",
        parse_mode="HTML",
    )
    await cb.answer()


# ── Ввод нового значения ──────────────────────────────────────────────────────

@router.message(EditState.editing_field, F.text, ~F.text.startswith("/"))
async def edit_field_submit(message: Message, state: FSMContext, db: Database):
    data        = await state.get_data()
    product_id  = data["editing_product_id"]
    field       = data["editing_field"]
    card_msg_id = data.get("card_msg_id")
    label       = _FIELDS[field]

    text = message.text.strip()

    # Очистка поля (для строковых nullable-полей)
    nullable = field in ("barcode", "extra_code", "category")
    if nullable and text in ("-", "—", "нет", "null", ""):
        value = None
    else:
        ftype = _FIELD_TYPE[field]
        try:
            if ftype == float:
                value = round(float(text.replace(",", ".")), 4)
                if value < 0:
                    raise ValueError("negative")
            elif ftype == int:
                value = int(text)
                if value < 1:
                    raise ValueError("< 1")
            else:
                value = text
        except ValueError:
            if ftype == float:
                await message.answer("❌ Введите корректное число (например: 125.50)")
            elif ftype == int:
                await message.answer("❌ Введите целое число ≥ 1 (например: 12)")
            else:
                await message.answer("❌ Некорректное значение.")
            return

    await db.update_product_field(product_id, field, value)
    await state.clear()

    display = f"<code>{value}</code>" if value is not None else "<i>очищено</i>"
    await message.answer(f"✅ <b>{label}</b> обновлено → {display}", parse_mode="HTML")

    # Обновить карточку
    if card_msg_id:
        product = await db.get_product_by_id(product_id)
        aliases = await db.get_product_aliases(product_id)
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=card_msg_id,
                text=_card_text(product, aliases),
                reply_markup=_card_kb(product_id, len(aliases)),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Не удалось обновить карточку #%s: %s", card_msg_id, e)
            await _show_card(message, product_id, db)


# ── Управление алиасами ────────────────────────────────────────────────────────

@router.callback_query(EditAliasCb.filter())
async def edit_aliases_list(cb: CallbackQuery, callback_data: EditAliasCb, db: Database):
    product_id = callback_data.product_id
    product    = await db.get_product_by_id(product_id)
    aliases    = await db.get_product_aliases(product_id)

    if not aliases:
        await cb.answer("У этого товара нет алиасов.", show_alert=True)
        return

    text, kb = _alias_text_and_kb(product, aliases)
    await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@router.callback_query(DeleteAliasCb.filter())
async def delete_alias_cb(cb: CallbackQuery, callback_data: DeleteAliasCb, db: Database):
    await db.delete_alias(callback_data.alias_id)
    await cb.answer("✅ Алиас удалён")

    product_id = callback_data.product_id
    product    = await db.get_product_by_id(product_id)
    aliases    = await db.get_product_aliases(product_id)

    if aliases:
        text, kb = _alias_text_and_kb(product, aliases)
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        # Все алиасы удалены — вернуть карточку
        await _show_card(cb, product_id, db)


@router.callback_query(BackToCardCb.filter())
async def back_to_card(cb: CallbackQuery, callback_data: BackToCardCb, db: Database):
    await _show_card(cb, callback_data.product_id, db)


# ── Готово ─────────────────────────────────────────────────────────────────────

@router.callback_query(EditDoneCb.filter())
async def edit_done(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.answer("✅ Изменения сохранены")


# ── Отмена ────────────────────────────────────────────────────────────────────

@router.message(F.text == "/cancel", EditState.searching)
@router.message(F.text == "/cancel", EditState.editing_field)
async def edit_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Режим редактирования отменён.")
