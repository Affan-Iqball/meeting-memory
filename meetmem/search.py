"""Passage search (FTS5 BM25 over normalised text) and turn reading.

Public API::

    search_passages(query, team=None, meeting_id=None, as_of_ord=None, limit=8, conn=None) -> list[dict]
        each hit: {passage_id, meeting_id, team, ord, first_turn, last_turn, score, text_display}
        score = -bm25 (HIGHER is better). Terms are OR-ed; stopwords dropped.
        as_of_ord: hard cutoff, only meetings with ord <= as_of_ord are returned.
    get_turns(meeting_id, first_turn, last_turn, conn=None) -> list[dict]   # inclusive range, max MAX_TURN_RANGE turns
    team_meetings(team, conn=None) -> list[dict]
    build_fts_query(query) -> str | None    # the exact MATCH expression used

``conn`` defaults to a shared connection on the default database.
"""
from __future__ import annotations

from .normalize import QUERY_STOPWORDS, tokens
from . import store

MAX_TURN_RANGE = 80
MAX_LIMIT = 50

_conn = None


def _c(conn):
    global _conn
    if conn is not None:
        return conn
    if _conn is None:
        _conn = store.connect()
    return _conn


def build_fts_query(query: str) -> str | None:
    """Normalise the query exactly like the index, drop stopwords, quote every
    token (so no FTS5 syntax can leak in) and OR them."""
    toks = tokens(query or "")
    seen, keep = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            keep.append(t)
    content = [t for t in keep if t not in QUERY_STOPWORDS] or keep
    if not content:
        return None
    return "text_norm : (" + " OR ".join(f'"{t}"' for t in content) + ")"


def search_passages(query, team=None, meeting_id=None, as_of_ord=None, limit=8, conn=None) -> list[dict]:
    conn = _c(conn)
    expr = build_fts_query(query)
    if expr is None:
        return []
    if as_of_ord is not None and not team and not meeting_id:
        raise ValueError("as_of_ord needs team (or meeting_id): ord is a position within one team")
    if meeting_id:
        expr = f'({expr}) AND scope : "mtg{meeting_id.lower()}"'
    elif team:
        expr = f'({expr}) AND scope : "team{team.lower()}"'
    sql = """SELECT p.passage_id, p.meeting_id, p.team, p.ord, p.first_turn, p.last_turn,
                    -bm25(passages_fts, 1.0, 0.0) AS score, p.text_display
             FROM passages_fts JOIN passages p ON p.pid = passages_fts.rowid
             WHERE passages_fts MATCH ?"""
    args = [expr]
    if team:
        sql += " AND p.team = ?"; args.append(team)
    if meeting_id:
        sql += " AND p.meeting_id = ?"; args.append(meeting_id)
    if as_of_ord is not None:
        sql += " AND p.ord IS NOT NULL AND p.ord <= ?"; args.append(int(as_of_ord))
    sql += " ORDER BY bm25(passages_fts, 1.0, 0.0) LIMIT ?"
    args.append(max(1, min(int(limit), MAX_LIMIT)))
    return [dict(r) for r in conn.execute(sql, args)]


def get_turns(meeting_id, first_turn, last_turn, conn=None) -> list[dict]:
    """Turns first_turn..last_turn inclusive (clamped to the meeting). Raises
    ValueError if the range exceeds MAX_TURN_RANGE turns."""
    conn = _c(conn)
    first_turn, last_turn = int(first_turn), int(last_turn)
    if last_turn < first_turn:
        raise ValueError("last_turn < first_turn")
    if last_turn - first_turn + 1 > MAX_TURN_RANGE:
        raise ValueError(f"range too large ({last_turn - first_turn + 1} turns, max {MAX_TURN_RANGE})")
    rows = conn.execute(
        """SELECT meeting_id, turn_idx, speaker, start_sec, time_str, text, n_words FROM turns
           WHERE meeting_id=? AND turn_idx BETWEEN ? AND ? ORDER BY turn_idx""",
        (meeting_id, max(0, first_turn), last_turn)).fetchall()
    return [dict(r) for r in rows]


def format_turns(rows) -> str:
    return "\n".join(store.display_line(r["turn_idx"], r["speaker"], r["time_str"], r["text"]) for r in rows)


def team_meetings(team, conn=None) -> list[dict]:
    return store.team_meetings(_c(conn), team)
