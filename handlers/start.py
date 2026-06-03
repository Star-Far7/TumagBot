import html
import os
import shutil

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile, Message

from config import ALLOWED_USERS, DATABASE_PATH, LOG_FILE
from db.database import Database
from keyboards.kb import (
    BTN_ALIASES,
    BTN_CANCEL,
    BTN_CATALOG,
    BTN_HELP,
    BTN_STATS,
    BTN_UPLOAD,
    main_menu_keyboard,
)

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
        "/help — подробная инструкция\n\n"
        "💡 <i>Меню снизу — быстрый доступ к командам.</i>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
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


@router.message(Command("dbbackup"))
async def cmd_dbbackup(message: Message):
    """Скачать файл базы данных через Telegram."""
    if not _allowed(message.from_user.id):
        return

    if not os.path.exists(DATABASE_PATH):
        await message.answer("❌ Файл базы данных не найден.")
        return

    try:
        await message.answer_document(
            FSInputFile(DATABASE_PATH, filename="umag_bot.db"),
            caption="📦 Резервная копия базы данных",
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.message(Command("dbrestore"))
async def cmd_dbrestore_hint(message: Message):
    """Подсказка — как восстановить базу."""
    if not _allowed(message.from_user.id):
        return
    await message.answer(
        "📦 <b>Восстановление базы</b>\n\n"
        "Отправьте файл <code>umag_bot.db</code> "
        "(тот что скачали через /dbbackup) как документ.\n\n"
        "⚠️ Текущая база будет заменена!",
        parse_mode="HTML",
    )


@router.message(F.document.file_name == "umag_bot.db")
async def on_db_restore(message: Message, db: Database):
    """Принять файл umag_bot.db и заменить текущую базу."""
    if not _allowed(message.from_user.id):
        return

    status = await message.answer("⏳ Загружаю базу…")

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await message.bot.download(message.document, destination=tmp_path)

        # Проверить что файл — валидный SQLite
        import aiosqlite
        async with aiosqlite.connect(tmp_path) as check_db:
            async with check_db.execute("SELECT COUNT(*) FROM products") as cur:
                count = (await cur.fetchone())[0]

        # Заменить текущую базу
        backup_path = DATABASE_PATH + ".bak"
        if os.path.exists(DATABASE_PATH):
            shutil.copy2(DATABASE_PATH, backup_path)

        shutil.copy2(tmp_path, DATABASE_PATH)

        # Переинициализировать (применить миграции если нужно)
        await db.init()

        await status.edit_text(
            f"✅ <b>База восстановлена!</b>\n\n"
            f"Товаров: <b>{count}</b>\n"
            f"Резервная копия старой базы: <code>{backup_path}</code>\n\n"
            "Перезапустите бота для полной загрузки.",
            parse_mode="HTML",
        )
    except Exception as e:
        await status.edit_text(f"❌ Ошибка: файл повреждён или не является базой бота.\n<code>{e}</code>", parse_mode="HTML")
    finally:
        os.unlink(tmp_path)


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


# ── Обработчики reply-кнопок главного меню ──────────────────────────────────
# Каждая кнопка делегирует работу соответствующей команде.

@router.message(F.text == BTN_UPLOAD)
async def btn_upload(message: Message):
    if not _allowed(message.from_user.id):
        return
    # Локальный импорт чтобы избежать циклической зависимости
    from handlers.catalog import cmd_upload
    await cmd_upload(message)


@router.message(F.text == BTN_CATALOG)
async def btn_catalog(message: Message, db: Database):
    if not _allowed(message.from_user.id):
        return
    from handlers.catalog import cmd_catalog
    await cmd_catalog(message, db)


@router.message(F.text == BTN_ALIASES)
async def btn_aliases(message: Message, db: Database):
    if not _allowed(message.from_user.id):
        return
    from handlers.catalog import cmd_aliases
    await cmd_aliases(message, db)


@router.message(F.text == BTN_STATS)
async def btn_stats(message: Message, db: Database):
    if not _allowed(message.from_user.id):
        return
    await cmd_stats(message, db)


@router.message(F.text == BTN_CANCEL)
async def btn_cancel(message: Message, state: FSMContext):
    if not _allowed(message.from_user.id):
        return
    from handlers.invoice import cmd_cancel
    await cmd_cancel(message, state)


@router.message(F.text == BTN_HELP)
async def btn_help(message: Message):
    if not _allowed(message.from_user.id):
        return
    await cmd_help(message)
