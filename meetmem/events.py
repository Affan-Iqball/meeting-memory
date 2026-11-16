"""Event ledger: the "memory over time" layer.

Division of labour
------------------
A cheap LLM reads one meeting PART and submits EVENTS that point at turns
(what happened, kind, topic, who). This module does everything else: it
validates each pointer, copies the evidence quote from the turn (a model never
types a quote), stores events per (meeting, part) with atomic replacement,
and derives per-topic status / reversals as of any meeting.

Tables (created idempotently by ``migrate``, which ``store.connect`` calls)::

    topics(topic_id, team, slug, label, aliases_json, first_meeting_id, updated_at)
    events(event_id, team, meeting_id, ord, part, at_turn, start_sec, topic_slug, kind,
           summary, speaker, owner, value, evidence_turn, evidence_quote,
           resolves_event_id, source, created_at)
    events_fts / topics_fts      FTS5 over normalised text, with a team/meeting scope column
    extraction_runs(meeting_id, part, source, n_submitted, n_accepted, n_rejected, n_warnings, submitted_at)
    answers(run, question_id, team, answer, citations_json, submitted_at, source)
    briefs(run, team, as_of_ord, text, sections_json, citations_json, submitted_at, source)
    speakers  (from store) + column ``part``; this module writes source='llm' rows

Status rules (``derive_status``)
--------------------------------
* status-bearing kinds: proposed, decided, rejected, reinstated, deferred.
  ``status`` = kind of the LATEST status-bearing event (order: ord, at_turn, event_id).
* each status kind has a side: decided/reinstated -> "in", rejected -> "out",
  proposed/deferred -> neutral (no side).
* ``firm`` = side of the latest NON-neutral event (None if there is none yet).
* ``state`` = "in"/"out" when the latest status event is firm; otherwise
  "open" (a proposal or deferral is pending; ``firm`` still tells what was
  last settled, e.g. TS3004b 31:19 "reopened" after a skip is open/firm=out).
* a REVERSAL is a change of side between consecutive non-neutral events
  (in -> out or out -> in). Neutral events never count, so "proposed, then
  decided" is 0 reversals, and "rejected, reopened (proposed), rejected" is
  0 reversals but 1 reopening. ``reopenings`` counts neutral events that
  follow an "out". ``last_reversal_meeting`` = meeting of the latest reversal.

Event ids are integers (AUTOINCREMENT, never reused). Resubmitting a part
keeps the id of every event whose natural key (meeting, topic, kind,
at_turn) is unchanged, so ``resolves`` links from later meetings survive.
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict

from . import cite
from .normalize import normalize

STATUS_KINDS = ("proposed", "decided", "rejected", "reinstated", "deferred")
OTHER_KINDS = ("objection", "number", "action_assigned", "action_reported", "requirement", "info")
KINDS = STATUS_KINDS + OTHER_KINDS
IN_KINDS = {"decided", "reinstated"}
OUT_KINDS = {"rejected"}

KIND_DEFS = {
    "proposed": "someone suggests an option, or an earlier dropped option is reopened for discussion",
    "decided": "the team agrees to include / choose something (often a short turn: \"Okay, we'll take the yellow.\")",
    "rejected": "the team drops or rules something out (\"we skip speech recognition\")",
    "reinstated": "something earlier rejected is put back in (\"can we put speech recognition in as well?\")",
    "deferred": "explicitly left open / pending until later or until some information arrives",
    "objection": "someone argues against an option (speaker = who objects)",
    "number": "a cost, price, budget or quantity figure; put the figure in value with digits (\"12.5 EUR\")",
    "action_assigned": "someone commits to or is asked to check / find out / prepare something (owner = who)",
    "action_reported": "someone reports back on an earlier commitment (set resolves to its event id)",
    "requirement": "a constraint handed to the team (new project requirements, management e-mail, budget limit)",
    "info": "a finding or fact reported to the team (research result, user survey)",
}
KIND_SYNONYMS = {
    "decision": "decided", "decide": "decided", "agreed": "decided", "accepted": "decided", "chosen": "decided",
    "proposal": "proposed", "propose": "proposed", "suggested": "proposed", "reopened": "proposed",
    "dropped": "rejected", "reject": "rejected", "removed": "rejected", "ruled_out": "rejected",
    "reversed": "reinstated", "restored": "reinstated", "pending": "deferred", "postponed": "deferred",
    "cost": "number", "price": "number", "figure": "number", "budget": "number",
    "action": "action_assigned", "todo": "action_assigned", "task": "action_assigned",
    "report": "action_reported", "followup": "action_reported", "follow_up": "action_reported",
    "constraint": "requirement", "finding": "info", "fact": "info",
}

SPLIT_THRESHOLD_WORDS = 4500
PART_TARGET_WORDS = 3500
PART_OVERLAP_TURNS = 6
QUOTE_MIN = cite.MIN_WORDS
QUOTE_MAX = 30
SUMMARY_MAX_WORDS = 25
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LETTER_RE = re.compile(r"^[A-Z]$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    topic_id     INTEGER PRIMARY KEY,
    team         TEXT NOT NULL,
    slug         TEXT NOT NULL,
    label        TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    first_meeting_id TEXT,
    updated_at   REAL,
    UNIQUE(team, slug)
);
CREATE VIRTUAL TABLE IF NOT EXISTS topics_fts USING fts5(
    text_norm, scope, tokenize="porter unicode61 tokenchars '.'");

CREATE TABLE IF NOT EXISTS events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    team          TEXT NOT NULL,
    meeting_id    TEXT NOT NULL,
    ord           INTEGER,
    part          INTEGER NOT NULL DEFAULT 1,
    at_turn       INTEGER NOT NULL,
    start_sec     INTEGER,
    topic_slug    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    summary       TEXT NOT NULL,
    speaker       TEXT,
    owner         TEXT,
    value         TEXT,
    evidence_turn INTEGER NOT NULL,
    evidence_quote TEXT NOT NULL,
    resolves_event_id INTEGER,
    source        TEXT,
    created_at    REAL
);
CREATE INDEX IF NOT EXISTS events_team ON events(team, ord, at_turn);
CREATE INDEX IF NOT EXISTS events_meeting ON events(meeting_id, part);
CREATE INDEX IF NOT EXISTS events_topic ON events(team, topic_slug);
CREATE INDEX IF NOT EXISTS events_resolves ON events(resolves_event_id);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    text_norm, scope, tokenize="porter unicode61 tokenchars '.'");

CREATE TABLE IF NOT EXISTS extraction_runs (
    meeting_id   TEXT NOT NULL,
    part         INTEGER NOT NULL,
    source       TEXT,
    n_submitted  INTEGER,
    n_accepted   INTEGER,
    n_rejected   INTEGER,
    n_warnings   INTEGER,
    submitted_at REAL,
    PRIMARY KEY (meeting_id, part)
);

CREATE TABLE IF NOT EXISTS answers (
    run          TEXT NOT NULL,
    question_id  TEXT NOT NULL,
    team         TEXT NOT NULL,
    answer       TEXT NOT NULL,
    citations_json TEXT NOT NULL,
    submitted_at REAL,
    source       TEXT,
    PRIMARY KEY (run, question_id)
);
CREATE TABLE IF NOT EXISTS briefs (
    run          TEXT NOT NULL,
    team         TEXT NOT NULL,
    as_of_ord    INTEGER NOT NULL,
    text         TEXT NOT NULL,
    sections_json TEXT NOT NULL,
    citations_json TEXT NOT NULL,
    submitted_at REAL,
    source       TEXT,
    PRIMARY KEY (run, team)
);
"""


class SubmitError(ValueError):
    """Hard error: the whole submission is refused (nothing stored)."""


def migrate(conn) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(speakers)")}
    if cols and "part" not in cols:
        conn.execute("ALTER TABLE speakers ADD COLUMN part INTEGER")
    if cols and "name_evidence_turn_idx" not in cols:
        conn.execute("ALTER TABLE speakers ADD COLUMN name_evidence_turn_idx INTEGER")


# --------------------------------------------------------------------------- meetings / parts

def meeting_info(conn, meeting_id: str) -> dict:
    r = conn.execute("SELECT meeting_id, team, letter, ord, n_turns, n_words FROM meetings WHERE meeting_id=?",
                     (meeting_id,)).fetchone()
    if r is None:
        raise SubmitError(f"unknown meeting {meeting_id!r}")
    return dict(r)


def split_parts(turns) -> list[dict]:
    """Pure: [(turn_idx, n_words), ...] -> parts. One part if the meeting has
    <= SPLIT_THRESHOLD_WORDS words; otherwise k = ceil(words / PART_TARGET_WORDS)
    balanced parts cut on turn boundaries, each later part starting
    PART_OVERLAP_TURNS turns before the previous part's end."""
    total = sum(w for _, w in turns)
    if not turns:
        return []
    if total <= SPLIT_THRESHOLD_WORDS:
        return [{"part": 1, "first_turn": turns[0][0], "last_turn": turns[-1][0], "n_words": total}]
    k = -(-total // PART_TARGET_WORDS)
    target = total / k
    cuts, acc, start = [], 0, 0
    for i, (_, w) in enumerate(turns):
        acc += w
        if acc >= target * (len(cuts) + 1) and len(cuts) < k - 1 and i < len(turns) - 1:
            cuts.append(i)
    spans, s = [], 0
    for c in cuts:
        spans.append((s, c))
        s = max(c + 1 - PART_OVERLAP_TURNS, s + 1)
    spans.append((s, len(turns) - 1))
    return [{"part": n, "first_turn": turns[a][0], "last_turn": turns[b][0],
             "n_words": sum(w for _, w in turns[a:b + 1])} for n, (a, b) in enumerate(spans, 1)]


def meeting_parts(conn, meeting_id: str) -> list[dict]:
    rows = conn.execute("SELECT turn_idx, n_words FROM turns WHERE meeting_id=? ORDER BY turn_idx",
                        (meeting_id,)).fetchall()
    return split_parts([(r[0], r[1]) for r in rows])


def get_part(conn, meeting_id: str, part: int) -> dict:
    parts = meeting_parts(conn, meeting_id)
    for p in parts:
        if p["part"] == int(part):
            return p
    raise SubmitError(f"{meeting_id} has {len(parts)} part(s); part {part} does not exist")


# --------------------------------------------------------------------------- evidence

_END = re.compile(r"[.?!]['\")]*$")


def _short_turn_suggestions(conn, meeting_id, turn_idx, lo=None, hi=None, limit=6):
    row = conn.execute("SELECT speaker FROM turns WHERE meeting_id=? AND turn_idx=?", (meeting_id, turn_idx)).fetchone()
    spk = row[0] if row else None
    lo = turn_idx - 15 if lo is None else max(lo, turn_idx - 15)
    hi = turn_idx + 15 if hi is None else min(hi, turn_idx + 15)
    rows = conn.execute(
        """SELECT turn_idx, speaker, n_words, text FROM turns WHERE meeting_id=? AND n_words>=? AND turn_idx BETWEEN ? AND ?
           AND (speaker=? OR abs(turn_idx-?)<=3) ORDER BY abs(turn_idx-?), turn_idx LIMIT ?""",
        (meeting_id, QUOTE_MIN, lo, hi, spk, turn_idx, turn_idx, limit)).fetchall()
    return [{"turn_idx": r[0], "speaker": r[1], "n_words": r[2], "text": r[3][:140]} for r in rows]


def _window(words, ws, we):
    """Choose [s, e) containing [ws, we): the sentence(s) around it if that is
    6..QUOTE_MAX words, otherwise a centred window clipped to QUOTE_MAX."""
    n = len(words)
    if we - ws >= QUOTE_MAX:
        return ws, ws + QUOTE_MAX
    s = ws
    while s > 0 and not _END.search(words[s - 1]):
        s -= 1
    e = we
    while e < n and not _END.search(words[e - 1]):
        e += 1
    if e - s > QUOTE_MAX:  # sentence too long: centre a window on the match, inside the sentence
        room = QUOTE_MAX - (we - ws)
        s = max(s, ws - room // 2)
        e = min(e, s + QUOTE_MAX)
        s = max(0, min(s, e - QUOTE_MAX)) if e - s < QUOTE_MAX else s
    # too short: grow by whole sentences (then words), right side first
    while e - s < QUOTE_MIN and (s > 0 or e < n):
        if e < n:
            e2 = e + 1
            while e2 < n and not _END.search(words[e2 - 1]) and e2 - s < QUOTE_MAX:
                e2 += 1
            e = e2
        else:
            s2 = s - 1
            while s2 > 0 and not _END.search(words[s2 - 1]) and e - s2 < QUOTE_MAX:
                s2 -= 1
            s = s2
    return s, min(e, s + QUOTE_MAX)


def resolve_evidence(conn, meeting_id: str, turn_idx: int, contains: str | None = None,
                     lo: int | None = None, hi: int | None = None):
    """Return (quote, warnings). Raises cite.QuoteError (with .suggestions) if
    the turn does not exist or has fewer than 6 words. The quote is copied
    from the stored turn text and always passes cite.verify_quote."""
    row = conn.execute("SELECT speaker, text FROM turns WHERE meeting_id=? AND turn_idx=?",
                       (meeting_id, int(turn_idx))).fetchone()
    if row is None:
        raise cite.QuoteError(f"no turn {turn_idx} in meeting {meeting_id}")
    words = row["text"].split()
    warnings = []
    if len(words) < QUOTE_MIN:
        sugg = _short_turn_suggestions(conn, meeting_id, int(turn_idx), lo, hi)
        listing = "; ".join(f"t{x['turn_idx']} {x['speaker']} ({x['n_words']}w): {x['text'][:70]!r}" for x in sugg) or "none"
        raise cite.QuoteError(
            f"t{turn_idx} has only {len(words)} words ({row['text']!r}); evidence needs a turn with >= {QUOTE_MIN} words. "
            f"Keep it as at_turn and point evidence at a nearby turn: {listing}", sugg)
    ws, we = 0, min(len(words), QUOTE_MAX)
    matched = False
    c = cite.normalize_for_match(contains or "")
    if c:
        lw = [w.lower() for w in words]
        joined = " ".join(lw)
        pos = joined.find(c)
        if pos >= 0:
            offs, acc = [], 0
            for w in lw:
                offs.append(acc)
                acc += len(w) + 1
            ws = max(i for i, o in enumerate(offs) if o <= pos)
            we = max(i for i, o in enumerate(offs) if o <= pos + len(c) - 1) + 1
            matched = True
        else:
            warnings.append(f"'contains' {contains!r} not found in t{turn_idx}; used the turn's first words instead")
    if matched:
        s, e = _window(words, ws, we)
    else:
        s, e = 0, min(len(words), QUOTE_MAX)
    quote = " ".join(words[s:e])
    ok, idx, reason = cite.verify_quote(meeting_id, quote, conn=conn)
    assert ok, (meeting_id, turn_idx, quote, reason)
    return quote, warnings


# --------------------------------------------------------------------------- speakers

def _speaker_rows(conn, team, before=None, as_of_ord=None):
    sql = """SELECT s.*, m.ord FROM speakers s JOIN meetings m ON m.meeting_id=s.meeting_id WHERE s.team=?"""
    args = [team]
    if as_of_ord is not None:
        sql += " AND m.ord <= ?"; args.append(int(as_of_ord))
    if before is not None:  # (ord, meeting_id, part): rows strictly before this part
        o, mid, part = before
        sql += " AND (m.ord < ? OR (s.meeting_id = ? AND s.source='llm' AND coalesce(s.part,1) < ?))"
        args += [o, mid, part]
    return [dict(r) for r in conn.execute(sql + " ORDER BY m.ord, s.speaker, s.source", args)]


def same_person(a: str | None, b: str | None) -> bool:
    """True if two stated names are the same person: equal ignoring case, or
    one is the leading word(s) of the other ("Paul" ~ "Paul Wiezer")."""
    if not a or not b:
        return False
    ta, tb = a.lower().split(), b.lower().split()
    if not ta or not tb:
        return False
    short, long_ = sorted((ta, tb), key=len)
    return long_[:len(short)] == short


def group_names(names) -> dict:
    """{name: iterable_of_meetings} -> {canonical (longest variant): set(variants)}.
    A short name joins a longer one only if exactly ONE longer group matches
    ("Paul" with both "Paul Wiezer" and "Paul Smith" stays on its own)."""
    groups = {}
    for n in sorted(set(names), key=lambda s: (-len(s.split()), -len(s), s.lower())):
        hits = [c for c in groups if same_person(c, n)]
        if len(hits) == 1:
            groups[hits[0]].add(n)
        else:
            groups[n] = {n}
    return groups


def speaker_table(conn, team: str, as_of_ord=None, before=None, merge_variants=True) -> dict:
    """letter -> {name, role, names{name:[mtg..]}, roles{...}, per_meeting{mtg:[{source,name,role,turn}]},
    conflicts[str]}. The chosen name/role is the one seen in most meetings
    (regex wins ties); disagreements are listed in ``conflicts``, never hidden.
    With ``merge_variants`` (read path) a first name and a longer name starting
    with it count as one person, shown by the longer form; ``names`` always
    keeps the raw variants (the submit path maps owners through it)."""
    out = {}
    for r in _speaker_rows(conn, team, before=before, as_of_ord=as_of_ord):
        d = out.setdefault(r["speaker"], {"name": None, "role": None, "names": defaultdict(set),
                                          "roles": defaultdict(set), "per_meeting": defaultdict(list),
                                          "conflicts": [], "_src": defaultdict(set)})
        if r["name"]:
            d["names"][r["name"]].add(r["meeting_id"]); d["_src"][("n", r["name"])].add(r["source"])
        if r["role"]:
            d["roles"][r["role"]].add(r["meeting_id"]); d["_src"][("r", r["role"])].add(r["source"])
        d["per_meeting"][r["meeting_id"]].append({
            "source": r["source"], "name": r["name"], "role": r["role"],
            "turn": r["name_evidence_turn_idx"] if r["name"] and not r["role"] else r["evidence_turn_idx"]})
    for letter, d in out.items():
        for kind, field in (("n", "names"), ("r", "roles")):
            vals = d[field]
            if not vals:
                continue
            if kind == "n" and merge_variants:
                merged, srcs = {}, {}
                for canon, variants in group_names(vals).items():
                    merged[canon] = set().union(*(vals[v] for v in variants))
                    srcs[canon] = set().union(*(d["_src"][("n", v)] for v in variants))
                vals = merged
                for canon, s in srcs.items():
                    d["_src"][("n", canon)] = s
            best = sorted(vals, key=lambda v: (-len(vals[v]), "regex" not in d["_src"][(kind, v)], v.lower()))
            d["name" if kind == "n" else "role"] = best[0]
            distinct = {v.lower() for v in vals}
            if len(distinct) > 1:
                d["conflicts"].append(f"{field[:-1]} differs: " + "; ".join(
                    f"{v} ({', '.join(sorted(vals[v]))})" for v in best))
        d["names"] = {k: sorted(v) for k, v in d["names"].items()}
        d["roles"] = {k: sorted(v) for k, v in d["roles"].items()}
        d["per_meeting"] = dict(d["per_meeting"])
        del d["_src"]
    # one name claimed by two letters
    name_letters = defaultdict(set)
    for letter, d in out.items():
        for n in d["names"]:
            name_letters[n.lower()].add(letter)
    for n, ls in name_letters.items():
        if len(ls) > 1:
            for letter in ls:
                out[letter]["conflicts"].append(f"name {n!r} also given to {', '.join(sorted(ls - {letter}))}")
    return out


def speaker_label(table: dict, letter: str | None) -> str:
    if not letter:
        return "?"
    d = table.get(letter)
    if not d:
        return letter
    bits = [b for b in (d["name"], d["role"]) if b]
    flag = " !conflict" if d["conflicts"] else ""
    return f"{letter} ({', '.join(bits)}{flag})" if bits else letter


class SpeakerView:
    """Read-path speaker resolution PER MEETING. A letter is only a seat: the
    same letter can be a different person (or change role) in another meeting
    of the team, so every label is resolved for the meeting it is printed in.

    cell(meeting_id, letter) -> {"name", "role", "sources": [...], "turns": [...]} or None
    label(meeting_id, letter) -> "B (May, marketing expert)"            stated in that meeting
                                 "B (Julian?, from other meetings)"      nothing stated there
                                 "B (industrial designer; elsewhere B = user interface designer in IS1001b)"
    """

    def __init__(self, conn, team, as_of_ord=None):
        self.team = team
        sql = "SELECT meeting_id, ord FROM meetings WHERE team=?"
        args = [team]
        if as_of_ord is not None:
            sql += " AND ord <= ?"; args.append(int(as_of_ord))
        self.meetings = [r[0] for r in conn.execute(sql + " ORDER BY ord", args)]
        rows = _speaker_rows(conn, team, as_of_ord=as_of_ord)
        # team-level name groups per letter, so "Paul" in one meeting shows as "Paul Wiezer"
        names_by_letter = defaultdict(set)
        for r in rows:
            if r["name"]:
                names_by_letter[r["speaker"]].add(r["name"])
        self._groups = {l: group_names(ns) for l, ns in names_by_letter.items()}
        raw = defaultdict(list)
        for r in rows:
            raw[(r["meeting_id"], r["speaker"])].append(r)
        self.cells = {}
        for key, rs in raw.items():
            letter = key[1]
            names = []
            for r in rs:
                if r["name"]:
                    c = self.canonical(letter, r["name"])
                    if c not in names:
                        names.append(c)
            roles = []
            for r in sorted(rs, key=lambda r: r["source"] != "regex"):  # self-stated regex role first
                if r["role"] and r["role"].lower() not in [x.lower() for x in roles]:
                    roles.append(r["role"])
            turns = sorted({t for r in rs for t in (r["evidence_turn_idx"], r["name_evidence_turn_idx"]) if t is not None})
            self.cells[key] = {"name": " / ".join(names) or None, "role": " / ".join(roles) or None,
                               "names": names, "roles": roles,
                               "sources": sorted({r["source"] for r in rs}), "turns": turns}
        self.letters = sorted({k[1] for k in self.cells})

    def canonical(self, letter, name):
        for canon, variants in self._groups.get(letter, {}).items():
            if name in variants:
                return canon
        return name

    def cell(self, meeting_id, letter):
        return self.cells.get((meeting_id, letter))

    def _others(self, meeting_id, letter, field, compatible=None):
        """[(value, meeting_id)] stated for ``letter`` in OTHER meetings, optionally only
        meetings whose cell passes ``compatible(cell)``."""
        out = []
        for mid in self.meetings:
            c = self.cells.get((mid, letter))
            if mid == meeting_id or not c or not c[field]:
                continue
            if compatible and not compatible(c):
                continue
            out.append((c[field], mid))
        return out

    @staticmethod
    def _distinct(pairs, same=lambda a, b: a.lower() == b.lower()):
        vals = []
        for v, _ in pairs:
            if not any(same(v, x) for x in vals):
                vals.append(v)
        return vals

    def conflicts(self, letter) -> list[str]:
        """Disagreements between meetings for one letter (shown, never merged)."""
        out = []
        for field in ("name", "role"):
            pairs = [(c[field], mid) for mid in self.meetings
                     for c in [self.cells.get((mid, letter))] if c and c[field]]
            vals = self._distinct(pairs, same_person if field == "name" else (lambda a, b: a.lower() == b.lower()))
            if len(vals) > 1:
                out.append(f"{field} differs: " + "; ".join(
                    f"{v} ({', '.join(m for x, m in pairs if (same_person(x, v) if field == 'name' else x.lower() == v.lower()))})"
                    for v in vals))
        return out

    def label(self, meeting_id, letter) -> str:
        if not letter:
            return "?"
        c = self.cells.get((meeting_id, letter))
        name, role = (c["name"], c["role"]) if c else (None, None)
        fallback = False
        bits = []
        if name:
            bits.append(name)
        else:
            cands = self._distinct(self._others(meeting_id, letter, "name",
                                                (lambda o: not o["role"] or not role or o["role"].lower() == role.lower())),
                                   same_person)
            if cands:
                bits.append(" or ".join(cands) + "?"); fallback = True
        if role:
            bits.append(role)
        else:
            cands = self._distinct(self._others(meeting_id, letter, "role",
                                                (lambda o: not o["name"] or not name or same_person(o["name"], name))))
            if cands:
                bits.append(" or ".join(cands) + "?"); fallback = True
        if not bits:
            return letter
        s = ", ".join(bits) + (", from other meetings" if fallback else "")
        if c:  # stated here: say so if the same letter is someone/something else elsewhere
            diff = []
            for field, same in (("name", same_person), ("role", lambda a, b: a.lower() == b.lower())):
                if not c[field]:
                    continue
                for v, mid in self._others(meeting_id, letter, field):
                    if not same(v, c[field]):
                        diff.append(f"{v} in {mid}")
            if diff:
                s += f"; elsewhere {letter} = " + ", ".join(diff)
        return f"{letter} ({s})"

    def grid(self) -> dict:
        return {"team": self.team, "meetings": self.meetings,
                "letters": {l: {"per_meeting": {mid: self.cells.get((mid, l)) for mid in self.meetings},
                                "conflicts": self.conflicts(l)} for l in self.letters}}

    def grid_text(self) -> str:
        if not self.letters:
            return f"no speaker information for {self.team}"
        lines = [f"Speakers of {self.team}, per meeting (a letter is a seat, not a person: read each meeting's own column).",
                 "Cell = what that meeting states [source turn ids]; '-' = nothing stated there.", ""]
        cols = []
        for l in self.letters:
            row = [l]
            for mid in self.meetings:
                c = self.cells.get((mid, l))
                if not c:
                    row.append("-")
                else:
                    v = ", ".join(x for x in (c["name"], c["role"]) if x)
                    row.append(v + (f" [t{','.join(map(str, c['turns'][:3]))}]" if c["turns"] else ""))
            cols.append(row)
        head = ["   "] + self.meetings
        widths = [max(len(r[i]) for r in cols + [head]) for i in range(len(head))]
        fmt = lambda r: "  ".join(x.ljust(w) for x, w in zip(r, widths)).rstrip()
        lines.append(fmt(head))
        lines += [fmt(r) for r in cols]
        notes = [f"  {l}: {c}" for l in self.letters for c in self.conflicts(l)]
        lines.append("")
        lines.append("Differences between meetings (NOT merged; in each meeting use that meeting's value):" if notes
                     else "No differences between meetings.")
        lines += notes
        return "\n".join(lines)


# --------------------------------------------------------------------------- topics

def _scope(team, mid=None):
    s = f"team{team.lower()}"
    return s + (f" mtg{mid.lower()}" if mid else "")


def _topic_fts_refresh(conn, topic_id, team, slug, label, aliases):
    conn.execute("DELETE FROM topics_fts WHERE rowid=?", (topic_id,))
    conn.execute("INSERT INTO topics_fts(rowid, text_norm, scope) VALUES (?,?,?)",
                 (topic_id, normalize(" ".join([slug.replace("-", " "), label] + list(aliases))), _scope(team)))


def upsert_topic(conn, team, slug, label=None, aliases=(), meeting_id=None):
    r = conn.execute("SELECT topic_id, label, aliases_json FROM topics WHERE team=? AND slug=?", (team, slug)).fetchone()
    if r is None:
        label = label or slug.replace("-", " ")
        al = sorted({a.strip() for a in aliases if a and a.strip()}, key=str.lower)
        cur = conn.execute("INSERT INTO topics(team, slug, label, aliases_json, first_meeting_id, updated_at) VALUES (?,?,?,?,?,?)",
                           (team, slug, label, json.dumps(al), meeting_id, time.time()))
        _topic_fts_refresh(conn, cur.lastrowid, team, slug, label, al)
        return
    old = json.loads(r["aliases_json"])
    new_label = r["label"]
    if label and new_label == slug.replace("-", " "):
        new_label = label
    al = sorted({a.strip() for a in list(old) + list(aliases) if a and a.strip()} - {new_label}, key=str.lower)
    if al != old or new_label != r["label"]:
        conn.execute("UPDATE topics SET label=?, aliases_json=?, updated_at=? WHERE topic_id=?",
                     (new_label, json.dumps(al), time.time(), r["topic_id"]))
        _topic_fts_refresh(conn, r["topic_id"], team, slug, new_label, al)


def team_topics(conn, team) -> dict:
    return {r["slug"]: {"slug": r["slug"], "label": r["label"], "aliases": json.loads(r["aliases_json"]),
                        "first_meeting_id": r["first_meeting_id"]}
            for r in conn.execute("SELECT * FROM topics WHERE team=? ORDER BY slug", (team,))}


def _slugify(s):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(s).lower())).strip("-")


# --------------------------------------------------------------------------- reading events / status

def team_events(conn, team, as_of_ord=None, topic=None, kinds=None, before=None, meeting_id=None) -> list[dict]:
    """Events of one team in true order (ord, at_turn, event_id). ``as_of_ord``
    is a hard SQL cutoff. ``before=(ord, meeting_id, part)`` = only events
    stored before that part (used for prompt context)."""
    sql, args = "SELECT * FROM events WHERE team=?", [team]
    if as_of_ord is not None:
        sql += " AND ord <= ?"; args.append(int(as_of_ord))
    if before is not None:
        o, mid, part = before
        sql += " AND (ord < ? OR (meeting_id=? AND part < ?))"; args += [o, mid, part]
    if topic:
        sql += " AND topic_slug=?"; args.append(topic)
    if meeting_id:
        sql += " AND meeting_id=?"; args.append(meeting_id)
    if kinds:
        kinds = list(kinds)
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"; args += kinds
    return [dict(r) for r in conn.execute(sql + " ORDER BY ord, at_turn, event_id", args)]


def side(kind):
    return "in" if kind in IN_KINDS else "out" if kind in OUT_KINDS else None


def derive_status(events) -> dict:
    """events: chronological events of ONE topic. See module docstring."""
    seq = [e for e in events if e["kind"] in STATUS_KINDS]
    firm, reversals, last_rev, reopen = None, 0, None, 0
    for e in seq:
        s = side(e["kind"])
        if s is None:
            if firm == "out":
                reopen += 1
            continue
        if firm is not None and s != firm:
            reversals += 1
            last_rev = e
        firm = s
    status = seq[-1]["kind"] if seq else None
    state = side(status) if status else None
    if status and state is None:
        state = "open"
    return {"status": status, "state": state, "firm": firm, "reversals": reversals,
            "reopenings": reopen,
            "last_reversal_meeting": last_rev["meeting_id"] if last_rev else None,
            "last_reversal_event": last_rev["event_id"] if last_rev else None,
            "status_event": seq[-1]["event_id"] if seq else None,
            "sequence": [{"meeting_id": e["meeting_id"], "kind": e["kind"], "at_turn": e["at_turn"],
                          "event_id": e["event_id"]} for e in seq],
            "n_events": len(events)}


def topic_statuses(conn, team, as_of_ord=None, before=None) -> dict:
    by = defaultdict(list)
    for e in team_events(conn, team, as_of_ord=as_of_ord, before=before):
        by[e["topic_slug"]].append(e)
    return {slug: derive_status(evs) for slug, evs in by.items()}


def resolved_ids(conn, team, as_of_ord=None, before=None) -> dict:
    """event_id -> [resolving events] (subject to the same cutoff)."""
    out = defaultdict(list)
    for e in team_events(conn, team, as_of_ord=as_of_ord, before=before):
        if e["resolves_event_id"] is not None:
            out[e["resolves_event_id"]].append(e)
    return out


# --------------------------------------------------------------------------- submit

def _compact(item, n=160):
    s = json.dumps(item, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "..."


def _as_int(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, str) and re.fullmatch(r"t?\d+", x.strip()):
        return int(x.strip().lstrip("t"))
    return None


def submit_events(conn, meeting_id: str, data, part: int = 1, source: str = "unknown") -> dict:
    """Validate + store one (meeting, part) submission; replaces the previous
    submission for that (meeting, part) in one transaction. Raises
    SubmitError only for whole-file problems; item problems are reported in
    ``rejected`` while the valid items are stored."""
    m = meeting_info(conn, meeting_id)
    team, ord_ = m["team"], m["ord"]
    p = get_part(conn, meeting_id, part)
    lo, hi = p["first_turn"], p["last_turn"]
    if not isinstance(data, dict):
        raise SubmitError("top level must be a JSON object with keys speakers, topics, events")
    rep = {"meeting_id": meeting_id, "part": int(part), "turn_range": [lo, hi], "source": source,
           "accepted": 0, "rejected": [], "warnings": [], "stored_event_ids": [], "stored": []}
    extra = set(data) - {"speakers", "topics", "events"}
    if extra:
        rep["warnings"].append(f"ignored top-level keys: {sorted(extra)}")
    for key in ("speakers", "topics", "events"):
        if data.get(key) is not None and not isinstance(data.get(key), list):
            raise SubmitError(f"'{key}' must be a list")
    turns = {r["turn_idx"]: dict(r) for r in conn.execute(
        "SELECT turn_idx, speaker, start_sec, n_words, text FROM turns WHERE meeting_id=?", (meeting_id,))}
    letters = sorted({t["speaker"] for t in turns.values()})

    def in_part(i):
        return i is not None and lo <= i <= hi

    def reject(section, idx, item, reasons, suggestions=None):
        r = {"section": section, "index": idx, "item": _compact(item), "reasons": reasons}
        if suggestions:
            r["suggestions"] = suggestions
        rep["rejected"].append(r)

    # ---- speakers
    spk_ok = []
    for i, s in enumerate(data.get("speakers") or []):
        if not isinstance(s, dict):
            reject("speakers", i, s, ["must be an object"]); continue
        reasons = []
        letter = str(s.get("speaker", "")).strip().upper()
        if letter not in letters:
            reasons.append(f"speaker {s.get('speaker')!r} is not a letter in this meeting ({', '.join(letters)})")
        ti = _as_int(s.get("turn_idx"))
        if not in_part(ti):
            reasons.append(f"turn_idx {s.get('turn_idx')!r} must be a turn in this part (t{lo}..t{hi}) that states the name/role")
        name = (s.get("name") or "").strip() or None
        role = (s.get("role") or "").strip().lower() or None
        if not name and not role:
            reasons.append("give a name or a role")
        if reasons:
            reject("speakers", i, s, reasons); continue
        spk_ok.append((letter, name, role, ti))

    # ---- topics
    existing = team_topics(conn, team)
    label_index = {}
    for slug, t in existing.items():
        for lab in [t["label"]] + t["aliases"]:
            label_index[normalize(lab)] = slug
    file_topics = {}
    for i, t in enumerate(data.get("topics") or []):
        if not isinstance(t, dict):
            reject("topics", i, t, ["must be an object"]); continue
        slug = str(t.get("slug", ""))
        if not SLUG_RE.match(slug):
            reject("topics", i, t, [f"slug {slug!r} must be lowercase-hyphen"],
                   [f"use {_slugify(slug)!r}"] if _slugify(slug) else None); continue
        aliases = t.get("aliases") or []
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            reject("topics", i, t, ["aliases must be a list of strings"]); continue
        label = str(t.get("label") or "").strip() or None
        if slug not in existing:
            for lab in [label or ""] + aliases:
                other = label_index.get(normalize(lab)) if lab else None
                if other and other != slug:
                    rep["warnings"].append(f"new topic {slug!r} has label/alias {lab!r} of existing topic {other!r}; "
                                           f"reuse {other!r} if it is the same thing")
                    break
        file_topics[slug] = (label, aliases)

    # ---- events
    table = speaker_table(conn, team, merge_variants=False)  # unchanged submit behaviour
    name_to_letter = {}
    for letter, d in table.items():
        for n in d["names"]:
            name_to_letter[n.lower()] = letter
        if d["role"]:
            name_to_letter[d["role"].lower()] = letter
    other_parts = {(r["topic_slug"], r["kind"], r["at_turn"]): (r["part"], r["event_id"]) for r in conn.execute(
        "SELECT topic_slug, kind, at_turn, part, event_id FROM events WHERE meeting_id=? AND part != ?", (meeting_id, part))}
    old_ids = {(r["topic_slug"], r["kind"], r["at_turn"]): r["event_id"] for r in conn.execute(
        "SELECT topic_slug, kind, at_turn, event_id FROM events WHERE meeting_id=? AND part = ?", (meeting_id, part))}
    seen, rows, new_topics = set(), [], {}
    events_in = data.get("events") or []
    for i, e in enumerate(events_in):
        if not isinstance(e, dict):
            reject("events", i, e, ["must be an object"]); continue
        reasons, sugg, warns = [], [], []
        topic = str(e.get("topic", "")).strip()
        if not SLUG_RE.match(topic):
            reasons.append(f"topic {topic!r} is not a lowercase-hyphen slug")
            if _slugify(topic):
                sugg.append(f"use topic {_slugify(topic)!r}")
        kind = str(e.get("kind", "")).strip()
        if kind not in KINDS:
            reasons.append(f"kind {kind!r} is not one of {', '.join(KINDS)}")
            k2 = KIND_SYNONYMS.get(kind.lower().replace(" ", "_").replace("-", "_"))
            if k2:
                sugg.append(f"use kind {k2!r}")
        summary = str(e.get("summary") or "").strip()
        if not summary:
            reasons.append("summary is empty")
        elif len(summary.split()) > SUMMARY_MAX_WORDS:
            warns.append(f"summary has {len(summary.split())} words (max {SUMMARY_MAX_WORDS})")
        at = _as_int(e.get("at_turn"))
        if at is None or at not in turns:
            reasons.append(f"at_turn {e.get('at_turn')!r} is not a turn id of {meeting_id}")
        elif not in_part(at):
            reasons.append(f"at_turn t{at} is outside this part (t{lo}..t{hi}); submit it with the part that contains it")
        ev = e.get("evidence")
        if ev is None:
            ev_turn, contains = at, None
        elif isinstance(ev, dict):
            ev_turn, contains = _as_int(ev.get("turn_idx")), ev.get("contains")
            if ev.get("turn_idx") is None:
                ev_turn = at
        else:
            ev_turn, contains = _as_int(ev), None
        if contains is not None and not isinstance(contains, str):
            contains = None
        quote = None
        if ev_turn is None or ev_turn not in turns:
            reasons.append(f"evidence.turn_idx {ev!r} is not a turn id of {meeting_id}")
        elif not in_part(ev_turn):
            reasons.append(f"evidence turn t{ev_turn} is outside this part (t{lo}..t{hi})")
        else:
            try:
                quote, w = resolve_evidence(conn, meeting_id, ev_turn, contains, lo, hi)
                warns += w
                if at is not None and abs(ev_turn - at) > 20:
                    warns.append(f"evidence t{ev_turn} is {abs(ev_turn - at)} turns away from at_turn t{at}")
            except cite.QuoteError as qe:
                reasons.append(f"evidence: {qe}")
                sugg += [f"evidence.turn_idx {s['turn_idx']} ({s['speaker']}: {s['text'][:70]!r})" for s in qe.suggestions]
        speaker = e.get("speaker")
        if speaker in (None, ""):
            speaker = turns[at]["speaker"] if at in turns else None
        else:
            speaker = str(speaker).strip().upper()
            if speaker not in letters:
                reasons.append(f"speaker {e.get('speaker')!r} is not a letter in this meeting ({', '.join(letters)})")
        owner = e.get("owner")
        if owner in (None, ""):
            owner = None
            if kind == "action_assigned":
                warns.append("action_assigned without owner")
        else:
            o = str(owner).strip()
            if o.upper() in letters and len(o) == 1:
                owner = o.upper()
            elif o.lower() in name_to_letter:
                owner = name_to_letter[o.lower()]
                warns.append(f"owner {o!r} mapped to speaker {owner} via the speaker table")
            else:
                reasons.append(f"owner {o!r} is not a speaker letter ({', '.join(letters)}) or a known name")
        value = e.get("value")
        value = None if value in (None, "") else str(value).strip()
        if kind == "number" and (not value or not re.search(r"\d", value)):
            warns.append("number event without a digit value (write e.g. \"12.5 EUR\")")
        res = e.get("resolves")
        res_id = None
        if res not in (None, "", 0):
            res_id = _as_int(str(res).lstrip("#"))
            tgt = conn.execute("SELECT event_id, team, meeting_id, ord, part, at_turn FROM events WHERE event_id=?",
                               (res_id,)).fetchone() if res_id is not None else None
            if tgt is None:
                reasons.append(f"resolves {res!r}: no such event id")
                sugg.append("use an id from OPEN ITEMS in the prompt context, or null")
            elif tgt["team"] != team:
                reasons.append(f"resolves {res_id}: that event belongs to team {tgt['team']}")
            elif not (tgt["ord"] < ord_ or (tgt["meeting_id"] == meeting_id and at is not None and tgt["at_turn"] < at)):
                reasons.append(f"resolves {res_id}: that event ({tgt['meeting_id']} t{tgt['at_turn']}) is not earlier than this one")
            elif tgt["meeting_id"] == meeting_id and tgt["part"] == int(part):
                warns.append(f"resolves {res_id} points into this same part; the link breaks if that event is not resubmitted")
        key = (topic, kind, at)
        if not reasons:
            if key in seen:
                reasons.append(f"duplicate of an earlier item in this file (same topic, kind, at_turn t{at})")
            elif key in other_parts:
                pp, eid = other_parts[key]
                reasons.append(f"duplicate: already stored from part {pp} as event {eid} (overlap turns); drop it")
        if reasons:
            reject("events", i, e, reasons, sugg); continue
        seen.add(key)
        if topic not in existing and topic not in file_topics and topic not in new_topics:
            new_topics[topic] = True
            warns.append(f"topic {topic!r} was not listed in 'topics'; created with label {topic.replace('-', ' ')!r}")
        for w in warns:
            rep["warnings"].append(f"events[{i}]: {w}")
        rows.append({"event_id": old_ids.get(key), "team": team, "meeting_id": meeting_id, "ord": ord_,
                     "part": int(part), "at_turn": at, "start_sec": turns[at]["start_sec"], "topic_slug": topic,
                     "kind": kind, "summary": summary, "speaker": speaker, "owner": owner, "value": value,
                     "evidence_turn": ev_turn, "evidence_quote": quote, "resolves_event_id": res_id,
                     "source": source, "created_at": time.time()})

    with conn:
        for (eid,) in conn.execute("SELECT event_id FROM events WHERE meeting_id=? AND part=?", (meeting_id, part)).fetchall():
            conn.execute("DELETE FROM events_fts WHERE rowid=?", (eid,))
        conn.execute("DELETE FROM events WHERE meeting_id=? AND part=?", (meeting_id, part))
        conn.execute("DELETE FROM speakers WHERE meeting_id=? AND source='llm' AND coalesce(part,1)=?", (meeting_id, part))
        for slug, (label, aliases) in file_topics.items():
            upsert_topic(conn, team, slug, label, aliases, meeting_id)
        for slug in new_topics:
            upsert_topic(conn, team, slug, None, (), meeting_id)
        cols = [c for c in rows[0].keys()] if rows else []
        for r in rows:
            vals = [r[c] for c in cols]
            if r["event_id"] is None:
                cur = conn.execute(f"INSERT INTO events({','.join(cols[1:])}) VALUES ({','.join('?' * (len(cols) - 1))})", vals[1:])
                r["event_id"] = cur.lastrowid
            else:
                conn.execute(f"INSERT INTO events({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            conn.execute("INSERT INTO events_fts(rowid, text_norm, scope) VALUES (?,?,?)",
                         (r["event_id"], normalize(" ".join(x for x in (r["summary"], r["topic_slug"].replace("-", " "),
                                                                        r["kind"].replace("_", " "), r["value"] or "") if x)),
                          _scope(team, meeting_id)))
            rep["stored_event_ids"].append(r["event_id"])
            rep["stored"].append(f"#{r['event_id']} t{r['at_turn']} {r['kind']} {r['topic_slug']}: "
                                 f"evidence t{r['evidence_turn']} \"{r['evidence_quote']}\"")
        for letter, name, role, ti in spk_ok:
            ex = conn.execute("SELECT name, role FROM speakers WHERE meeting_id=? AND speaker=? AND source='llm'",
                              (meeting_id, letter)).fetchone()
            if ex:  # another part already gave this letter: fill gaps only
                conn.execute("""UPDATE speakers SET name=coalesce(name, ?), role=coalesce(role, ?),
                                name_evidence_turn_idx=coalesce(name_evidence_turn_idx, ?),
                                evidence_turn_idx=coalesce(evidence_turn_idx, ?)
                                WHERE meeting_id=? AND speaker=? AND source='llm'""",
                             (name, role, ti if name else None, ti if role else None, meeting_id, letter))
            else:
                conn.execute("""INSERT INTO speakers(team, meeting_id, speaker, name, role, evidence_turn_idx,
                                name_evidence_turn_idx, source, part) VALUES (?,?,?,?,?,?,?, 'llm', ?)""",
                             (team, meeting_id, letter, name, role, ti if role else None, ti if name else None, int(part)))
        n_items = sum(len(data.get(k) or []) for k in ("speakers", "topics", "events"))
        conn.execute("""INSERT OR REPLACE INTO extraction_runs VALUES (?,?,?,?,?,?,?,?)""",
                     (meeting_id, int(part), source, len(events_in), len(rows),
                      sum(1 for r in rep["rejected"] if r["section"] == "events"), len(rep["warnings"]), time.time()))
    rep["accepted"] = len(rows)
    rep["accepted_speakers"] = len(spk_ok)
    rep["accepted_topics"] = len(file_topics) + len(new_topics)
    rep["n_items"] = n_items
    return rep
