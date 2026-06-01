import html
import os

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from config import ALLOWED_USERS, LOG_FILE
from db.database import Database

router = Router()


def _allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


@router.message(CommandStart())
async def cmd_start(message: Message):
    if not _allowed(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этому боту.")
        return

    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "Я помогаю обрабатывать бумажные накладные поставщиков и готовлю "
        "Excel-файл для загрузки в <b>Umag</b>.\n\n"

        "<b>━━ Быстрый старт ━━</b>\n"
        "1️⃣ Загрузите каталог товаров из Umag → /upload\n"
        "2️⃣ Сфотографируйте накладную и отправьте 📷\n"
        "3️⃣ Подтвердите сопоставление товаров\n"
        "4️⃣ Скачайте готовый Excel 📥\n\n"

        "<b>━━ Возможности ━━</b>\n"
        "• 🤖 Распознавание накладных через Gemini AI\n"
        "• 📄 Многостраничные накладные (несколько фото)\n"
        "• 🔄 Автосопоставление по штрихкоду и псевдонимам\n"
        "• ⚖️ Поддержка весовых товаров (кг / поштучно)\n"
        "• 📦 Учёт коробочной упаковки (кор./уп./ящик)\n"
        "• ➕ Добавление новых товаров в Excel без базы\n"
        "• 📷 Сканирование штрихкода через камеру\n"
        "• 🎨 Цветовая подсветка изменений цены\n\n"

        "<b>━━ Excel на выходе ━━</b>\n"
        "• Лист <b>«Для Umag»</b> — чистые данные для импорта\n"
        "• Лист <b>«Детали»</b>  — полная информация с цветами\n"
        "• Лист <b>«Изменения цен»</b> — только при наличии\n\n"

        "<b>━━ Команды ━━</b>\n"
        "/upload — загрузить каталог (Excel/CSV из Umag)\n"
        "/catalog — статистика каталога\n"
        "/aliases — выученные псевдонимы поставщиков\n"
        "/logs — последние строки лога (для отладки)\n"
        "/cancel — отменить текущую обработку\n"
        "/help — подробная инструкция",
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not _allowed(message.from_user.id):
        return

    await message.answer(
        "<b>📖 Подробная инструкция</b>\n\n"

        "<b>Шаг 1 — Каталог товаров</b>\n"
        "• Выгрузите товары из Umag в Excel (.xlsx) или CSV\n"
        "• Отправьте файл боту (команда /upload)\n"
        "• Бот распознает колонки автоматически\n\n"

        "<b>Шаг 2 — Обработка накладной</b>\n"
        "• Сфотографируйте накладную и отправьте фото\n"
        "• Gemini AI извлекает все товарные позиции\n"
        "• Для многостраничных — отправляйте по одной фото,\n"
        "  затем нажмите «Перейти к проверке»\n\n"

        "<b>Шаг 3 — Проверка позиций</b>\n"
        "• Известные товары сопоставятся автоматически\n"
        "• Для новых — бот предложит варианты из каталога\n"
        "• Ваш выбор сохраняется как псевдоним навсегда\n"
        "• ⚖️ Весовые товары: выбор кг или поштучно\n"
        "• 📦 Коробочная поставка: выбор штук в упаковке\n"
        "• ➕ Нет в базе — добавить в Excel с фото штрихкода\n\n"

        "<b>Шаг 4 — Скачать Excel</b>\n"
        "• <b>«Для Umag»</b> — готов для импорта (без заголовков)\n"
        "• <b>«Детали»</b> — полная таблица с подсветкой\n"
        "  🔴 Красный — цена выросла\n"
        "  🟢 Зелёный — цена снизилась\n"
        "  ⬜ Серый  — позиция пропущена\n\n"

        "<b>Инлайн-поиск товаров</b>\n"
        "Введите <code>@имя_бота текст</code> в любом чате\n"
        "для быстрого поиска по каталогу.",
        parse_mode="HTML",
    )


@router.message(Command("logs"))
async def cmd_logs(message: Message):
    """Последние 80 строк лог-файла прямо в Telegram."""
    if not _allowed(message.from_user.id):
        return

    if not os.path.exists(LOG_FILE):
        await message.answer("📭 Лог-файл ещё не создан.")
        return

    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        await message.answer(f"❌ Не удалось прочитать лог: {e}")
        return

    tail = "".join(lines[-80:])
    # Telegram: max 4096 символов в одном сообщении — берём хвост
    if len(tail) > 3900:
        tail = "…\n" + tail[-3900:]

    await message.answer(
        f"<pre>{html.escape(tail)}</pre>",
        parse_mode="HTML",
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database):
    if not _allowed(message.from_user.id):
        return

    products = await db.count_products()
    aliases = await db.count_aliases()

    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"Товаров в каталоге: <b>{products:,}</b>\n"
        f"Выученных псевдонимов: <b>{aliases:,}</b>",
        parse_mode="HTML",
    )
