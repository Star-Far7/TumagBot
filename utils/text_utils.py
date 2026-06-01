import re
from typing import List

# Слова, не несущие смысловой нагрузки при поиске
STOPWORDS = {
    "шт", "уп", "упак", "упаковка", "арт", "артикул", "код",
    "тов", "товар", "пр-во", "произв", "от", "и", "в", "с",
    "на", "по", "для", "the", "a", "an", "of",
}

# Числа с единицами: «1.5л», «200мл», «3.2%», «500г»
_UNIT_RE = re.compile(
    r"(\d+[.,]?\d*)\s*"
    r"(мл|л|г|гр|кг|мг|%|мм|см|mm|cm|ml|kg|g|oz)\b",
    re.IGNORECASE,
)
_BARE_PCT_RE = re.compile(r"(\d+[.,]?\d*)\s*%")
_NUMBER_RE = re.compile(r"\d+[.,]?\d*")

_UNIT_NORM = {
    "мл": "мл", "ml": "мл",
    "л":  "л",  "l":  "л",
    "г":  "г",  "гр": "г", "g": "г",
    "кг": "кг", "kg": "кг",
    "мг": "мг", "mg": "мг",
    "%":  "%",
}


def extract_numeric_tokens(text: str) -> List[str]:
    """Извлечь все числовые маркеры: «1.5л», «3.2%», «500г» и т.п."""
    text_l = text.lower()
    seen: set[str] = set()
    result: List[str] = []

    for m in _UNIT_RE.finditer(text_l):
        num = m.group(1).replace(",", ".")
        unit = _UNIT_NORM.get(m.group(2).lower(), m.group(2).lower())
        token = f"{num}{unit}"
        if token not in seen:
            seen.add(token)
            result.append(token)

    for m in _BARE_PCT_RE.finditer(text_l):
        token = f"{m.group(1).replace(',', '.')}%"
        if token not in seen:
            seen.add(token)
            result.append(token)

    return result


def tokenize_text(text: str) -> List[str]:
    """Вернуть список смысловых текстовых токенов (без цифр и стоп-слов)."""
    cleaned = _UNIT_RE.sub(" ", text.lower())
    cleaned = _BARE_PCT_RE.sub(" ", cleaned)
    cleaned = _NUMBER_RE.sub(" ", cleaned)
    tokens = re.split(r"[\s,./\-_()+«»\"']+", cleaned)
    return [t for t in tokens if t and t not in STOPWORDS and len(t) > 1]
