import re
from typing import Dict, List, Optional, Tuple

from rapidfuzz import fuzz

from utils.text_utils import extract_numeric_tokens, tokenize_text

_NORM_RE = re.compile(r"\s+")


def _normalize_full(text: str) -> str:
    """Нижний регистр + одиночные пробелы."""
    return _NORM_RE.sub(" ", text.lower()).strip()


def _split_extra_codes(raw: str) -> List[str]:
    """
    Поле extra_code может содержать несколько кодов через ';'.
    Возвращает список очищенных кодов (пустые отбрасываются).
    """
    return [c.strip() for c in raw.split(";") if c.strip()]


class ProductMatcher:
    """
    Многоуровневый нечёткий поиск товаров.

    ── Точное совпадение (приоритет) ─────────────────────────────────────────
    1. barcode   → products.barcode
    2. barcode   → products.extra_code  (поддержка нескольких кодов через ";")
    3. article   → products.extra_code  (артикул из накладной → доп. код в базе)

    ── Нечёткий скоринг (берём максимум трёх уровней) ────────────────────────
    1. token_sort_ratio  — строгое покрытие токенов
    2. token_set_ratio   — ловит аббревиатуры, штрафуется по разнице длин
    3. partial_ratio     — длинные имена / бренды, ограничен × 0.82

    ── Поправки ──────────────────────────────────────────────────────────────
    • Числовой штраф:     −0.28 за каждый несовпавший числовой маркер (объём, %, вес)
    • Бонус ведущих слов: первые 1–2 смысловых слова часто являются брендом или типом.
                          Их совпадение добавляет +0.04 … +0.14 к итогу.
    • Бонус категории:    +0.08 при совпадении категории из запроса.

    Формула итогового скора:
        score = max(token_sort, token_set_adj, partial) + lead_bonus
                - num_penalty + category_bonus
    """

    def __init__(self, products: List[Dict]):
        self.products = products
        self._index: List[Dict] = []
        for p in products:
            tokens = tokenize_text(p["name"])
            lead_n = min(2, len(tokens))
            self._index.append({
                "text":      " ".join(tokens),
                "lead":      set(tokens[:lead_n]),  # первые 1-2 слова (бренд/тип)
                "all_set":   set(tokens),            # все токены — для «мягкого» поиска
                "nums":      set(extract_numeric_tokens(p["name"])),
                "full_norm": _normalize_full(p["name"]),
            })

    # ── Scoring ───────────────────────────────────────────────────────────────

    def score(self, query: str, idx: int, query_category: Optional[str] = None) -> float:
        q_tokens   = tokenize_text(query)
        q_text     = " ".join(q_tokens)
        q_nums     = set(extract_numeric_tokens(query))
        q_full     = _normalize_full(query)
        c          = self._index[idx]

        if not q_text or not c["text"]:
            return 0.0

        # ── Уровень 1: token_sort (строгий) ──────────────────────────────────
        ts = fuzz.token_sort_ratio(q_text, c["text"]) / 100.0

        # ── Уровень 2: token_set с поправкой на покрытие ─────────────────────
        tset     = fuzz.token_set_ratio(q_text, c["text"]) / 100.0
        q_words  = len(q_text.split())
        c_words  = len(c["text"].split())
        coverage = min(q_words, c_words) / max(q_words, c_words)
        tset_adj = tset * (0.5 + 0.5 * coverage)

        # ── Уровень 3: partial ratio (бренды, длинные имена) ─────────────────
        # Ограничен × 0.82, чтобы не превысить auto_threshold без числового подтверждения
        partial = fuzz.partial_ratio(q_full, c["full_norm"]) / 100.0 * 0.82

        text_score = max(ts, tset_adj, partial)

        # ── Числовой штраф ────────────────────────────────────────────────────
        # Каждый объёмный/весовой/процентный маркер из запроса, которого нет в товаре,
        # снижает скор на 0.28 — это жёстко разделяет «молоко 3.2%» и «молоко 2.5%».
        num_penalty = 0.0
        if q_nums:
            unmatched   = q_nums - c["nums"]
            num_penalty = len(unmatched) * 0.28

        # ── Бонус ведущих токенов (бренд / тип товара) ───────────────────────
        # Первые 1-2 смысловых слова запроса и товара сравниваются отдельно:
        # это часто бренд («Простоквашино», «Activia») или тип («Молоко», «Йогурт»).
        #
        # Уровни бонуса (используем первое сработавшее условие):
        #   +0.14 — ВСЕ ведущие токены запроса и товара совпали (бренд + тип совпали)
        #   +0.07 — часть ведущих совпала (общий тип ИЛИ общий бренд)
        #   +0.04 — ведущий токен запроса встречается где-то в названии товара
        lead_bonus = 0.0
        q_lead = set(q_tokens[:min(2, len(q_tokens))]) if q_tokens else set()
        c_lead = c["lead"]
        if q_lead and c_lead:
            # Доля точного совпадения ведущих токенов
            exact = len(q_lead & c_lead) / max(len(q_lead), len(c_lead))
            # Доля ведущих токенов запроса, присутствующих хоть где-то в товаре
            soft  = len(q_lead & c["all_set"]) / len(q_lead)
            if exact == 1.0:
                lead_bonus = 0.14       # полное совпадение (бренд + тип)
            elif exact >= 0.5:
                lead_bonus = 0.07       # одно из двух слов совпало
            elif soft >= 0.5:
                lead_bonus = 0.04       # ведущее слово запроса есть где-то в продукте

        # ── Бонус категории ───────────────────────────────────────────────────
        category_bonus = 0.0
        if query_category:
            prod_cat = self.products[idx].get("category") or ""
            if prod_cat and query_category.lower() == prod_cat.lower():
                category_bonus = 0.08

        return max(0.0, min(1.0, text_score + lead_bonus - num_penalty + category_bonus))

    # ── Find matches ──────────────────────────────────────────────────────────

    def find_matches(
        self,
        query: str,
        barcode: Optional[str] = None,
        article: Optional[str] = None,
        top_n: int = 5,
        auto_threshold: float = 0.82,
        suggest_threshold: float = 0.40,
    ) -> Tuple[str, List[Dict]]:
        """
        Возвращает (match_type, matches).

        match_type:
            'exact_barcode' — точное совпадение по штрихкоду / extra_code / артикулу
            'auto'          — уверенное авто-совпадение (score ≥ auto_threshold)
            'suggest'       — предложить варианты пользователю
            'not_found'     — ничего подходящего нет
        """
        # ── Приоритет 1: основной штрихкод (EAN) ─────────────────────────────
        if barcode:
            for p in self.products:
                if p.get("barcode") and p["barcode"] == barcode:
                    return "exact_barcode", [{"product": p, "score": 1.0}]

        # ── Приоритет 2: extra_code (поддержка нескольких кодов через ";") ───
        # Проверяем и barcode, и article — оба могут быть дополнительным кодом.
        for code in filter(None, [barcode, article]):
            for p in self.products:
                if p.get("extra_code"):
                    if code in _split_extra_codes(p["extra_code"]):
                        return "exact_barcode", [{"product": p, "score": 1.0}]

        # ── Нечёткий текстовый поиск ──────────────────────────────────────────
        scored = []
        for i, p in enumerate(self.products):
            s = self.score(query, i)
            if s >= suggest_threshold:
                scored.append({"product": p, "score": s})

        scored.sort(key=lambda x: x["score"], reverse=True)
        top = scored[:top_n]

        if not top:
            return "not_found", []
        if top[0]["score"] >= auto_threshold:
            return "auto", top
        return "suggest", top
