"""SQLite schema + per-meeting incremental ingest.

Public API::

    connect(db_path=None) -> sqlite3.Connection     # creates schema if missing
    ingest_file(conn, path) -> dict                  # idempotent per meeting
    ingest_meeting(conn, parsed: ParsedMeeting) -> dict
    team_meetings(conn, team) -> list[dict]          # ordered by letter, with ord
    last_meeting(conn, team) -> dict | None          # highest ord (never assumes "d")
    build_passages(turns) -> list[dict]              # pure function, no DB

Database path: argument > $MEETMEM_DB > <project root>/memory.db.

Scale notes: every write is scoped to one meeting (DELETE ... WHERE
meeting_id=? then INSERT), inside one transaction, so 10,000 new meetings a
day are 10,000 small independent transactions. The only cross-meeting work
is recomputing ``ord`` for the meeting's own team (<= a handful of rows).
The FTS index carries a ``scope`` column (``team<id> mtg<id>`` tokens) so a
team- or meeting-filtered search is an index intersection, not a scan.
"""
from __future__ import annotations

import os
import sqlite3
import time

from .normalize import normalize
from .parse import ParsedMeeting, parse_file

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(PROJECT_ROOT, "memory.db")

PASSAGE_MIN_WORDS = 250
PASSAGE_MAX_WORDS = 350
PASSAGE_OVERLAP_TURNS = 2
PASSAGE_MIN_TAIL_WORDS = 100   # a trailing passage smaller than this is merged into the previous one

SCHEMA = f"""
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id   TEXT PRIMARY KEY,
    team         TEXT NOT NULL,
    letter       TEXT NOT NULL,
    ord          INTEGER,               -- 1-based position in the team's sequence (sorted by letter)
    n_turns      INTEGER NOT NULL,
    n_words      INTEGER NOT NULL,
    header_number TEXT,                 -- raw "Meeting number:" header (letter-based, NOT ord)
    source_path  TEXT,
    parse_problems TEXT,
    ingested_at  REAL
);
CREATE INDEX IF NOT EXISTS meetings_team ON meetings(team, letter);

CREATE TABLE IF NOT EXISTS turns (
    meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id) ON DELETE CASCADE,
    turn_idx   INTEGER NOT NULL,
    speaker    TEXT NOT NULL,
    start_sec  INTEGER NOT NULL,
    time_str   TEXT NOT NULL,
    text       TEXT NOT NULL,
    n_words    INTEGER NOT NULL,
    PRIMARY KEY (meeting_id, turn_idx)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS passages (
    pid          INTEGER PRIMARY KEY,   -- FTS rowid
    passage_id   TEXT UNIQUE NOT NULL,  -- "<meeting_id>:p<NNN>"
    meeting_id   TEXT NOT NULL REFERENCES meetings(meeting_id) ON DELETE CASCADE,
    team         TEXT NOT NULL,
    ord          INTEGER,
    first_turn   INTEGER NOT NULL,
    last_turn    INTEGER NOT NULL,      -- inclusive
    n_words      INTEGER NOT NULL,
    text_display TEXT NOT NULL,
    text_norm    TEXT NOT NULL,
    scope        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS passages_meeting ON passages(meeting_id);
CREATE INDEX IF NOT EXISTS passages_team ON passages(team, ord);

CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
    text_norm, scope,
    content='passages', content_rowid='pid',
    tokenize="porter unicode61 tokenchars '.'"
);
CREATE TRIGGER IF NOT EXISTS passages_ai AFTER INSERT ON passages BEGIN
    INSERT INTO passages_fts(rowid, text_norm, scope) VALUES (new.pid, new.text_norm, new.scope);
END;
CREATE TRIGGER IF NOT EXISTS passages_ad AFTER DELETE ON passages BEGIN
    INSERT INTO passages_fts(passages_fts, rowid, text_norm, scope)
        VALUES ('delete', old.pid, old.text_norm, old.scope);
END;

CREATE TABLE IF NOT EXISTS speakers (
    team              TEXT NOT NULL,
    meeting_id        TEXT NOT NULL REFERENCES meetings(meeting_id) ON DELETE CASCADE,
    speaker           TEXT NOT NULL,
    name              TEXT,
    role              TEXT,
    evidence_turn_idx INTEGER,           -- turn where the role was stated
    name_evidence_turn_idx INTEGER,      -- turn where the name was stated
    source           TEXT NOT NULL,     -- 'regex' here; later passes add e.g. 'llm'
    PRIMARY KEY (meeting_id, speaker, source)
);
"""


def scope_tokens(team: str, meeting_id: str) -> str:
    return f"team{team.lower()} mtg{meeting_id.lower()}"


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or os.environ.get("MEETMEM_DB") or DEFAULT_DB
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    from . import events as events_mod  # local import: events imports cite -> store
    events_mod.migrate(conn)
    return conn


def display_line(turn_idx: int, speaker: str, time_str: str, text: str) -> str:
    return f"[t{turn_idx} {speaker} {time_str}] {text}"


def build_passages(turns) -> list[dict]:
    """Group consecutive turns into ~250-350 word windows (never splitting a
    turn) with a 2-turn overlap. ``turns`` items need turn_idx, speaker,
    time_str, text, n_words (attribute or key access)."""
    def g(t, k):
        return t[k] if isinstance(t, (dict, sqlite3.Row)) else getattr(t, k)

    n = len(turns)
    if n == 0:
        return []
    words = [g(t, "n_words") for t in turns]
    spans = []
    start = 0
    while start < n:
        total = 0
        end = start  # exclusive
        while end < n:
            w = words[end]
            if total >= PASSAGE_MIN_WORDS:
                break
            if total > 0 and total + w > PASSAGE_MAX_WORDS and total >= PASSAGE_MIN_WORDS * 0.6:
                break
            total += w
            end += 1
        if end < n and sum(words[end:]) < PASSAGE_MIN_TAIL_WORDS:
            end = n
        spans.append((start, end))
        if end >= n:
            break
        start = max(end - PASSAGE_OVERLAP_TURNS, start + 1)

    out = []
    for k, (s, e) in enumerate(spans):
        chunk = turns[s:e]
        out.append({
            "first_turn": g(chunk[0], "turn_idx"),
            "last_turn": g(chunk[-1], "turn_idx"),
            "n_words": sum(words[s:e]),
            "text_display": "\n".join(display_line(g(t, "turn_idx"), g(t, "speaker"), g(t, "time_str"), g(t, "text")) for t in chunk),
            "text_norm": "\n".join(normalize(g(t, "text")) for t in chunk),
            "seq": k,
        })
    return out


def _recompute_team_ords(conn, team: str) -> None:
    rows = conn.execute("SELECT meeting_id FROM meetings WHERE team=? ORDER BY letter", (team,)).fetchall()
    for i, r in enumerate(rows, 1):
        conn.execute("UPDATE meetings SET ord=? WHERE meeting_id=? AND (ord IS NOT ? )", (i, r[0], i))
        conn.execute("UPDATE passages SET ord=? WHERE meeting_id=? AND (ord IS NOT ?)", (i, r[0], i))
        conn.execute("UPDATE events SET ord=? WHERE meeting_id=? AND (ord IS NOT ?)", (i, r[0], i))


def ingest_meeting(conn: sqlite3.Connection, pm: ParsedMeeting, source_path: str | None = None) -> dict:
    """Replace everything stored for ``pm.meeting_id`` (turns, passages,
    regex speaker rows) in one transaction. Idempotent."""
    from . import speakers as speakers_mod  # local import: speakers imports store

    mid, team = pm.meeting_id, pm.team
    passages = build_passages(pm.turns)
    with conn:
        conn.execute("DELETE FROM passages WHERE meeting_id=?", (mid,))  # fires FTS delete trigger
        conn.execute("DELETE FROM turns WHERE meeting_id=?", (mid,))
        conn.execute("DELETE FROM speakers WHERE meeting_id=? AND source='regex'", (mid,))
        conn.execute(
            """INSERT INTO meetings(meeting_id, team, letter, ord, n_turns, n_words, header_number,
                                    source_path, parse_problems, ingested_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(meeting_id) DO UPDATE SET team=excluded.team, letter=excluded.letter,
                 n_turns=excluded.n_turns, n_words=excluded.n_words, header_number=excluded.header_number,
                 source_path=excluded.source_path, parse_problems=excluded.parse_problems,
                 ingested_at=excluded.ingested_at""",
            (mid, team, pm.letter, None, len(pm.turns), pm.n_words, pm.header.get("meeting number"),
             source_path, "\n".join(pm.problems) or None, time.time()))
        conn.executemany(
            "INSERT INTO turns VALUES (?,?,?,?,?,?,?)",
            [(mid, t.turn_idx, t.speaker, t.start_sec, t.time_str, t.text, t.n_words) for t in pm.turns])
        scope = scope_tokens(team, mid)
        conn.executemany(
            """INSERT INTO passages(passage_id, meeting_id, team, ord, first_turn, last_turn, n_words,
                                    text_display, text_norm, scope) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [(f"{mid}:p{p['seq']:03d}", mid, team, None, p["first_turn"], p["last_turn"], p["n_words"],
              p["text_display"], p["text_norm"], scope) for p in passages])
        n_spk = speakers_mod.store_regex_speakers(conn, team, mid, pm.turns)
        _recompute_team_ords(conn, team)
    return {"meeting_id": mid, "turns": len(pm.turns), "words": pm.n_words,
            "passages": len(passages), "speaker_rows": n_spk, "problems": pm.problems}


def ingest_file(conn: sqlite3.Connection, path: str) -> dict:
    return ingest_meeting(conn, parse_file(path), source_path=os.path.abspath(path))


def team_meetings(conn: sqlite3.Connection, team: str) -> list[dict]:
    """A team's meetings in order: [{meeting_id, letter, ord, n_turns, n_words}, ...]."""
    rows = conn.execute(
        "SELECT meeting_id, letter, ord, n_turns, n_words FROM meetings WHERE team=? ORDER BY letter",
        (team,)).fetchall()
    return [dict(r) for r in rows]


def last_meeting(conn: sqlite3.Connection, team: str) -> dict | None:
    ms = team_meetings(conn, team)
    return ms[-1] if ms else None


def meeting_ord(conn: sqlite3.Connection, meeting_id: str) -> int | None:
    r = conn.execute("SELECT ord FROM meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
    return r[0] if r else None
