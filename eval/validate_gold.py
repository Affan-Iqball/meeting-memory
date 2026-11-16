"""Validate eval/gold.json: schema, type mix, team rules and every evidence quote.

Every quote must pass meetmem.cite.verify_quote (>= 6 words, exact, inside one
speaker turn, case/space-insensitive, punctuation significant) AND be found in
the turn the evidence item names. Exit code 1 on any failure.

Run:  python3 eval/validate_gold.py [path/to/gold.json]
"""
import collections
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from meetmem.cite import verify_quote  # noqa: E402

ALLOWED_TEAMS = {"ES2005", "ES2012", "ES2013", "ES2015", "IS1000", "IS1001", "IS1004",
                 "IS1005", "IS1006", "TS3005", "TS3008"}
TYPES = {"decision_change", "commitment_followup", "number", "reversal_count", "who",
         "false_premise", "other"}
MIN_TYPES = {"decision_change": 3, "commitment_followup": 2, "number": 2,
             "reversal_count": 1, "who": 2}
FIELDS = ["question_id", "team", "type", "question", "gold_answer", "key_points",
          "evidence", "must_cite_meetings", "difficulty_notes"]


def main(path):
    data = json.load(open(path))
    qs = data["questions"]
    errors, n_quotes = [], 0
    official = {q["team"] for q in json.load(open(os.path.join(ROOT, "data", "questions.json")))["questions"]}
    ids = set()
    for q in qs:
        qid = q.get("question_id", "?")
        err = lambda m: errors.append(f"{qid}: {m}")  # noqa: E731
        for f in FIELDS:
            if f not in q:
                err(f"missing field {f}")
        if qid in ids:
            err("duplicate question_id")
        ids.add(qid)
        team = q["team"]
        if team not in ALLOWED_TEAMS or team in official or team == "TS3012":
            err(f"team {team} not allowed")
        if q["type"] not in TYPES:
            err(f"bad type {q['type']}")
        if not 2 <= len(q["key_points"]) <= 5:
            err(f"{len(q['key_points'])} key_points (need 2-5)")
        if not 2 <= len(q["evidence"]) <= 5:
            err(f"{len(q['evidence'])} evidence items (need 2-5)")
        mc = q["must_cite_meetings"]
        if any(not m.startswith(team) for m in mc):
            err(f"must_cite_meetings outside team: {mc}")
        if q["type"] != "false_premise" and len(set(mc)) < 2:
            err("needs at least two meetings")
        ev_meetings = set()
        for e in q["evidence"]:
            n_quotes += 1
            m = e["meeting_id"]
            ev_meetings.add(m)
            if not m.startswith(team):
                err(f"evidence meeting {m} not in team")
            ok, turn, reason = verify_quote(m, e["quote"])
            if not ok:
                err(f"quote fails verify_quote in {m}: {reason} :: {e['quote'][:80]!r}")
            elif turn != e["turn_idx"]:
                # the same words may also occur in an earlier turn; accept only if
                # they really are inside the named turn too
                from meetmem.search import get_turns
                from meetmem.cite import normalize_for_match
                row = get_turns(m, e["turn_idx"], e["turn_idx"])
                if not row or normalize_for_match(e["quote"]) not in normalize_for_match(row[0]["text"]):
                    err(f"quote found at t{turn}, not at stated t{e['turn_idx']} in {m}")
        missing = set(mc) - ev_meetings
        if missing:
            err(f"must_cite meetings without any evidence quote: {sorted(missing)}")
    types = collections.Counter(q["type"] for q in qs)
    for t, n in MIN_TYPES.items():
        if types[t] < n:
            errors.append(f"type mix: {t} has {types[t]}, need >= {n}")
    if types["false_premise"] != 1:
        errors.append(f"type mix: false_premise has {types['false_premise']}, need exactly 1")
    teams = collections.Counter(q["team"] for q in qs)

    print(f"questions: {len(qs)}   evidence quotes checked: {n_quotes}")
    print("types:", dict(sorted(types.items())))
    print("teams:", dict(sorted(teams.items())))
    if errors:
        print(f"FAIL: {len(errors)} problem(s)")
        for e in errors:
            print("  -", e)
        return 1
    print("PASS: all quotes verified (exact, >= 6 words, single turn, in the stated turn)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "eval", "gold.json")))
