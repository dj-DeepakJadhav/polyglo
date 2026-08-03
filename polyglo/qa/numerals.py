"""Number-to-words expansion, per locale.

Why this exists: the QA gate compares the text we *sent* to TTS against what ASR
*heard*. The source text says "3"; the ASR transcript says "tres". Without expansion
that is a substitution error, and every sentence containing a digit fails the gate for
no reason. This module is small, boring, and the single most common cause of a
naive WER implementation producing garbage.

Coverage is explicit rather than pretended. An expander returns ``None`` for values it
cannot render correctly, and the caller then leaves the digits untouched — a known
false-failure risk is better than confidently emitting the wrong word, which is a false
failure *and* a lie in the diff panel.

Diacritics are stripped downstream during normalisation, so expansions here do not need
to match ASR accent conventions exactly ("dieciséis" vs "dieciseis" both fold to
"dieciseis").
"""

from __future__ import annotations

from collections.abc import Callable

# ---------------------------------------------------------------------------
# English
# ---------------------------------------------------------------------------

_EN_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_EN_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
            "eighty", "ninety"]


def _en(n: int) -> str | None:
    if not 0 <= n <= 9999:
        return None
    if n < 20:
        return _EN_ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _EN_TENS[t] + (f" {_EN_ONES[o]}" if o else "")
    if n < 1000:
        h, r = divmod(n, 100)
        out = f"{_EN_ONES[h]} hundred"
        return out + (f" {_en(r)}" if r else "")
    th, r = divmod(n, 1000)
    out = f"{_en(th)} thousand"
    return out + (f" {_en(r)}" if r else "")


# ---------------------------------------------------------------------------
# Spanish
# ---------------------------------------------------------------------------

_ES_ONES = [
    "cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve",
    "diez", "once", "doce", "trece", "catorce", "quince", "dieciseis", "diecisiete",
    "dieciocho", "diecinueve", "veinte", "veintiuno", "veintidos", "veintitres",
    "veinticuatro", "veinticinco", "veintiseis", "veintisiete", "veintiocho",
    "veintinueve",
]
_ES_TENS = ["", "", "veinte", "treinta", "cuarenta", "cincuenta", "sesenta",
            "setenta", "ochenta", "noventa"]
_ES_HUNDREDS = ["", "ciento", "doscientos", "trescientos", "cuatrocientos",
                "quinientos", "seiscientos", "setecientos", "ochocientos",
                "novecientos"]


def _es(n: int) -> str | None:
    if not 0 <= n <= 9999:
        return None
    if n < 30:
        return _ES_ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _ES_TENS[t] + (f" y {_ES_ONES[o]}" if o else "")
    if n == 100:
        return "cien"
    if n < 1000:
        h, r = divmod(n, 100)
        return _ES_HUNDREDS[h] + (f" {_es(r)}" if r else "")
    th, r = divmod(n, 1000)
    head = "mil" if th == 1 else f"{_es(th)} mil"
    return head + (f" {_es(r)}" if r else "")


# ---------------------------------------------------------------------------
# French  — the awkward one: 70/80/90 are arithmetic, and 21/31/... take "et"
# ---------------------------------------------------------------------------

_FR_ONES = [
    "zero", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf",
    "dix", "onze", "douze", "treize", "quatorze", "quinze", "seize", "dix-sept",
    "dix-huit", "dix-neuf",
]
_FR_TENS = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante", 6: "soixante"}


def _fr_below_100(n: int) -> str:
    if n < 20:
        return _FR_ONES[n]
    if n < 70:
        t, o = divmod(n, 10)
        base = _FR_TENS[t]
        if o == 0:
            return base
        if o == 1:
            return f"{base} et un"
        return f"{base}-{_FR_ONES[o]}"
    if n < 80:                                  # 70-79 = soixante + 10..19
        rest = n - 60
        if rest == 11:
            return "soixante et onze"
        return f"soixante-{_FR_ONES[rest]}"
    # 80-99 = quatre-vingt(s) + 0..19
    rest = n - 80
    if rest == 0:
        return "quatre-vingts"                  # trailing s only when exactly 80
    return f"quatre-vingt-{_FR_ONES[rest]}"


def _fr(n: int) -> str | None:
    if not 0 <= n <= 9999:
        return None
    if n < 100:
        return _fr_below_100(n)
    if n < 1000:
        h, r = divmod(n, 100)
        if h == 1:
            head = "cent"
        else:
            head = f"{_FR_ONES[h]} cent" + ("s" if r == 0 else "")
        return head + (f" {_fr(r)}" if r else "")
    th, r = divmod(n, 1000)
    head = "mille" if th == 1 else f"{_fr(th)} mille"
    return head + (f" {_fr(r)}" if r else "")


# ---------------------------------------------------------------------------
# German — compound, written as one word, units before tens
# ---------------------------------------------------------------------------

_DE_ONES = [
    "null", "eins", "zwei", "drei", "vier", "funf", "sechs", "sieben", "acht", "neun",
    "zehn", "elf", "zwolf", "dreizehn", "vierzehn", "funfzehn", "sechzehn",
    "siebzehn", "achtzehn", "neunzehn",
]
_DE_ONES_COMBINING = list(_DE_ONES)
_DE_ONES_COMBINING[1] = "ein"                   # einundzwanzig, not einsundzwanzig
_DE_TENS = ["", "", "zwanzig", "dreissig", "vierzig", "funfzig", "sechzig",
            "siebzig", "achtzig", "neunzig"]


def _de(n: int) -> str | None:
    if not 0 <= n <= 9999:
        return None
    if n < 20:
        return _DE_ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        if o == 0:
            return _DE_TENS[t]
        return f"{_DE_ONES_COMBINING[o]}und{_DE_TENS[t]}"
    # "hundert"/"tausend" and "einhundert"/"eintausend" are both valid German.
    # We emit the explicit "ein-" forms because TTS engines reading a numeral aloud
    # tend to be explicit, and it is the ASR transcript we have to match. If real
    # Riva output turns out to prefer the short forms, this is a one-line change —
    # flagged for threshold calibration in task #18.
    if n < 1000:
        h, r = divmod(n, 100)
        head = f"{_DE_ONES_COMBINING[h]}hundert"
        return head + (_de(r) or "" if r else "")
    th, r = divmod(n, 1000)
    head = f"{_de(th)}tausend" if th > 1 else "eintausend"
    return head + (_de(r) or "" if r else "")


# ---------------------------------------------------------------------------
# Italian — elision: venti + uno -> ventuno, venti + otto -> ventotto
# ---------------------------------------------------------------------------

_IT_ONES = [
    "zero", "uno", "due", "tre", "quattro", "cinque", "sei", "sette", "otto", "nove",
    "dieci", "undici", "dodici", "tredici", "quattordici", "quindici", "sedici",
    "diciassette", "diciotto", "diciannove",
]
_IT_TENS = ["", "", "venti", "trenta", "quaranta", "cinquanta", "sessanta",
            "settanta", "ottanta", "novanta"]


def _it(n: int) -> str | None:
    if not 0 <= n <= 9999:
        return None
    if n < 20:
        return _IT_ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        base = _IT_TENS[t]
        if o == 0:
            return base
        if o in (1, 8):                          # drop the final vowel before uno/otto
            base = base[:-1]
        if o == 3:                               # accented tre in compounds
            return f"{base}tre"
        return f"{base}{_IT_ONES[o]}"
    if n < 1000:
        h, r = divmod(n, 100)
        head = "cento" if h == 1 else f"{_IT_ONES[h]}cento"
        return head + (_it(r) or "" if r else "")
    th, r = divmod(n, 1000)
    head = "mille" if th == 1 else f"{_it(th)}mila"
    return head + (_it(r) or "" if r else "")


# ---------------------------------------------------------------------------
# Portuguese (Brazil)
# ---------------------------------------------------------------------------

_PT_ONES = [
    "zero", "um", "dois", "tres", "quatro", "cinco", "seis", "sete", "oito", "nove",
    "dez", "onze", "doze", "treze", "quatorze", "quinze", "dezesseis", "dezessete",
    "dezoito", "dezenove",
]
_PT_TENS = ["", "", "vinte", "trinta", "quarenta", "cinquenta", "sessenta",
            "setenta", "oitenta", "noventa"]
_PT_HUNDREDS = ["", "cento", "duzentos", "trezentos", "quatrocentos", "quinhentos",
                "seiscentos", "setecentos", "oitocentos", "novecentos"]


def _pt(n: int) -> str | None:
    if not 0 <= n <= 9999:
        return None
    if n < 20:
        return _PT_ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _PT_TENS[t] + (f" e {_PT_ONES[o]}" if o else "")
    if n == 100:
        return "cem"
    if n < 1000:
        h, r = divmod(n, 100)
        return _PT_HUNDREDS[h] + (f" e {_pt(r)}" if r else "")
    th, r = divmod(n, 1000)
    head = "mil" if th == 1 else f"{_pt(th)} mil"
    return head + (f" e {_pt(r)}" if r else "")


# ---------------------------------------------------------------------------
# Japanese — kanji numerals, regular system
# ---------------------------------------------------------------------------

_JA_DIGITS = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九"]


def _ja(n: int) -> str | None:
    if not 0 <= n <= 9999:
        return None
    if n == 0:
        return "零"
    parts: list[str] = []
    for value, mark in ((1000, "千"), (100, "百"), (10, "十")):
        q, n = divmod(n, value)
        if q:
            parts.append(("" if q == 1 else _JA_DIGITS[q]) + mark)
    if n:
        parts.append(_JA_DIGITS[n])
    return "".join(parts)


# ---------------------------------------------------------------------------
# Hindi — 0-100, complete
#
# Hindi 21-99 are individually irregular (each has its own word, not a compound of
# tens+ones the way European languages work), so this is a literal lookup table
# rather than an algorithm. Originally left as a partial table (0-20, tens only)
# with 21-99 declining rather than guessing, on the reasoning that a wrong entry
# produces a false WER failure indistinguishable from a real TTS bug — worse than
# leaving the digit unexpanded.
#
# Completed against a table cross-referenced across two independent sources
# (englishtohindi.net/hindi-numbers as primary; individual spot-checks against
# search results for 44, 75, 79 — the three entries a first recollection attempt
# got wrong relative to the primary source — confirmed the primary source's
# spelling in every case: 44 चौवालीस, 75 पचहत्तर, 79 उन्यासी). Still recommend a
# native-Hindi-reading spot check before relying on this for anything beyond a
# hackathon QA gate — Unicode Devanagari transcription from any source, including
# a cross-referenced one, is exactly the kind of thing worth a second human look.
# ---------------------------------------------------------------------------

_HI_TABLE: dict[int, str] = {
    0: "शून्य", 1: "एक", 2: "दो", 3: "तीन", 4: "चार", 5: "पाँच",
    6: "छह", 7: "सात", 8: "आठ", 9: "नौ", 10: "दस",
    11: "ग्यारह", 12: "बारह", 13: "तेरह", 14: "चौदह", 15: "पंद्रह",
    16: "सोलह", 17: "सत्रह", 18: "अठारह", 19: "उन्नीस", 20: "बीस",
    21: "इक्कीस", 22: "बाईस", 23: "तेईस", 24: "चौबीस", 25: "पच्चीस",
    26: "छब्बीस", 27: "सत्ताईस", 28: "अट्ठाईस", 29: "उनतीस", 30: "तीस",
    31: "इकतीस", 32: "बत्तीस", 33: "तैंतीस", 34: "चौंतीस", 35: "पैंतीस",
    36: "छत्तीस", 37: "सैंतीस", 38: "अड़तीस", 39: "उनतालीस", 40: "चालीस",
    41: "इकतालीस", 42: "बयालीस", 43: "तैंतालीस", 44: "चौवालीस", 45: "पैंतालीस",
    46: "छियालीस", 47: "सैंतालीस", 48: "अड़तालीस", 49: "उनचास", 50: "पचास",
    51: "इक्यावन", 52: "बावन", 53: "तिरेपन", 54: "चौवन", 55: "पचपन",
    56: "छप्पन", 57: "सत्तावन", 58: "अट्ठावन", 59: "उनसठ", 60: "साठ",
    61: "इकसठ", 62: "बासठ", 63: "तिरेसठ", 64: "चौंसठ", 65: "पैंसठ",
    66: "छियासठ", 67: "सड़सठ", 68: "अड़सठ", 69: "उनहत्तर", 70: "सत्तर",
    71: "इकहत्तर", 72: "बहत्तर", 73: "तिहत्तर", 74: "चौहत्तर", 75: "पचहत्तर",
    76: "छिहत्तर", 77: "सतहत्तर", 78: "अठहत्तर", 79: "उन्यासी", 80: "अस्सी",
    81: "इक्यासी", 82: "बयासी", 83: "तिरासी", 84: "चौरासी", 85: "पचासी",
    86: "छियासी", 87: "सत्तासी", 88: "अट्ठासी", 89: "नवासी", 90: "नब्बे",
    91: "इक्यानवे", 92: "बानवे", 93: "तिरानवे", 94: "चौरानवे", 95: "पचानवे",
    96: "छियानवे", 97: "सत्तानवे", 98: "अट्ठानवे", 99: "निन्यानवे", 100: "सौ",
}


def _hi(n: int) -> str | None:
    return _HI_TABLE.get(n)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

Expander = Callable[[int], "str | None"]

EXPANDERS: dict[str, Expander] = {
    "en": _en, "es": _es, "fr": _fr, "de": _de,
    "it": _it, "pt": _pt, "ja": _ja, "hi": _hi,
}

#: Human-readable coverage, surfaced in docs and the dashboard so the limitation
#: is visible rather than discovered during a demo.
COVERAGE: dict[str, str] = {
    "en": "0-9999", "es": "0-9999", "fr": "0-9999", "de": "0-9999",
    "it": "0-9999", "pt": "0-9999", "ja": "0-9999 (kanji)",
    "hi": "0-100 (literal table, not an algorithm — see numerals.py's Hindi section)",
}


def language_of(locale: str) -> str:
    """'es-ES' -> 'es'."""
    return locale.split("-")[0].lower()


def number_to_words(n: int, locale: str) -> str | None:
    """Render ``n`` in words for ``locale``; ``None`` if unsupported."""
    fn = EXPANDERS.get(language_of(locale))
    return fn(n) if fn else None


def coverage(locale: str) -> str:
    return COVERAGE.get(language_of(locale), "unsupported — digits left as-is")


def is_supported(locale: str) -> bool:
    return language_of(locale) in EXPANDERS
