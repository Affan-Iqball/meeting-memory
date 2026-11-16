"""Citation machinery. Design rule: models never type quotes. They return
(meeting_id, turn_idx, word range) and ``quote_from_span`` copies the text.

Grading rule mirrored here (from questions.json): a citation is a meeting id
plus a quote of >= 6 words copied exactly from ONE speaker turn of that
transcript; case and spacing are ignored; punctuation is NOT ignored.

Public API::

    normalize_for_match(s) -> str
    verify_quote(meeting_id, quote, conn=None) -> (ok: bool, turn_idx: int | None, reason: str)
    quote_from_span(meeting_id, turn_idx, start_word=None, end_word=None,
                    max_words=MAX_QUOTE_WORDS, conn=None) -> str
    QuoteError (ValueError) — raised by quote_from_span; has .suggestions

Reason codes (the reason string always starts with one of these):
    "ok", "too_short", "meeting_not_found", "spans_turns", "near_miss", "not_found"
For "near_miss" the returned turn_idx is the closest turn (difflib), so a
caller can re-derive a correct span with quote_from_span.
"""
from __future__ import annotations

import difflib
import re

from . import store

MIN_WORDS = 6
MAX_QUOTE_WORDS = 60
NEAR_MISS_RATIO = 0.6

_WS = re.compile(r"\s+")
_conn = None


def _c(conn):
    global _conn
    if conn is not None:
        return conn
    if _conn is None:
        _conn = store.connect()
    return _conn


def normalize_for_match(s: str) -> str:
    """Lowercase and collapse whitespace runs; NOTHING else (punctuation and
    underscores are significant)."""
    return _WS.sub(" ", (s or "").lower()).strip()


class QuoteError(ValueError):
    def __init__(self, msg, suggestions=None):
        super().__init__(msg)
        self.suggestions = suggestions or []


def _meeting_turns(conn, meeting_id):
    return conn.execute("SELECT turn_idx, speaker, text, n_words FROM turns WHERE meeting_id=? ORDER BY turn_idx",
                        (meeting_id,)).fetchall()


def verify_quote(meeting_id: str, quote: str, conn=None):
    conn = _c(conn)
    q = normalize_for_match(quote)
    n = len(q.split())
    if n < MIN_WORDS:
        return False, None, f"too_short: {n} words, need >= {MIN_WORDS}"
    rows = _meeting_turns(conn, meeting_id)
    if not rows:
        return False, None, f"meeting_not_found: {meeting_id!r}"
    norm = [normalize_for_match(r["text"]) for r in rows]
    for r, t in zip(rows, norm):
        if q in t:
            return True, r["turn_idx"], "ok"
    # Spanning check 1: all turns in order. Check 2: each speaker's own turns
    # in order (interruptions put other speakers' turns in between a split
    # sentence, e.g. IS1008d t.."Euro per" / [A, B, A, B] / "remote, now ...").
    streams = [("meeting", list(range(len(rows))))]
    for spk in sorted({r["speaker"] for r in rows}):
        streams.append((f"speaker {spk}", [i for i, r in enumerate(rows) if r["speaker"] == spk]))
    for label, idxs in streams:
        parts = [norm[i] for i in idxs]
        joined = " ".join(parts)
        pos = joined.find(q)
        if pos < 0:
            continue
        offs, acc = [], 0
        for t in parts:
            offs.append(acc)
            acc += len(t) + 1
        first = idxs[max(i for i, o in enumerate(offs) if o <= pos)]
        last = idxs[max(i for i, o in enumerate(offs) if o <= pos + len(q) - 1)]
        return False, rows[first]["turn_idx"], (
            f"spans_turns: text exists but crosses turn boundaries t{rows[first]['turn_idx']}..t{rows[last]['turn_idx']} "
            f"({label} order); quote from within one turn")
    # near miss: closest turn by the share of the quote's WORDS that difflib
    # aligns, in order, with the turn's words (word-level, so scattered
    # character matches in a long turn do not count).
    best, best_ratio = None, 0.0
    qw = q.split()
    qset = set(qw)
    for r, t in zip(rows, norm):
        tw = t.split()
        if len(qset & set(tw)) < max(2, len(qset) // 3):
            continue
        # slide a window ~quote length (+slack) over the turn; best aligned share wins
        width = len(qw) + 3
        step = max(1, len(qw) // 3)
        ratio = 0.0
        for s in range(0, max(1, len(tw) - width + step), step):
            sm = difflib.SequenceMatcher(None, qw, tw[s:s + width], autojunk=False)
            ratio = max(ratio, sum(b.size for b in sm.get_matching_blocks()) / len(qw))
        if ratio > best_ratio:
            best, best_ratio = r, ratio
    if best is not None and best_ratio >= NEAR_MISS_RATIO:
        return False, best["turn_idx"], (
            f"near_miss: closest is t{best['turn_idx']} (speaker {best['speaker']}, {best_ratio:.0%} of characters match): "
            f"{best['text'][:200]!r}")
    return False, None, "not_found: quote not in any turn of this meeting"


def quote_from_span(meeting_id: str, turn_idx: int, start_word: int | None = None, end_word: int | None = None,
                    max_words: int = MAX_QUOTE_WORDS, conn=None) -> str:
    """Copy raw words [start_word, end_word) of one turn (whole turn if both
    omitted), joined by single spaces. If the span is longer than
    ``max_words`` it is TRIMMED to its first ``max_words`` words (explicit
    cap; pass max_words=None to disable). Raises QuoteError if the turn does
    not exist, the range is invalid, or the result has < 6 words (the error
    lists nearby turns by the same speaker that have >= 6 words)."""
    conn = _c(conn)
    row = conn.execute("SELECT speaker, text FROM turns WHERE meeting_id=? AND turn_idx=?",
                       (meeting_id, int(turn_idx))).fetchone()
    if row is None:
        raise QuoteError(f"no turn {turn_idx} in meeting {meeting_id!r}")
    words = row["text"].split()
    s = 0 if start_word is None else int(start_word)
    e = len(words) if end_word is None else int(end_word)
    if s < 0 or e > len(words) or s >= e:
        raise QuoteError(f"bad word range [{s}, {e}) for t{turn_idx}, which has {len(words)} words")
    span = words[s:e]
    if max_words is not None and len(span) > max_words:
        span = span[:max_words]
    if len(span) < MIN_WORDS:
        near = conn.execute(
            """SELECT turn_idx, n_words, text FROM turns WHERE meeting_id=? AND speaker=? AND n_words>=?
               AND turn_idx BETWEEN ? AND ? ORDER BY abs(turn_idx-?) LIMIT 5""",
            (meeting_id, row["speaker"], MIN_WORDS, int(turn_idx) - 15, int(turn_idx) + 15, int(turn_idx))).fetchall()
        sugg = [{"turn_idx": r["turn_idx"], "n_words": r["n_words"], "text": r["text"][:160]} for r in near]
        listing = "; ".join(f"t{x['turn_idx']} ({x['n_words']}w): {x['text'][:80]!r}" for x in sugg) or "none within 15 turns"
        raise QuoteError(f"span gives {len(span)} words (< {MIN_WORDS}) from t{turn_idx} speaker {row['speaker']}. "
                         f"Nearby turns by the same speaker with >= {MIN_WORDS} words: {listing}", sugg)
    return " ".join(span)
