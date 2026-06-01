"""
Управление каталогом товаров: загрузка из Excel/CSV, просмотр, псевдонимы.
"""
import io
import logging
import os
import tempfile
from typing import Dict, List, Optional

import openpyxl
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Document, Message

from config import ALLOWED_USERS
from db.database import Database

logger = logging.getLogger(__name__)
router = Router()


def _allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


# ── Команды ───────────────────────────────────────────────────────────────────

@router.message(Command("catalog"))
async def cmd_catalog(message: Message, db: Database):
    if not _allowed(message.from_user.id):
        return

    products = await db.count_products()
    aliases  = await db.count_aliases()

    await message.answer(
        f"📦 <b>Каталог товаров</b>\n\n"
        f"Товаров в базе: <b>{products:,}</b>\n"
        f"Псевдонимов поставщиков: <b>{aliases:,}</b>\n\n"
        f"/upload — загрузить/обновить каталог\n"
        f"/aliases — посмотреть выученные псевдонимы",
        parse_mode="HTML",
    )


@router.message(Command("upload"))
async def cmd_upload(message: Message):
    if not _allowed(message.from_user.id):
        return

    await message.answer(
        "📤 <b>Загрузка каталога товаров</b>\n\n"
        "Отправьте файл выгрузки из Umag:\n"
        "• <b>Excel</b> (.xlsx) — предпочтительно\n"
        "• <b>CSV</b> (.csv) — кодировка UTF-8 или CP1251\n\n"
        "Файл должен содержать хотя бы колонку <b>Наименование</b>.\n"
        "Дополнительно распознаются: Штрихкод, <b>Дополнительный код</b>, Категория, Ед.изм., Цена закупа, ID/Артикул.",
        parse_mode="HTML",
    )


@router.message(Command("aliases"))
async def cmd_aliases(message: Message, db: Database):
    if not _allowed(message.from_user.id):
        return

    aliases = await db.get_top_aliases(limit=25)

    if not aliases:
        await message.answer(
            "📭 Псевдонимов пока нет.\n"
            "Они появятся после первого ручного сопоставления товаров."
        )
        return

    lines = ["📚 <b>Псевдонимы поставщиков (топ-25 по частоте):</b>\n"]
    for a in aliases:
        sup  = a["supplier_name"][:38]
        prod = a["product_name"][:38]
        lines.append(f"• <i>{sup}</i>\n  → {prod}  (×{a['use_count']})")

    await message.answer("\n".join(lines), parse_mode="HTML")


# ── Приём файла ───────────────────────────────────────────────────────────────

@router.message(F.document)
async def handle_document(message: Message, db: Database):
    if not _allowed(message.from_user.id):
        return

    doc: Document = message.document
    fname = (doc.file_name or "").lower()

    if not (fname.endswith(".xlsx") or fname.endswith(".csv")):
        # Не файл каталога — игнорируем (возможно, другой тип документа)
        return

    status = await message.answer("⏳ Обрабатываю каталог…")

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await message.bot.download(doc, destination=tmp_path)

        with open(tmp_path, "rb") as f:
            file_bytes = f.read()

        if fname.endswith(".xlsx"):
            products = _parse_excel(file_bytes)
        else:
            products = _parse_csv(file_bytes)

        if not products:
            await status.edit_text(
                "❌ Не удалось извлечь товары.\n"
                "Убедитесь, что файл содержит колонку «Наименование»."
            )
            return

        count = await db.upsert_products(products)
        total = await db.count_products()

        await status.edit_text(
            f"✅ <b>Каталог обновлён!</b>\n\n"
            f"Строк обработано: <b>{count:,}</b>\n"
            f"Товаров в базе: <b>{total:,}</b>",
            parse_mode="HTML",
        )

    except Exception as exc:
        logger.exception("Ошибка импорта каталога")
        await status.edit_text(f"❌ Ошибка при обработке файла:\n{str(exc)[:300]}")

    finally:
        os.unlink(tmp_path)


# ── Парсеры ───────────────────────────────────────────────────────────────────

def _parse_excel(data: bytes) -> List[Dict]:
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active

    products: List[Dict] = []
    headers: Optional[List[str]] = None

    for row in ws.iter_rows(values_only=True):
        if not any(row):
            continue

        if headers is None:
            headers = [str(h).lower().strip() if h is not None else "" for h in row]
            continue

        row_dict = dict(zip(headers, row))
        product  = _extract_product(row_dict)
        if product:
            products.append(product)

    wb.close()
    return products


def _parse_csv(data: bytes) -> List[Dict]:
    import csv

    for enc in ("utf-8-sig", "cp1251", "utf-8"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("Не удалось определить кодировку CSV-файла.")

    delimiter = ";" if text.count(";") > text.count(",") else ","
    reader    = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    products: List[Dict] = []

    for row in reader:
        row_lower = {k.lower().strip(): v for k, v in row.items()}
        product   = _extract_product(row_lower)
        if product:
            products.append(product)

    return products


def _extract_product(row: Dict) -> Optional[Dict]:
    name = _col(row, [
        "наименование", "наименование товара",
        "название товара", "название",
        "товар", "товары",
        "name", "product", "item",
    ])
    if not name:
        return None

    ext_id = _col(row, ["id", "код", "внешний код", "артикул", "external_id", "код товара"])
    if not ext_id:
        ext_id = f"name_{name[:60]}"

    return {
        "external_id":    ext_id,
        "name":           name,
        "barcode":        _col(row, ["штрихкод", "штрих-код", "штрихкод товара",
                                      "barcode", "ean", "ean13", "ean-13", "код товара (ean)"]),
        "extra_code":     _col(row, ["дополнительный код", "доп. код", "доп.код", "допкод",
                                      "дополнительный штрихкод", "доп. штрихкод", "доп штрихкод",
                                      "extra_code", "additional_code", "alt_barcode", "код2"]),
        "category":       _col(row, ["категория", "группа товаров", "группа",
                                      "category", "group", "раздел"]),
        "unit":           _col(row, ["ед.изм.", "ед.изм", "единица измерения", "единица",
                                      "ед.", "ед измерения", "unit"]) or "шт",
        "purchase_price": _float(row, ["цена закупа", "закупочная цена", "цена прихода",
                                        "цена (закупочная)", "себестоимость",
                                        "purchase_price", "cost"]),
        "selling_price":  _float(row, ["цена продажи", "продажная цена", "розничная цена",
                                        "цена реализации", "цена (реализации)", "цена (продажи)",
                                        "цена продаж", "отпускная цена", "цена",
                                        "retail_price", "selling_price", "price"]),
    }


def _col(row: Dict, keys: List[str]) -> Optional[str]:
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip() and str(v).strip().lower() != "none":
            return str(v).strip()
    return None


def _float(row: Dict, keys: List[str]) -> float:
    for k in keys:
        v = row.get(k)
        if v is not None:
            try:
                return float(str(v).replace(",", ".").replace(" ", "").replace("\xa0", ""))
            except (ValueError, TypeError):
                pass
    return 0.0
