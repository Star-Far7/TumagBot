# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Telegram bot that processes paper supplier invoices for Kazakh/CIS retail stores using Gemini AI OCR, matches recognized items to a product catalog, and generates Excel files for import into the Umag POS system. All UI text and comments are in Russian.

## Running the bot

```bash
pip install -r requirements.txt
python main.py
```

Requires `.env` with `BOT_TOKEN` and `GEMINI_API_KEY` (see `.env.example`). The bot uses long-polling (no webhook).

## Architecture

### Core flow

```
Photo → Gemini OCR → item list → matching pipeline → user review (FSM) → Excel export
```

1. **`services/gemini_ocr.py`** — Sends invoice photo to Gemini with a large Russian-language prompt (`_PROMPT`). Returns JSON array of items. Contains extensive post-processing rules in Python that fix common Gemini errors (wrong quantity multiplication, A*B*C packaging format, qty-in-name patterns). The prompt and Python post-processing are tightly coupled — changes to one often require changes to the other.

2. **`services/product_matcher.py`** (`ProductMatcher`) — Multi-level fuzzy search: exact barcode → extra_code (semicolon-separated) → article code → text scoring (token_sort + token_set + partial_ratio) with numeric penalty (-0.28 per unmatched % / volume / weight marker) and lead-token bonus.

3. **`handlers/invoice.py`** — The main FSM handler (~1100 lines). States: `collecting` → `reviewing` → `searching` / `weight_input` / `barcode_photo`. Contains `_find_best_alias()` for fuzzy alias matching (also with numeric penalty). Router order in `main.py` matters: catalog must come before invoice so `F.document` is caught first.

4. **`services/excel_exporter.py`** — Generates 3-sheet workbook: "Для Umag" (headerless raw data for import), "Детали" (formatted with color coding), "Изменения цен" (price changes only).

### Matching pipeline priority (in `handle_photo`)

```
For each OCR item:
  1. Fuzzy alias search (≥80% with numeric penalty) — checks name + supplier barcode
  2. Exact barcode match in products table
  3. Extra_code / article match
  4. Fuzzy text match against product names (auto ≥82%, suggest ≥40%)
```

### Database

SQLite via aiosqlite. Schema in `db/database.py` SCHEMA constant. Migrations are idempotent ALTER TABLE statements in `_migrate()` — add new migrations at the end of the list. Key tables:

- **products** — catalog from Umag. `external_id` is the unique key for upsert. `extra_code` can contain multiple codes separated by `;`.
- **supplier_aliases** — learned mappings from supplier item names to products. Stored lowercase (SQLite NOCASE doesn't work for Cyrillic). Searched with `LOWER()` on both sides.
- **invoice_sessions / invoice_items** — processing history.

### Alias system

Aliases are saved on every user confirmation (including auto-matches). The `supplier_barcode` field enables fallback lookup when OCR produces slightly different item names across scans. Fuzzy alias matching uses `rapidfuzz.fuzz.token_sort_ratio` minus a numeric penalty (symmetric_difference of all numbers extracted from both strings × 0.28).

### OCR post-processing rules (gemini_ocr.py)

These Python corrections run after Gemini returns data, to fix recurring errors:

- **Undo weight×count** — `90г*12` in name: if Gemini multiplied qty by the number after `*`, undo it
- **A*B*C gr format** — `24*20*18 gr`: extract boxes from Gemini's qty, recalculate as `boxes × B`, set `pack_size = B`
- **Qty in name** — `100шт` in name with small column qty: multiply `qty × name_qty`
- **Price recovery** — if unit_price=0 but total>0, compute from total/qty

Each rule has a matching instruction in the `_PROMPT` string AND a Python fallback. When adding new OCR rules, update both.

## Key conventions

- All handler functions receive `db: Database` via `DatabaseMiddleware` (injected in `main.py`)
- FSM state data keys: `session_id`, `items` (list), `results` (dict keyed by index), `review_queue` (list of indices), `review_pos` (current position)
- Callbacks use aiogram `CallbackData` classes defined in `keyboards/kb.py`
- Logging: RotatingFileHandler to `logs/bot.log` (5MB × 3 files). OCR results and match decisions are logged at INFO level for debugging
- Bot commands `/dbbackup` and `/dbrestore` allow transferring the SQLite database via Telegram messages (send/receive the .db file)

## Deployment

Docker-based: `docker compose up -d --build`. Volumes mount `./data` and `./logs` to persist the database and logs outside the container. Also deployable on Railway with a Volume mounted at `/app/data`.

## Language note

The entire codebase — variable names, comments, log messages, user-facing text, Gemini prompts — is in Russian. When modifying prompts or user messages, maintain Russian language.
