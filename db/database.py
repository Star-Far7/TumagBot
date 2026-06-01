import aiosqlite
import logging
from pathlib import Path
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id     TEXT UNIQUE,
    name            TEXT NOT NULL,
    barcode         TEXT,
    extra_code      TEXT,
    category        TEXT,
    unit            TEXT DEFAULT 'шт',
    purchase_price  REAL DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode) WHERE barcode IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_products_name    ON products(name);

CREATE TABLE IF NOT EXISTS supplier_aliases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_name   TEXT NOT NULL COLLATE NOCASE,
    product_id      INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    supplier_barcode TEXT,
    confirmed_by    INTEGER,
    use_count       INTEGER DEFAULT 1,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(supplier_name)
);
CREATE INDEX IF NOT EXISTS idx_aliases_name ON supplier_aliases(supplier_name);

CREATE TABLE IF NOT EXISTS invoice_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    supplier        TEXT,
    status          TEXT DEFAULT 'processing',
    item_count      INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at    TIMESTAMP
);

CREATE TABLE IF NOT EXISTS invoice_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES invoice_sessions(id) ON DELETE CASCADE,
    product_id      INTEGER REFERENCES products(id),
    supplier_name   TEXT NOT NULL,
    supplier_barcode TEXT,
    quantity        REAL NOT NULL DEFAULT 1,
    price           REAL NOT NULL DEFAULT 0,
    old_price       REAL,
    unit            TEXT,
    match_type      TEXT DEFAULT 'manual',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()
        await self._migrate()
        logger.info("Database initialized at %s", self.db_path)

    async def _migrate(self):
        """Применить накопившиеся ALTER TABLE миграции (идемпотентно)."""
        migrations = [
            # v2: размер упаковки поставщика
            "ALTER TABLE products ADD COLUMN pack_size INTEGER DEFAULT 1",
            # v3: дополнительный код (второй штрихкод из Umag)
            "ALTER TABLE products ADD COLUMN extra_code TEXT",
            "CREATE INDEX IF NOT EXISTS idx_products_extra_code ON products(extra_code) WHERE extra_code IS NOT NULL",
            # v4: продажная цена из Umag-каталога
            "ALTER TABLE products ADD COLUMN selling_price REAL DEFAULT 0",
            # v5: нормализация supplier_name к нижнему регистру.
            # SQLite COLLATE NOCASE работает только для ASCII, кириллица не покрыта.
            # Удаляем дубликаты (разный регистр → одна строка), затем lowercase всё.
            """
            DELETE FROM supplier_aliases WHERE id NOT IN (
                SELECT MIN(id) FROM supplier_aliases GROUP BY LOWER(supplier_name)
            )
            """,
            "UPDATE supplier_aliases SET supplier_name = LOWER(supplier_name)",
        ]
        async with aiosqlite.connect(self.db_path) as db:
            for sql in migrations:
                try:
                    await db.execute(sql)
                    await db.commit()
                    logger.info("Migration applied: %s", sql[:60])
                except Exception:
                    pass  # колонка уже существует

    # ── Products ────────────────────────────────────────────────────────────

    async def get_all_products(self) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM products ORDER BY name") as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def get_product_by_id(self, product_id: int) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM products WHERE id = ?", (product_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_product_by_barcode(self, barcode: str) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM products WHERE barcode = ?", (barcode,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def upsert_products(self, products: List[Dict]) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            for p in products:
                await db.execute(
                    """
                    INSERT INTO products
                        (external_id, name, barcode, extra_code, category, unit, purchase_price, selling_price, updated_at)
                    VALUES
                        (:external_id, :name, :barcode, :extra_code, :category, :unit, :purchase_price, :selling_price,
                         CURRENT_TIMESTAMP)
                    ON CONFLICT(external_id) DO UPDATE SET
                        name           = excluded.name,
                        barcode        = COALESCE(excluded.barcode,     products.barcode),
                        extra_code     = COALESCE(excluded.extra_code,  products.extra_code),
                        category       = COALESCE(excluded.category,    products.category),
                        unit           = COALESCE(excluded.unit,        products.unit),
                        purchase_price = CASE WHEN excluded.purchase_price > 0
                                              THEN excluded.purchase_price
                                              ELSE products.purchase_price END,
                        selling_price  = CASE WHEN excluded.selling_price > 0
                                              THEN excluded.selling_price
                                              ELSE products.selling_price END,
                        updated_at     = CURRENT_TIMESTAMP
                    """,
                    p,
                )
            await db.commit()
            return len(products)

    async def update_pack_size(self, product_id: int, pack_size: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE products SET pack_size = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (pack_size, product_id),
            )
            await db.commit()

    async def update_product_price(self, product_id: int, new_price: float):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE products SET purchase_price = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_price, product_id),
            )
            await db.commit()

    async def count_products(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM products") as cur:
                row = await cur.fetchone()
                return row[0]

    # ── Aliases ─────────────────────────────────────────────────────────────

    async def get_alias(self, supplier_name: str) -> Optional[Dict]:
        # LOWER() на обеих сторонах: работает и со старыми mixed-case алиасами,
        # и с новыми lowercase. SQLite NOCASE не справляется с кириллицей.
        key = supplier_name.lower().strip()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT sa.*, p.name AS product_name, p.purchase_price, p.barcode AS product_barcode
                FROM supplier_aliases sa
                JOIN products p ON p.id = sa.product_id
                WHERE LOWER(sa.supplier_name) = ?
                """,
                (key,),
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_alias_by_supplier_barcode(self, supplier_barcode: str) -> Optional[Dict]:
        """
        Запасной поиск алиаса по штрихкоду/коду из накладной поставщика.
        Используется когда имя OCR чуть изменилось, но код стабилен.
        Возвращает алиас с наибольшим use_count (самый часто подтверждённый).
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT sa.*, p.name AS product_name, p.purchase_price, p.barcode AS product_barcode
                FROM supplier_aliases sa
                JOIN products p ON p.id = sa.product_id
                WHERE sa.supplier_barcode = ?
                ORDER BY sa.use_count DESC
                LIMIT 1
                """,
                (supplier_barcode,),
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_all_aliases(self) -> List[Dict]:
        """Все алиасы с данными привязанных товаров — для нечёткого поиска."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT sa.*, p.name AS product_name, p.purchase_price,
                       p.barcode AS product_barcode
                FROM supplier_aliases sa
                JOIN products p ON p.id = sa.product_id
                ORDER BY sa.use_count DESC
                """,
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def save_alias(
        self,
        supplier_name: str,
        product_id: int,
        supplier_barcode: Optional[str],
        confirmed_by: int,
    ):
        # Нормализуем к нижнему регистру: SQLite NOCASE не работает с кириллицей
        key = supplier_name.lower().strip()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO supplier_aliases
                    (supplier_name, product_id, supplier_barcode, confirmed_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(supplier_name) DO UPDATE SET
                    product_id       = excluded.product_id,
                    confirmed_by     = excluded.confirmed_by,
                    use_count        = use_count + 1
                """,
                (key, product_id, supplier_barcode, confirmed_by),
            )
            await db.commit()

    async def get_product_aliases(self, product_id: int) -> List[Dict]:
        """Все алиасы конкретного товара, по убыванию use_count."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM supplier_aliases WHERE product_id = ? ORDER BY use_count DESC",
                (product_id,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def delete_alias(self, alias_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM supplier_aliases WHERE id = ?", (alias_id,))
            await db.commit()

    async def update_product_field(self, product_id: int, field: str, value):
        """Обновить одно поле товара. Список допустимых полей зафиксирован."""
        _ALLOWED = {"name", "barcode", "extra_code", "category", "unit", "purchase_price", "pack_size"}
        if field not in _ALLOWED:
            raise ValueError(f"Нельзя редактировать поле: {field}")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE products SET {field} = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (value, product_id),
            )
            await db.commit()

    async def count_aliases(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM supplier_aliases") as cur:
                row = await cur.fetchone()
                return row[0]

    async def get_top_aliases(self, limit: int = 25) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT sa.supplier_name, p.name AS product_name, sa.use_count
                FROM supplier_aliases sa
                JOIN products p ON p.id = sa.product_id
                ORDER BY sa.use_count DESC, sa.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    # ── Sessions ─────────────────────────────────────────────────────────────

    async def create_session(self, user_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "INSERT INTO invoice_sessions (user_id) VALUES (?)", (user_id,)
            ) as cur:
                await db.commit()
                return cur.lastrowid

    async def complete_session(self, session_id: int, item_count: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE invoice_sessions
                SET status = 'completed', item_count = ?, completed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (item_count, session_id),
            )
            await db.commit()

    async def add_invoice_item(
        self,
        session_id: int,
        product_id: Optional[int],
        supplier_name: str,
        supplier_barcode: Optional[str],
        quantity: float,
        price: float,
        old_price: Optional[float],
        unit: str,
        match_type: str,
    ):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO invoice_items
                    (session_id, product_id, supplier_name, supplier_barcode,
                     quantity, price, old_price, unit, match_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, product_id, supplier_name, supplier_barcode,
                    quantity, price, old_price, unit, match_type,
                ),
            )
            await db.commit()

    async def get_session_items(self, session_id: int) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT ii.*,
                       p.name          AS product_name,
                       p.category      AS category,
                       p.barcode       AS product_barcode,
                       p.selling_price AS selling_price
                FROM invoice_items ii
                LEFT JOIN products p ON p.id = ii.product_id
                WHERE ii.session_id = ?
                ORDER BY ii.id
                """,
                (session_id,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
