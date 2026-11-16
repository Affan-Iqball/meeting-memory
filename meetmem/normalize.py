"""Search-text normaliser. The SAME function is applied to passages and queries.

``normalize(text) -> str`` returns space-separated tokens:

* lowercased; punctuation dropped (apostrophes split words: "we're" -> "we re",
  matching what FTS5's unicode61 tokenizer would do anyway);
* underscore acronyms collapsed: ``L_C_D_`` -> ``lcd``, ``T_V_s`` -> ``tvs``
  (the porter stemmer in the FTS index then also matches ``tv``); dotted
  acronyms in queries (``L.C.D.``) collapse the same way;
* spelled-out numbers keep their words AND get a digit token appended right
  after them: "twelve and a half euros" -> "twelve and a half 12.5 euros";
  "twenty five" -> "+ 25"; "fifteen hundred" -> "+ 1500"; "a hundred and
  fifty" -> "+ 150"; "zero point eight" -> "+ 0.8"; "two thousand and five"
  -> "+ 2005";
* "price style" pairs such as "twelve fifty" (an integer 1..99 directly
  followed by a 10..99 number) additionally get the decimal reading and the
  hundreds reading: "12", "50", "12.5", "1250";
* digits already in the text (queries) are canonicalised: "12.50" -> "12.5",
  "1,500" -> "1500", "€12" -> "12".

Only digits and the '.' inside decimals survive as non-letters, so the FTS
index uses ``tokenize="porter unicode61 tokenchars '.'"`` and "12.5" is one
token on both sides.

Known limits (deliberately conservative):

* a bare "one" is never turned into "1" (it is mostly a pronoun: "the blue
  one"); "one hundred", "one point five", "one fifty" are converted.
* the article "a" only counts inside "a hundred / a thousand / a million" and
  "and a half / and a quarter"; "a half" or "half a euro" alone gives nothing.
* ordinals (first, second), fractions other than half/quarter, "a couple",
  "a dozen", "per cent" -> "%" and "twelve euros fifty" are not converted.
* the pair rule ("twelve fifty") can produce a spurious extra token for
  number lists like "seven twelve"; this only adds recall noise.
* numbers split across speaker turns are not joined (each turn is
  normalised separately).
"""
from __future__ import annotations

import re
from decimal import Decimal

UNITS = {"zero": 0, "nought": 0, "one": 1, "two": 2, "three": 3, "four": 4,
         "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
         "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
         "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
        "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
BIG = {"thousand": 1000, "million": 1000000}
FRACTIONS = {"half": Decimal("0.5"), "quarter": Decimal("0.25")}

_ACRO_RE = re.compile(r"\b((?:[a-z]_)+)([a-z]*)")
_DOTTED_RE = re.compile(r"\b((?:[a-z]\.){2,})")
_DIGIT_RE = re.compile(r"\d+(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?|[a-z]+")


def fmt_num(d: Decimal) -> str:
    """Canonical digit form: no trailing zeros, no exponent ("12.50" -> "12.5")."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _canon_digits(m: re.Match) -> str:
    return " " + fmt_num(Decimal(m.group(0).replace(",", ""))) + " "


def _collapse_acronym(m: re.Match) -> str:
    return m.group(1).replace("_", "") + m.group(2)


def _parse_cardinal(toks, i):
    """Greedy English cardinal at toks[i]. Returns (Decimal value, end, n_scale, kinds) or None."""
    total = 0
    cur = 0
    last = None          # None | unit | teen | tens | hundred | big | a | and
    j = i
    kinds = []
    n = len(toks)
    while j < n:
        t = toks[j]
        nxt = toks[j + 1] if j + 1 < n else None
        if t in UNITS or t in TEENS:
            v = UNITS.get(t, TEENS.get(t))
            if last in (None, "hundred", "big", "and") or (last == "tens" and t in UNITS and v > 0):
                cur += v
                last = "teen" if t in TEENS else "unit"
            else:
                break
        elif t in TENS:
            if last in (None, "hundred", "big", "and") and cur % 100 == 0:
                cur += TENS[t]
                last = "tens"
            else:
                break
        elif t == "hundred":
            if last == "a":
                cur = 100
            elif last in ("unit", "teen", "tens") and 0 < cur < 100:
                cur = cur * 100
            else:
                break
            last = "hundred"
        elif t in BIG:
            if last == "a":
                total += BIG[t]
            elif last in ("unit", "teen", "tens", "hundred") and cur > 0:
                total += cur * BIG[t]
            else:
                break
            cur = 0
            last = "big"
        elif t == "a" and last is None and nxt in ("hundred", "thousand", "million"):
            last = "a"
        elif t == "and" and last in ("hundred", "big") and (nxt in UNITS or nxt in TEENS or nxt in TENS):
            last = "and"
        else:
            break
        kinds.append(last)
        j += 1
    while kinds and kinds[-1] in ("a", "and"):
        kinds.pop()
        j -= 1
    if not kinds:
        return None
    n_scale = sum(1 for k in kinds if k in ("hundred", "big"))
    return Decimal(total + cur), j, n_scale, kinds


def _number_tokens(toks):
    """Return a new token list with digit tokens inserted after number phrases."""
    out = []
    i = 0
    n = len(toks)
    prev = None  # (value, n_scale, end_index, simple) of the number phrase ending at i
    while i < n:
        p = _parse_cardinal(toks, i)
        if p is None:
            out.append(toks[i])
            i += 1
            prev = None
            continue
        val, j, n_scale, kinds = p
        words = toks[i:j]
        extra = []
        decimal_used = False
        # "<n> point five [two ...]"
        if j + 1 < n and toks[j] == "point" and (toks[j + 1] in UNITS or toks[j + 1] in TEENS or toks[j + 1] in TENS):
            k = j + 1
            digits = ""
            if toks[k] in UNITS:
                while k < n and toks[k] in UNITS:
                    digits += str(UNITS[toks[k]])
                    k += 1
            else:
                q = _parse_cardinal(toks, k)
                if q and q[2] == 0 and q[0] < 100:
                    digits = str(int(q[0]))
                    k = q[1]
            if digits:
                words = toks[i:k]
                val = Decimal(f"{int(val)}.{digits}")
                j = k
                decimal_used = True
        # "<n> and a half / quarter"
        if not decimal_used and j + 2 < n and toks[j] == "and" and toks[j + 1] == "a" and toks[j + 2] in FRACTIONS:
            val = val + FRACTIONS[toks[j + 2]]
            words = toks[i:j + 3]
            j = j + 3
            decimal_used = True
        simple = (not decimal_used and n_scale == 0)
        # "twelve fifty": previous simple 1..99 immediately followed by this simple 10..99
        if prev is not None and prev[2] == i and prev[3] and simple and 10 <= val <= 99 and 1 <= prev[0] <= 99:
            a, b = int(prev[0]), int(val)
            extra = [fmt_num(Decimal(f"{a}.{b:02d}")), str(a * 100 + b)]
        out.extend(words)
        bare_one = (len(words) == 1 and words[0] == "one")
        if not bare_one:
            out.append(fmt_num(val))
        out.extend(extra)
        prev = (val, n_scale, j, simple)
        i = j
    return out


def tokens(text: str) -> list[str]:
    """Normalised token list (see module docstring)."""
    s = text.lower()
    s = _ACRO_RE.sub(_collapse_acronym, s)
    s = _DOTTED_RE.sub(lambda m: m.group(1).replace(".", ""), s)
    s = _DIGIT_RE.sub(_canon_digits, s)
    return _number_tokens(_TOKEN_RE.findall(s))


def normalize(text: str) -> str:
    """Normalised search text for one turn / passage / query."""
    return " ".join(tokens(text))


# Words dropped from free-text queries only (never from the index).
QUERY_STOPWORDS = frozenset("""
a an the and or but if of to in on at by for with from as is are was were be been
being it its this that these those there here i you he she we they me him her us
them my your our their his what which who whom how when where why do does did
done have has had not no so then than too very can could would should will shall
just about into over also any some all um uh mm hmm okay ok yeah yes oh well like
re s t ll ve d m
""".split())
