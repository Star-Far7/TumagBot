import asyncio
import logging
import logging.handlers
import os
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject

from config import BOT_TOKEN, DATABASE_PATH, LOG_FILE, LOG_LEVEL
from db.database import Database
from handlers import catalog, edit, inline, invoice, start

# ── Логирование ───────────────────────────────────────────────────────────────
_log_dir = os.path.dirname(LOG_FILE)
if _log_dir:
    os.makedirs(_log_dir, exist_ok=True)

_fmt = logging.Formatter("%(asctime)s  %(name)-28s  %(levelname)-8s  %(message)s")

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
)
_file_handler.setFormatter(_fmt)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_fmt)

_level = getattr(logging, LOG_LEVEL, logging.INFO)
logging.basicConfig(level=_level, handlers=[_file_handler, _console_handler])
# ─────────────────────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)


class DatabaseMiddleware(BaseMiddleware):
    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        data["db"] = self.db
        return await handler(event, data)


async def main():
    db = Database(DATABASE_PATH)
    await db.init()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Внедрить базу данных во все хэндлеры
    dp.update.outer_middleware(DatabaseMiddleware(db))

    # Роутеры — порядок важен: invoice должен идти после catalog
    # (чтобы F.document в catalog.py перехватывал раньше)
    dp.include_router(start.router)
    dp.include_router(catalog.router)
    dp.include_router(edit.router)
    dp.include_router(invoice.router)
    dp.include_router(inline.router)

    logger.info("Бот запущен. Нажмите Ctrl+C для остановки.")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
