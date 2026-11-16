"""Code-only first pass at "who is who": self-introductions and stated roles.

Table ``speakers(team, meeting_id, speaker, name, role, evidence_turn_idx,
name_evidence_turn_idx, source)``, one row per (meeting, speaker letter,
source). This module only writes ``source='regex'`` rows, and only when the
speaker says it about THEMSELVES ("I'm the project manager", "My name is
Ed", "I'm Laura and I'm the ..."). No row = unknown (a later LLM pass fills
gaps). Nothing is propagated across a team's meetings: letters are measured,
not assumed, to keep the same person (see ``role_consistency``).

Canonical roles: 'project manager', 'industrial designer',
'user interface designer', 'marketing expert'.

Public API::

    detect(turns) -> dict[speaker, {"name", "role", "evidence_turn_idx", "name_evidence_turn_idx"}]
    store_regex_speakers(conn, team, meeting_id, turns) -> int
    speaker_rows(conn, team=None, meeting_id=None) -> list[dict]
    role_consistency(conn) -> dict          # measurement + conflicts with evidence
    python3 -m meetmem.speakers             # prints the consistency report
"""
from __future__ import annotations

import re
from collections import defaultdict

PM, ID, UI, ME = "project manager", "industrial designer", "user interface designer", "marketing expert"

# order matters: first match wins
_ROLE_ALTS = [
    (UI, r"(?:user\s+(?:(?:um|uh|us)\s+)*)?interface\s+(?:design\s+)?design(?:er)?|user\s+interface\s+manager|u_i_\s+designer|ui\s+designer"),
    (ID, r"industrial\s+design(?:er)?"),
    (ME, r"marketing(?:\s+(?:expert|manager|person|guy|specialist|executive))?|market(?:ing)?\s+research(?:er)?(?:\s+person)?|market\s+expert|marketer"),
    (PM, r"(?:project|program|programme)\s+(?:(?:um|uh|pr|ma)\s+)?manager|project\s+leader"),
]
_ROLE_RE = "(?P<role>" + "|".join(f"(?:{alt})" for _, alt in _ROLE_ALTS) + r")\b"
_FILL = (r"(?:\s+(?:the|your|our|a|an|uh|um|i'm|i|us|pr|just|also|now|here|excellent|new|basically|"
         r"actually|doing|playing|into|in|gonna\s+be|going\s+to\s+be|supposed\s+to\s+be|sort\s+of|kind\s+of))*")
_SELF = r"(?:\bi'm|\bi\s+am|\bi'll\s+be|\bi\s+will\s+be|\bmy\s+role\s+is|\bmy\s+role's|\bi\s+work\s+as|\bi'm\s+working\s+as)"

ROLE_PATTERNS = [
    re.compile(_SELF + _FILL + r"\s+" + _ROLE_RE, re.I),
    # "I'm Sarah, the project manager" / "I am Hemant, the industrial designer"
    re.compile(r"\b[Ii](?:'m|\s+am)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?,?\s+(?:and\s+)?(?i:the|your)" + _FILL + r"\s+(?i:" + _ROLE_RE + ")"),
    re.compile(r"\bi(?:'m|\s+am)\s+(?:the\s+|an\s+)?expert\s+(?:at|in|on|for)\s+(?P<role>marketing)\b", re.I),
    # "as (the) project manager, I ..."
    re.compile(r"\bas" + _FILL + r"\s+" + _ROLE_RE + r",?\s+(?:i|i'm|i'll|i've|i'd|my)\b", re.I),
]

_NOT_NAMES = {
    "User", "Yeah", "What", "Well", "We", "Sorry", "So", "See", "Project", "Oh", "No", "Industrial",
    "Marketing", "Market", "Interface", "Okay", "Not", "Sure", "Just", "I", "The", "A", "An", "Um", "Uh",
    "Your", "Our", "Going", "Gonna", "Here", "Now", "Fine", "Good", "Mm", "Hmm", "And", "But", "It", "Name",
    "Also", "Really", "Very", "Still", "Done", "Ready", "Back", "Afraid", "Glad", "Happy", "Hungry", "English",
}
NAME_PATTERNS = [
    re.compile(r"\b[Mm]y\s+name(?:'s|\s+is)\s+(?:(?:uh|um)\s+)*(?P<name>[A-Z][a-z]+)\b"),
    re.compile(r"\b(?:I'm|I\s+am)\s+(?P<name>[A-Z][a-z]+)\b(?!')"),
]


def _canon_role(s: str) -> str:
    s = s.lower()
    for canon, alt in _ROLE_ALTS:
        if re.fullmatch(alt, s, re.I):
            return canon
    return s


def _g(t, k):
    return t[k] if isinstance(t, dict) or hasattr(t, "keys") else getattr(t, k)


_BARE_OK_NEXT = {"and", "so", "um", "uh", "person", "side", "department", "expert", "guy", "but"}


def _plausible(m, text) -> bool:
    """Reject bare "marketing" used as a verb/noun phrase ("what I'm marketing.",
    "I'm marketing right now") and statements preceded by a negation/question
    ("I don't know what I'm ...")."""
    role = m.group("role").lower()
    before = text[max(0, m.start() - 25):m.start()].lower()
    if re.search(r"\b(what|don't know|not sure|whether|if)\s*$", before) or re.search(r"\bwhat\s+i\b", before + text[m.start():m.start() + 4].lower()):
        return False
    if role == "marketing":
        nxt = text[m.end():].lstrip()
        if not nxt:
            return True
        if nxt[0] in ",.!?":
            return True
        w = re.match(r"[A-Za-z']+", nxt)
        return bool(w) and w.group(0).lower() in _BARE_OK_NEXT
    return True


def detect(turns) -> dict:
    """First self-stated role and first self-stated name per speaker letter."""
    found: dict[str, dict] = {}
    for t in turns:
        text, spk, idx = _g(t, "text"), _g(t, "speaker"), _g(t, "turn_idx")
        rec = found.setdefault(spk, {"name": None, "role": None, "evidence_turn_idx": None,
                                     "name_evidence_turn_idx": None})
        if rec["role"] is None:
            for pat in ROLE_PATTERNS:
                for m in pat.finditer(text):
                    if not _plausible(m, text):
                        continue
                    rec["role"] = _canon_role(m.group("role"))
                    rec["evidence_turn_idx"] = idx
                    break
                if rec["role"]:
                    break
        if rec["name"] is None:
            for pat in NAME_PATTERNS:
                for m in pat.finditer(text):
                    if m.group("name") not in _NOT_NAMES:
                        rec["name"] = m.group("name")
                        rec["name_evidence_turn_idx"] = idx
                        break
                if rec["name"]:
                    break
    return {k: v for k, v in found.items() if v["role"] or v["name"]}


def _ensure_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(speakers)")}
    if "name_evidence_turn_idx" not in cols:
        conn.execute("ALTER TABLE speakers ADD COLUMN name_evidence_turn_idx INTEGER")


def store_regex_speakers(conn, team: str, meeting_id: str, turns) -> int:
    """Replace this meeting's source='regex' rows. Called by store.ingest_meeting."""
    _ensure_columns(conn)
    conn.execute("DELETE FROM speakers WHERE meeting_id=? AND source='regex'", (meeting_id,))
    rows = detect(turns)
    conn.executemany(
        """INSERT INTO speakers(team, meeting_id, speaker, name, role, evidence_turn_idx,
                                name_evidence_turn_idx, source) VALUES (?,?,?,?,?,?,?, 'regex')""",
        [(team, meeting_id, spk, r["name"], r["role"], r["evidence_turn_idx"], r["name_evidence_turn_idx"])
         for spk, r in sorted(rows.items())])
    return len(rows)


def speaker_rows(conn, team=None, meeting_id=None) -> list[dict]:
    _ensure_columns(conn)
    sql, args = "SELECT * FROM speakers WHERE 1=1", []
    if team:
        sql += " AND team=?"; args.append(team)
    if meeting_id:
        sql += " AND meeting_id=?"; args.append(meeting_id)
    return [dict(r) for r in conn.execute(sql + " ORDER BY meeting_id, speaker, source", args)]


def _evidence(conn, mid, idx):
    if idx is None:
        return None
    r = conn.execute("SELECT text FROM turns WHERE meeting_id=? AND turn_idx=?", (mid, idx)).fetchone()
    return r[0][:160] if r else None


def role_consistency(conn) -> dict:
    """Measure whether a speaker letter keeps the same detected role (and name)
    across a team's meetings. Also flags two letters claiming one role in the
    same meeting."""
    rows = speaker_rows(conn)
    by_team = defaultdict(list)
    for r in rows:
        if r["source"] == "regex":
            by_team[r["team"]].append(r)
    all_teams = [r[0] for r in conn.execute("SELECT DISTINCT team FROM meetings ORDER BY team")]
    report = {"teams_total": len(all_teams), "teams_with_any_role": 0,
              "teams_role_in_2plus_meetings": 0, "teams_consistent": 0, "teams_conflict": 0,
              "rows": len(rows), "rows_with_role": sum(1 for r in rows if r["role"]),
              "rows_with_name": sum(1 for r in rows if r["name"]),
              "conflicts": [], "per_team": {}}
    for team in all_teams:
        trs = by_team.get(team, [])
        letter_roles = defaultdict(list)   # letter -> [(meeting, role, idx)]
        letter_names = defaultdict(list)
        for r in trs:
            if r["role"]:
                letter_roles[r["speaker"]].append((r["meeting_id"], r["role"], r["evidence_turn_idx"]))
            if r["name"]:
                letter_names[r["speaker"]].append((r["meeting_id"], r["name"], r["name_evidence_turn_idx"]))
        if not letter_roles:
            report["per_team"][team] = "no roles detected"
            continue
        report["teams_with_any_role"] += 1
        multi = any(len({m for m, _, _ in v}) >= 2 for v in letter_roles.values())
        conflicts = []
        for letter, v in letter_roles.items():
            if len({role for _, role, _ in v}) > 1:
                conflicts.append({"team": team, "type": "letter changes role", "speaker": letter,
                                  "evidence": [(m, role, i, _evidence(conn, m, i)) for m, role, i in v]})
        for letter, v in letter_names.items():
            if len({n for _, n, _ in v}) > 1:
                conflicts.append({"team": team, "type": "letter changes name", "speaker": letter,
                                  "evidence": [(m, n, i, _evidence(conn, m, i)) for m, n, i in v]})
        per_mtg = defaultdict(lambda: defaultdict(list))
        for letter, v in letter_roles.items():
            for m, role, i in v:
                per_mtg[m][role].append((letter, i))
        for m, roles in per_mtg.items():
            for role, ls in roles.items():
                if len(ls) > 1:
                    conflicts.append({"team": team, "type": "two letters claim one role in a meeting",
                                      "role": role, "evidence": [(m, l, i, _evidence(conn, m, i)) for l, i in ls]})
        role_to_letters = defaultdict(set)
        for letter, v in letter_roles.items():
            for _, role, _ in v:
                role_to_letters[role].add(letter)
        for role, ls in role_to_letters.items():
            if len(ls) > 1 and not any(c.get("role") == role for c in conflicts):
                conflicts.append({"team": team, "type": "role held by different letters across meetings",
                                  "role": role, "evidence": [(m, l, r2, i, _evidence(conn, m, i))
                                                             for l in sorted(ls) for m, r2, i in letter_roles[l] if r2 == role]})
        if multi:
            report["teams_role_in_2plus_meetings"] += 1
            if not conflicts:
                report["teams_consistent"] += 1
        if conflicts:
            report["teams_conflict"] += 1
            report["conflicts"].extend(conflicts)
        report["per_team"][team] = {l: sorted({f"{m[-1]}:{role}" for m, role, _ in v}) for l, v in letter_roles.items()}
    return report


def main():  # pragma: no cover
    import json
    from .store import connect
    rep = role_consistency(connect())
    print(json.dumps({k: v for k, v in rep.items() if k != "per_team"}, indent=1))
    print("per team:")
    for t, v in rep["per_team"].items():
        print(" ", t, v)


if __name__ == "__main__":
    main()
