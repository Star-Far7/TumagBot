import asyncio
import json
import logging
import re
from pathlib import Path
from typing import List, Dict, Optional

import google.generativeai as genai
from google.api_core import exceptions as google_exceptions

from config import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger(__name__)
genai.configure(api_key=GEMINI_API_KEY)

_PROMPT = """Ты — система распознавания товарных накладных стран СНГ.
Проанализируй изображение и извлеки ВСЕ товарные строки.

Верни ТОЛЬКО JSON-массив (без markdown, без пояснений):
[
  {
    "name": "точное название из накладной, включая бренд/объём/жирность/вес",
    "barcode": "числовой код из колонок «Штрихкод»/«ШК»/«EAN»/«Номенклатурный номер» или null",
    "article": "буквенно-цифровой артикул из колонки «Артикул»/«Код»/«Внутр.код» или null",
    "quantity": 60,
    "pack_size": 12,
    "unit_price": 317.17,
    "total_price": 19030.20,
    "unit": "шт",
    "is_weight": false
  }
]

═══ ПРАВИЛО №0 — КОЛОНКА ИТОГОВОЙ ЦЕНЫ (КРИТИЧЕСКИ ВАЖНО) ═══
В накладной может быть несколько ценовых колонок. ВСЕГДА используй:
  ┌─────────────────────────────────────────────────────────────────────┐
  │  total_price = «ВСЕГО СТОИМОСТЬ РЕАЛИЗАЦИИ» / «ИТОГО С НДС»        │
  │              / «СУММА С НДС» / «Сумма с НДС, KZT»                 │
  │               (итог строки С учётом НДС)                           │
  │                                                                     │
  │  ✅ «Цена за ед. с НДС» / «Цена с НДС» → unit_price (читай прямо) │
  │  ❌ «СУММА НДС» / «Сумма НДС, KZT» — это ТОЛЬКО сумма налога,     │
  │      НЕ итоговая стоимость строки — НЕ ИСПОЛЬЗОВАТЬ                │
  │  Если нет колонки с НДС → «СТОИМОСТЬ ТОВАРОВ БЕЗ НДС»             │
  └─────────────────────────────────────────────────────────────────────┘

═══ ПРАВИЛО №1 — КОЛИЧЕСТВО: НЕСКОЛЬКО КОЛОНОК (КРИТИЧЕСКИ ВАЖНО) ═══
Накладные дистрибьюторов (Алиди и др.) содержат несколько колонок количества.
Их значения РАЗНЫЕ — важно выбрать правильную:

  ┌─────────────────────────────────────────────────────────────────────┐
  │  «Кол-во в упаковке/блоке»        → pack_size (штук в упаковке)   │
  │  «Кол-во в коробке/ящике, штук»   → сколько штук В ОДНОЙ коробке  │
  │                                      (НЕ является итоговым кол-вом)│
  │  «Кол-во коробок/блоков отпущено» → число коробок                  │
  │  Отдельная колонка «ШТУК» / «шт.» → ✅ ИТОГОВОЕ кол-во → quantity │
  └─────────────────────────────────────────────────────────────────────┘

  Проверка: quantity × unit_price ≈ total_price (отклонение < 1%).
  Если не сходится — пересмотри выбор колонки количества.

  Пример (Алиди): кол-во в уп.=5 | в коробке=240 | коробок=5 | штук=5
    → quantity=5 (берём «штук»), НЕ 240 и НЕ 5×240=1200

═══ ПРАВИЛО №1б — ФОРМАТ «N × M» В КОЛОНКЕ КОЛИЧЕСТВА ═══
Некоторые накладные пишут количество в одной ячейке как «5 X 12» или «2 x 6».
Правило применяется ТОЛЬКО к содержимому колонки количества, НЕ к названию товара.
  N = количество упаковок/коробок,  M = штук в одной упаковке
  ┌─────────────────────────────────────────────────────────────────────┐
  │  quantity  = N × M   (итоговое число штук)                         │
  │  pack_size = M                                                      │
  │  unit_price = total_price ÷ quantity                                │
  └─────────────────────────────────────────────────────────────────────┘
  Пример: КОЛ-ВО «1 X 12», ВСЕГО = 3806,06
    → quantity=12, pack_size=12, unit_price=317.17
  Пример: КОЛ-ВО «5 X 12», ВСЕГО = 23392,20
    → quantity=60, pack_size=12, unit_price=389.87

  ⛔ ИСКЛЮЧЕНИЕ — «Xг*N», «Xмл*N», «Xл*N» В НАЗВАНИИ (НЕ в колонке):
  Если «90г*12», «38г*24», «1.5л*6» и т.п. написаны в НАЗВАНИИ товара —
  это формат упаковки (вес × штук в блоке), а НЕ запись количества.
  Количество из колонки К-во брать НАПРЯМУЮ, БЕЗ умножения.
  pack_size = число после «*» (для справки), unit_price = цена из колонки.
  ┌─────────────────────────────────────────────────────────────────────┐
  │  Название «Лапша 90г*12», К-во=14, Цена=375, Итого=5250            │
  │  ✅ ВЕРНО:  quantity=14, pack_size=12, unit_price=375               │
  │  ❌ ОШИБКА: quantity=168 (14×12), unit_price=31.25  ← НЕ ДЕЛАТЬ    │
  └─────────────────────────────────────────────────────────────────────┘

═══ ПРАВИЛО №1в — КОЛИЧЕСТВО В НАЗВАНИИ ТОВАРА ═══
Если само название товара содержит количество штук («100шт», «48шт», «24шт», «12шт» и т.п.)
и в колонке количества стоит небольшое число (обычно 1–12) — это число упаковок, а не штук:
  ┌─────────────────────────────────────────────────────────────────────┐
  │  name_qty  = число из названия (напр. 100 из «Фрешбум кола 100шт») │
  │  pack_size = name_qty                                               │
  │  quantity  = кол-во_в_колонке × name_qty    ← итоговое число штук  │
  │  unit_price = total_price ÷ quantity                                │
  └─────────────────────────────────────────────────────────────────────┘
  Пример: «Фрешбум (кола 100шт)», кол-во колонки=1, итого=585
    → pack_size=100, quantity=100, unit_price=5.85
  Пример: «Конфеты Ассорти 48шт», кол-во колонки=2, итого=2160
    → pack_size=48, quantity=96, unit_price=22.50

═══ ПРАВИЛО №1г — ФОРМАТ «A*B*C gr» В НАЗВАНИИ (УПАКОВКА НАПИТКОВ/КОФЕ) ═══
Продукция MacCoffee, MacTea, MacChocolate и аналогичных марок содержит
в названии тройную запись «A*B*C gr» (или «A*B*C г»), где:
  A = пачек/стиков в одной коробке
  B = саше/стиков в одной пачке (= pack_size)
  C = граммов в одном саше

  ┌─────────────────────────────────────────────────────────────────────┐
  │  quantity  = Кол-во_из_колонки × A  (общее число пачек)            │
  │  pack_size = B  (саше в пачке)                                     │
  │  unit_price = total_price ÷ quantity  (цена за пачку)              │
  │  total_price = из колонки «Сумма»                                  │
  └─────────────────────────────────────────────────────────────────────┘
  Пример: «MacCoffee Americano 24*20*18 gr», К-во=1, Итого=2320
    → quantity = 1 × 24 = 24,  pack_size = 20,  unit_price = 2320/24 ≈ 96.67
  Пример: «MacCoffee 3 in 1 40*25*20 gr RU», К-во=8, Итого=16600
    → quantity = 8 × 40 = 320, pack_size = 25, unit_price = 16600/320 ≈ 51.88
  Пример: «MacTea Lemon 50*20*16 gr», К-во=1, Итого=1660
    → quantity = 1 × 50 = 50,  pack_size = 20,  unit_price = 1660/50 = 33.20

═══ ПРАВИЛО №2 — ОСТАЛЬНЫЕ ПОЛЯ ═══
- name:        точно как написано; включай бренд, объём, %, вес
- barcode:     числовой штрихкод (8–14 цифр) из колонок: «Штрихкод», «ШК», «EAN»,
               «Номенклатурный номер», «Номенкл. №», «Ном. номер» — или null
- article:     буквенно-цифровой артикул/код товара из отдельной колонки накладной
               (например «АРТ-001», «SM0045», «000123»); НЕ путать со штрихкодом; иначе null
- unit:        шт / кг / л / упак и т.д.; «шт» по умолчанию
- quantity:    см. Правило №1. Проверяй: quantity × unit_price ≈ total_price
- pack_size:   1 если нет признаков упаковки
- unit_price:  ПРИОРИТЕТ:
               1) Колонка «Цена за ед. с НДС» / «Цена с НДС» → читай напрямую
               2) Иначе: total_price ÷ quantity
               ❌ НЕ берём из «ЦЕНА ЗА УП.» / «ЦЕНА БЕЗ НДС» / «ЦЕНА БЕЗ АКЦИЗА»
- total_price: из правила №0 (с НДС); если нет → quantity × unit_price

═══ ПРАВИЛО №3 — ИГНОРИРОВАТЬ ═══
Строки итогов, заголовки, реквизиты, подписи, НДС-строки, пустые строки.

═══ ПРАВИЛО №3в — СЕТЧАТЫЙ ПРАЙС-ЛИСТ (КОЛОНКИ ПО ВКУСАМ/ВИДАМ) ═══
Некоторые дистрибьюторы оформляют заказ сеткой:
  строки  = весовые варианты (70г, 100г, 200г и т.п.)
  колонки = вкусы или виды товара (классика, соль, особо солёный, барбекю, сыр…)
  цена    = одна на всю строку (одинакова для всех вкусов одного веса)

Правила для такого формата:
  ┌─────────────────────────────────────────────────────────────────────┐
  │  Каждая НЕПУСТАЯ ячейка (вес × вкус) = ОТДЕЛЬНЫЙ товар в JSON      │
  │  name       = «[Группа товара] [вес] [вкус]»                       │
  │  quantity   = число из этой ячейки                                  │
  │  unit_price = цена из колонки «цена» той же строки                  │
  │  total_price = quantity × unit_price                                │
  │  Пустые ячейки → пропустить (вкус не заказан)                      │
  └─────────────────────────────────────────────────────────────────────┘
  ⛔ НЕ суммировать количества из разных вкусовых колонок в одну строку!
  Пример: «Семечки 70г» цена=267 | классика=5 | соль=5
    ✅ ВЕРНО:  Товар 1 «Семечки 70г классика» qty=5, price=267
              Товар 2 «Семечки 70г соль»     qty=5, price=267
    ❌ ОШИБКА: «Семечки 70г» qty=10, price=267  ← НЕ ДЕЛАТЬ
  Пример: «Семечки 100г» цена=408 | классика=5 | соль=20 | особо соленый=10
    ✅ ВЕРНО:  3 отдельных товара: qty=5, qty=20, qty=10 → все по price=408

═══ ПРАВИЛО №3б — НЕЗАКАЗАННЫЕ СТРОКИ (ПРАЙС-ЛИСТЫ) ═══
Некоторые поставщики присылают накладную-прайслист: весь ассортимент напечатан,
а покупатель вписывает ручкой только те позиции которые ЗАКАЗАЛ.

  ┌─────────────────────────────────────────────────────────────────────┐
  │  Если у строки ПУСТА колонка Количество (К-во) — товар не заказан. │
  │  Если у строки ПУСТА колонка Сумма (Итого) — товар не заказан.     │
  │  Пропусти такую строку — НЕ добавляй в JSON.                       │
  │  НЕ подставляй quantity=1 для пустой ячейки!                       │
  └─────────────────────────────────────────────────────────────────────┘
  Пример: строка «Товар А» — Количество=[пусто], Цена=165, Сумма=[пусто]
    → ПРОПУСТИТЬ, товар не был заказан.
  Пример: строка «Товар Б» — Количество=40, Цена=220, Сумма=8800
    → ВКЛЮЧИТЬ: quantity=40, unit_price=220, total_price=8800.

═══ ПРАВИЛО №4 — ВЕСОВЫЕ ТОВАРЫ ═══
- is_weight: true  — товар продаётся по весу: quantity задана в кг/г (bulk-товар,
             например «сыр весовой», «мясо», «рыба» и т.п.), unit = "кг" или "г"
- is_weight: false — для всех остальных штучных товаров (по умолчанию)
"""

_MIME = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".webp": "image/webp",
}


async def extract_invoice_items(image_path: str) -> List[Dict]:
    """
    OCR накладной через Gemini. Возвращает список товарных позиций.
    При любой ошибке API бросает ValueError с понятным русским сообщением.
    """
    mime_type  = _MIME.get(Path(image_path).suffix.lower(), "image/jpeg")
    model_name = GEMINI_MODEL

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    logger.info("Запрос к Gemini (модель: %s, размер фото: %d байт)", model_name, len(image_bytes))

    try:
        model = genai.GenerativeModel(model_name)
        loop  = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: model.generate_content(
                [{"mime_type": mime_type, "data": image_bytes}, _PROMPT]
            ),
        )
    except google_exceptions.NotFound as e:
        logger.error("Модель Gemini не найдена: %s", e)
        raise ValueError(
            f"❌ Модель <b>{model_name}</b> не найдена.\n\n"
            "Проверьте значение <code>GEMINI_MODEL</code> в файле .env\n"
            "Доступные модели: <code>gemini-2.0-flash-lite</code>, "
            "<code>gemini-2.0-flash</code>, <code>gemini-2.5-flash</code>"
        )
    except google_exceptions.PermissionDenied as e:
        logger.error("Ошибка доступа Gemini API: %s", e)
        raise ValueError(
            "❌ Неверный или отозванный <b>GEMINI_API_KEY</b>.\n\n"
            "Проверьте ключ на https://aistudio.google.com/apikey"
        )
    except google_exceptions.ResourceExhausted as e:
        logger.error("Превышен лимит Gemini API: %s", e)
        raise ValueError(
            "❌ Превышен лимит запросов Gemini API.\n"
            "Подождите минуту и попробуйте снова.\n"
            "Или перейдите на платный тариф на https://aistudio.google.com"
        )
    except google_exceptions.InvalidArgument as e:
        logger.error("Неверный аргумент Gemini: %s", e)
        raise ValueError(
            "❌ Не удалось отправить изображение в Gemini.\n"
            "Убедитесь, что фото в формате JPG, PNG или WEBP."
        )
    except google_exceptions.ServiceUnavailable as e:
        logger.error("Gemini API недоступен: %s", e)
        raise ValueError(
            "❌ Сервис Gemini временно недоступен.\n"
            "Попробуйте через несколько минут."
        )
    except Exception as e:
        logger.exception("Неизвестная ошибка Gemini API")
        raise ValueError(
            f"❌ Ошибка при обращении к Gemini API:\n<code>{type(e).__name__}: {str(e)[:200]}</code>"
        )

    # ── Парсинг ответа ────────────────────────────────────────────────────────
    raw = response.text.strip()
    logger.debug("Полный ответ Gemini (%d симв.):\n%s", len(raw), raw)

    # Убрать markdown-обёртки если есть
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Gemini вернул невалидный JSON: %s\nОтвет: %s", exc, raw[:800])
        raise ValueError(
            "❌ ИИ не смог разобрать содержимое накладной.\n"
            "Попробуйте:\n"
            "• Сфотографировать чётче и под прямым углом\n"
            "• Убедиться, что весь лист попал в кадр\n"
            "• Улучшить освещение (без теней)"
        )

    if not isinstance(data, list):
        raise ValueError("❌ Неожиданный формат ответа от ИИ. Попробуйте ещё раз.")

    logger.info("Gemini вернул %d строк JSON", len(data))

    items: List[Dict] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue

        qty         = _to_float(row.get("quantity"),   default=1.0)
        pack_size   = max(1, int(_to_float(row.get("pack_size"), default=1.0)))
        unit_price  = _to_float(row.get("unit_price"), default=0.0)
        total_price = _to_float(row.get("total_price"), default=0.0)

        # Если Gemini вернул цену за упаковку вместо цены за единицу — пересчитать
        # (защита на случай неполного следования промпту)
        if pack_size > 1 and unit_price > 0:
            # Проверяем: unit_price × qty ≈ total_price → всё верно
            # unit_price × qty × pack_size ≈ total_price → значит unit_price = цена за упак
            if total_price > 0:
                expected_by_unit = unit_price * qty
                expected_by_box  = unit_price * (qty / pack_size)
                if abs(expected_by_box - total_price) < abs(expected_by_unit - total_price):
                    # unit_price оказался ценой за упаковку — делим
                    unit_price = unit_price / pack_size

        # ── Undo «вес*N» в названии — ошибочное N×M из промпта №1б ─────────────
        # «90г*12», «38г*24», «1.5л*6» и т.п. в НАЗВАНИИ = формат упаковки,
        # а НЕ запись количества. Если Gemini всё равно перемножил — отменяем.
        if pack_size > 1:
            _wc = re.search(
                r'[\d.,]+\s*(?:г|мл|кг|л)\s*[*×xX]\s*(\d+)',
                name,
                re.IGNORECASE,
            )
            if _wc:
                M = int(_wc.group(1))
                if M == pack_size and qty > 0 and qty % M == 0:
                    orig_qty = qty / M
                    if orig_qty >= 1:
                        logger.info(
                            "  undo weight*count: '%s'  qty %g→%g  price %.4f→%.4f  (pack=%d)",
                            name[:45], qty, orig_qty, unit_price, unit_price * M, M,
                        )
                        unit_price = unit_price * M
                        qty        = orig_qty
                        # pack_size оставляем = M (число штук в блоке — полезно для Excel)

        # ── Правило №1г: A*B*C gr в названии (MacCoffee и т.п.) ────────────
        # «24*20*18 gr» → A=24 (пачек/кор), B=20 (саше/пачку), C=18 (грамм/саше)
        # pack_size = B (второе число).
        #
        # Gemini непоследовательно умножает qty: иногда на A, иногда на B.
        # Мы ВСЕГДА пересчитываем: находим исходное кол-во коробок из колонки,
        # затем считаем qty = коробки × B (количество саше в пачке).
        _abc = re.search(
            r'\b(\d+)\s*[*×xX]\s*(\d+)\s*[*×xX]\s*[\d.,]+\s*(?:gr|г)\b',
            name,
            re.IGNORECASE,
        )
        if _abc:
            A = int(_abc.group(1))
            B = int(_abc.group(2))
            if A >= 1 and B >= 1:
                # Восстановить исходное кол-во коробок из колонки:
                # Gemini мог умножить на A, на B, или на A*B, или оставить как есть
                boxes = qty  # по умолчанию — не умножалось
                if A != B:
                    if qty > 1 and qty % A == 0 and qty // A != B:
                        boxes = qty // A      # Gemini умножил на A
                    elif qty > 1 and qty % B == 0 and qty // B != A:
                        boxes = qty // B      # Gemini умножил на B
                    elif qty > 1 and qty % (A * B) == 0:
                        boxes = qty // (A * B)  # Gemini умножил на A*B
                else:
                    # A == B (напр. 20*20*25.5): проверить делимость
                    if qty > 1 and qty % A == 0:
                        boxes = qty // A

                new_qty = boxes * B
                pack_size = B

                if new_qty != qty:
                    logger.info(
                        "  Правило 1г: '%s' A=%d B=%d | boxes=%g  qty %g→%g  price %.2f→%.2f",
                        name[:40], A, B, boxes, qty, new_qty,
                        unit_price, total_price / new_qty if new_qty > 0 else 0,
                    )
                    qty = new_qty
                    if total_price > 0 and qty > 0:
                        unit_price = total_price / qty
                else:
                    logger.debug(
                        "  Правило 1г: '%s' A=%d B=%d → pack_size=%d (qty OK)",
                        name[:40], A, B, B,
                    )

        # ── Правило №1в: количество в названии товара ────────────────────────
        # Если в имени «100шт», «48шт» и т.п., а pack_size ещё не учтён —
        # значит кол-во в накладной = число упаковок, а не штук.
        if pack_size == 1:
            _m = re.search(r'\b(\d+)\s*шт\b', name, re.IGNORECASE)
            if _m:
                _name_qty = int(_m.group(1))
                # Применяем только когда qty небольшое (число упаковок ≤ 24)
                # и name_qty достаточно большое чтобы быть размером упаковки
                if _name_qty >= 6 and 1 <= qty <= 24:
                    pack_size   = _name_qty
                    qty         = qty * pack_size
                    if total_price > 0 and qty > 0:
                        unit_price = total_price / qty
                    elif unit_price > 0:
                        unit_price = unit_price / pack_size
                    logger.debug(
                        "Правило №1в: '%s' → pack_size=%d, qty=%g, unit_price=%.4f",
                        name, pack_size, qty, unit_price,
                    )

        # Восстановить unit_price из total если нужно
        if unit_price == 0 and total_price > 0 and qty > 0:
            unit_price = total_price / qty
        # Восстановить total из unit_price если нужно
        if total_price == 0 and unit_price > 0:
            total_price = unit_price * qty

        # Нормализовать article: null / None / пустая строка → None
        raw_article = str(row.get("article") or "").strip()
        article = raw_article if raw_article and raw_article.lower() not in ("null", "none") else None

        logger.info(
            "  OCR[%02d] %-50s  qty=%-6g  price=%-8.2f  total=%-9.2f  bc=%s  art=%s",
            len(items) + 1,
            name[:50],
            qty,
            unit_price,
            total_price,
            row.get("barcode") or "—",
            article or "—",
        )

        items.append({
            "name":        name,
            "barcode":     str(row["barcode"]).strip() if row.get("barcode") else None,
            "article":     article,
            "quantity":    qty,
            "pack_size":   pack_size,
            "price":       unit_price,
            "total_price": total_price,
            "unit":        str(row.get("unit") or "шт").strip(),
            "is_weight":   bool(row.get("is_weight", False)),
        })

    logger.info("Извлечено позиций: %d", len(items))
    return items


_BARCODE_PROMPT = (
    "На изображении штрихкод товара (EAN-8, EAN-13, Code 128 и т.п.).\n"
    "Верни ТОЛЬКО цифры штрихкода — одну строку без пробелов и пояснений.\n"
    "Если штрихкод нечитаем или отсутствует — верни слово: null"
)


async def extract_barcode(image_path: str) -> Optional[str]:
    """
    Считать штрихкод с фотографии через Gemini.
    Возвращает строку цифр (минимум 4) или None если не найдено / не читается.
    """
    mime_type = _MIME.get(Path(image_path).suffix.lower(), "image/jpeg")
    with open(image_path, "rb") as f:
        image_bytes = f.read()

    try:
        model    = genai.GenerativeModel(GEMINI_MODEL)
        loop     = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: model.generate_content(
                [{"mime_type": mime_type, "data": image_bytes}, _BARCODE_PROMPT]
            ),
        )
    except Exception as e:
        logger.error("Ошибка Gemini при считывании штрихкода: %s", e)
        return None

    raw = response.text.strip()
    logger.debug("Штрихкод из Gemini: %r", raw)

    if raw.lower() in ("null", "none", "нет", "не видно", ""):
        return None

    # Оставляем только цифры (EAN содержит только цифры)
    digits = "".join(c for c in raw if c.isdigit())
    return digits if len(digits) >= 4 else None


def _to_float(val, default: float = 0.0) -> float:
    try:
        return float(str(val).replace(",", "."))
    except (TypeError, ValueError):
        return default
