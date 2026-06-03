"""
Удалить дубликаты в products, появившиеся из-за смены формата external_id.

Алгоритм:
  1. Найти все пары: старая запись (external_id LIKE 'name_%') и новая (external_id LIKE 'bc_%')
     с одинаковым штрихкодом.
  2. Перенести supplier_aliases со старого product_id на новый.
  3. Удалить старую запись.

Также чистит чисто старые "name_X" дубликаты — даже если у них нет пары "bc_X",
но в базе есть другая запись с тем же штрихкодом.
"""
import sqlite3

DB = "data/umag_bot.db"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 1. Найти все старые записи (с external_id="name_...") у которых есть штрихкод
old_rows = conn.execute("""
    SELECT id, external_id, name, barcode
    FROM products
    WHERE external_id LIKE 'name_%' AND barcode IS NOT NULL AND barcode != ''
""").fetchall()

print(f"Старых записей 'name_*' с штрихкодом: {len(old_rows)}")

migrated_aliases = 0
deleted = 0
no_dup = 0

for old in old_rows:
    # Найти "новую" запись с тем же штрихкодом, но другим id
    new = conn.execute("""
        SELECT id, external_id FROM products
        WHERE barcode = ? AND id != ?
        ORDER BY (external_id LIKE 'bc_%') DESC
        LIMIT 1
    """, (old["barcode"], old["id"])).fetchone()

    if not new:
        no_dup += 1
        continue   # дубля нет — оставляем

    # Перенести алиасы старого id → новый id
    cur = conn.execute(
        "UPDATE OR IGNORE supplier_aliases SET product_id = ? WHERE product_id = ?",
        (new["id"], old["id"])
    )
    migrated_aliases += cur.rowcount

    # Алиасы, которые UPDATE OR IGNORE не смог перенести (уже есть на новом) — удалить
    conn.execute("DELETE FROM supplier_aliases WHERE product_id = ?", (old["id"],))

    # Удалить старую запись
    conn.execute("DELETE FROM products WHERE id = ?", (old["id"],))
    deleted += 1

conn.commit()

# Итоговая статистика
total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
aliases = conn.execute("SELECT COUNT(*) FROM supplier_aliases").fetchone()[0]

print()
print(f"Удалено старых записей-дубликатов: {deleted}")
print(f"Перенесено алиасов: {migrated_aliases}")
print(f"Уникальных 'name_*' (без пары) осталось: {no_dup}")
print()
print(f"Итого товаров в базе: {total}")
print(f"Итого алиасов в базе: {aliases}")

conn.close()
