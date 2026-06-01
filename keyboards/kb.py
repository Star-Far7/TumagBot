from typing import Dict, List, Optional

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


# ── Callback data schemas ─────────────────────────────────────────────────────

class MatchCb(CallbackData, prefix="match"):
    item_idx: int
    product_id: int

class SkipCb(CallbackData, prefix="skip"):
    item_idx: int

class SearchCb(CallbackData, prefix="search"):
    item_idx: int

class SearchCancelCb(CallbackData, prefix="search_cancel"):
    item_idx: int

class ExportCb(CallbackData, prefix="export"):
    session_id: int

class ConfirmAutoCb(CallbackData, prefix="confirm_auto"):
    """Пользователь подтвердил весь список авто-сопоставлений."""
    session_id: int

class StartReviewCb(CallbackData, prefix="startreview"):
    """Пользователь завершил добавление страниц → перейти к проверке."""
    session_id: int

class PackSizeCb(CallbackData, prefix="pack"):
    item_idx:  int
    pack_size: int   # 1 = цена верная как есть; >1 = количество в упаковке

class WeightModeCb(CallbackData, prefix="weight"):
    item_idx: int
    mode:     str   # "kg" | "pieces"

class AddNewCb(CallbackData, prefix="addnew"):
    """Добавить товар в Excel без сопоставления с базой."""
    item_idx: int

class SkipBarcodeCb(CallbackData, prefix="skipbc"):
    """Пропустить сканирование штрихкода, добавить без него."""
    item_idx: int

class UmagUploadCb(CallbackData, prefix="umag"):
    """Загрузить приёмку напрямую в Umag."""
    session_id: int

class EditQtyCb(CallbackData, prefix="editqty"):
    """Исправить количество или цену позиции."""
    item_idx: int


# ── Keyboards ─────────────────────────────────────────────────────────────────

def item_keyboard(
    item_idx: int,
    suggestions: List[Dict],
    auto_match: Optional[Dict] = None,
) -> InlineKeyboardMarkup:
    """
    Клавиатура для одной позиции накладной.
    auto_match — если задан, показываем "Да / Другой / Пропустить".
    Иначе показываем список вариантов.
    """
    btns: list[list[InlineKeyboardButton]] = []

    if auto_match:
        prod_name = auto_match["product"]["name"]
        score_pct = int(auto_match["score"] * 100)
        btns.append([
            InlineKeyboardButton(
                text=f"✅ {prod_name[:42]} ({score_pct}%)",
                callback_data=MatchCb(
                    item_idx=item_idx,
                    product_id=auto_match["product"]["id"],
                ).pack(),
            )
        ])
        btns.append([
            InlineKeyboardButton(
                text="🔄 Другой товар",
                callback_data=SearchCb(item_idx=item_idx).pack(),
            ),
            InlineKeyboardButton(
                text="⏭ Пропустить",
                callback_data=SkipCb(item_idx=item_idx).pack(),
            ),
        ])
    else:
        for s in suggestions:
            prod = s["product"]
            score_pct = int(s["score"] * 100)
            btns.append([
                InlineKeyboardButton(
                    text=f"[{score_pct}%] {prod['name'][:44]}",
                    callback_data=MatchCb(
                        item_idx=item_idx,
                        product_id=prod["id"],
                    ).pack(),
                )
            ])
        btns.append([
            InlineKeyboardButton(
                text="🔍 Найти вручную",
                callback_data=SearchCb(item_idx=item_idx).pack(),
            ),
            InlineKeyboardButton(
                text="⏭ Пропустить",
                callback_data=SkipCb(item_idx=item_idx).pack(),
            ),
        ])

    # Изменить количество/цену + добавить в Excel
    btns.append([
        InlineKeyboardButton(
            text="✏️ Изменить кол-во/цену",
            callback_data=EditQtyCb(item_idx=item_idx).pack(),
        ),
    ])
    btns.append([
        InlineKeyboardButton(
            text="➕ Добавить в Excel",
            callback_data=AddNewCb(item_idx=item_idx).pack(),
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=btns)


def search_cancel_keyboard(item_idx: int) -> InlineKeyboardMarkup:
    """Клавиатура ручного поиска: вернуться к товару / пропустить / добавить в Excel."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="↩️ Вернуться к товару",
            callback_data=SearchCancelCb(item_idx=item_idx).pack(),
        )],
        [
            InlineKeyboardButton(
                text="⏭ Пропустить",
                callback_data=SkipCb(item_idx=item_idx).pack(),
            ),
            InlineKeyboardButton(
                text="➕ Добавить в Excel",
                callback_data=AddNewCb(item_idx=item_idx).pack(),
            ),
        ],
    ])


def pack_size_keyboard(
    item_idx: int,
    suggested_packs: List[int],
    invoice_price: float,
    known_price: float,
) -> InlineKeyboardMarkup:
    """Клавиатура выбора размера упаковки при аномальной цене."""
    btns: list[list[InlineKeyboardButton]] = []

    for pack in suggested_packs:
        unit_p = invoice_price / pack
        btns.append([
            InlineKeyboardButton(
                text=f"📦 {pack} шт/уп → {unit_p:.2f}/шт",
                callback_data=PackSizeCb(item_idx=item_idx, pack_size=pack).pack(),
            )
        ])

    btns.append([
        InlineKeyboardButton(
            text=f"✅ Цена верная ({invoice_price:.2f}/шт)",
            callback_data=PackSizeCb(item_idx=item_idx, pack_size=1).pack(),
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=btns)


def collecting_keyboard(session_id: int, total_items: int, page_count: int) -> InlineKeyboardMarkup:
    """Клавиатура после обработки страницы: перейти к проверке или добавить ещё."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"✅ Перейти к проверке  ({total_items} поз. / {page_count} стр.)",
            callback_data=StartReviewCb(session_id=session_id).pack(),
        )
    ]])


def barcode_skip_keyboard(item_idx: int) -> InlineKeyboardMarkup:
    """Клавиатура под запросом штрихкода — пропустить сканирование."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="⏩ Добавить без штрихкода",
            callback_data=SkipBarcodeCb(item_idx=item_idx).pack(),
        )
    ]])


def container_pack_keyboard(item_idx: int, invoice_price: float) -> InlineKeyboardMarkup:
    """Клавиатура выбора количества штук в коробке/упаковке (без эталонной цены)."""
    btns = []
    for pack in [2, 4, 6, 10, 12, 24]:
        unit_p = invoice_price / pack
        btns.append([InlineKeyboardButton(
            text=f"📦 {pack} шт/уп → {unit_p:.2f}/шт",
            callback_data=PackSizeCb(item_idx=item_idx, pack_size=pack).pack(),
        )])
    btns.append([InlineKeyboardButton(
        text=f"✅ Это цена за штуку ({invoice_price:.2f}/шт)",
        callback_data=PackSizeCb(item_idx=item_idx, pack_size=1).pack(),
    )])
    return InlineKeyboardMarkup(inline_keyboard=btns)


def weight_mode_keyboard(item_idx: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора единицы учёта весового товара."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="⚖️ По весу (кг)",
            callback_data=WeightModeCb(item_idx=item_idx, mode="kg").pack(),
        ),
        InlineKeyboardButton(
            text="📦 Поштучно",
            callback_data=WeightModeCb(item_idx=item_idx, mode="pieces").pack(),
        ),
    ]])


def export_keyboard(session_id: int, umag_enabled: bool = False) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(
            text="📥 Скачать Excel",
            callback_data=ExportCb(session_id=session_id).pack(),
        )
    ]]
    if umag_enabled:
        rows.append([
            InlineKeyboardButton(
                text="📤 Загрузить в Umag",
                callback_data=UmagUploadCb(session_id=session_id).pack(),
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
