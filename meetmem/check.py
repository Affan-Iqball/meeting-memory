"""Validate a submission file against questions.json and the transcripts.

    python3 -m meetmem.check answers.json [--questions data/questions.json] [--db memory.db]

Checks per question: present; non-empty answer; 1-3 citations; each
citation's meeting exists and belongs to the question's team; each quote
passes cite.verify_quote. Warnings (not failures): all citations from one
meeting (questions are designed to need >= 2 meetings), unknown question ids,
answers longer than ~3 sentences. Brief must be a non-empty string.
Exit code 0 only if everything passes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from . import store
from .cite import verify_quote

DEFAULT_QUESTIONS = os.path.join(store.PROJECT_ROOT, "data", "questions.json")


def check_submission(sub: dict, questions: dict, conn) -> dict:
    qs = {q["question_id"]: q for q in questions["questions"]}
    results, failures, warnings = [], 0, []
    answers = sub.get("answers") if isinstance(sub, dict) else None
    if not isinstance(answers, list):
        return {"results": [], "failures": 1, "warnings": [], "fatal": "submission has no 'answers' list"}
    by_id = {}
    for a in answers:
        qid = a.get("question_id") if isinstance(a, dict) else None
        if qid in by_id:
            warnings.append(f"duplicate answer for {qid}; checking the first")
            continue
        by_id[qid] = a
    for qid in by_id:
        if qid not in qs:
            warnings.append(f"unknown question_id {qid!r}")
    for qid, q in qs.items():
        res = {"question_id": qid, "ok": True, "problems": [], "warnings": [], "citations": []}
        a = by_id.get(qid)
        if a is None:
            res["ok"] = False
            res["problems"].append("missing answer")
            results.append(res)
            failures += 1
            continue
        if not str(a.get("answer", "")).strip():
            res["ok"] = False
            res["problems"].append("empty answer")
        elif len(re.findall(r"[.!?](\s|$)", str(a["answer"]).strip())) > 3:
            res["warnings"].append("answer looks longer than three sentences")
        cits = a.get("citations")
        if not isinstance(cits, list) or not (1 <= len(cits) <= 3):
            res["ok"] = False
            res["problems"].append(f"need 1-3 citations, got {len(cits) if isinstance(cits, list) else 'none'}")
            cits = cits if isinstance(cits, list) else []
        mids = set()
        for k, c in enumerate(cits):
            mid = str(c.get("meeting_id", "")) if isinstance(c, dict) else ""
            quote = str(c.get("quote", "")) if isinstance(c, dict) else ""
            cr = {"meeting_id": mid, "ok": True, "reason": "ok", "turn_idx": None}
            team = conn.execute("SELECT team FROM meetings WHERE meeting_id=?", (mid,)).fetchone()
            if team is None:
                cr.update(ok=False, reason=f"meeting_not_found: {mid!r}")
            elif team[0] != q["team"]:
                cr.update(ok=False, reason=f"wrong_team: {mid} belongs to {team[0]}, question is about {q['team']}")
            else:
                ok, idx, reason = verify_quote(mid, quote, conn=conn)
                cr.update(ok=ok, reason=reason, turn_idx=idx)
            if not cr["ok"]:
                res["ok"] = False
                res["problems"].append(f"citation {k+1} ({mid}): {cr['reason']}")
            mids.add(mid)
            res["citations"].append(cr)
        if len(mids) == 1 and cits:
            res["warnings"].append("all citations come from one meeting")
        if not res["ok"]:
            failures += 1
        results.append(res)
    brief = sub.get("brief")
    brief_ok = isinstance(brief, str) and bool(brief.strip())
    if not brief_ok:
        failures += 1
    return {"results": results, "failures": failures, "warnings": warnings, "brief_ok": brief_ok}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m meetmem.check")
    ap.add_argument("answers")
    ap.add_argument("--questions", default=DEFAULT_QUESTIONS)
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    with open(args.answers, encoding="utf-8") as fh:
        sub = json.load(fh)
    with open(args.questions, encoding="utf-8") as fh:
        questions = json.load(fh)
    rep = check_submission(sub, questions, store.connect(args.db))
    if rep.get("fatal"):
        print("FATAL:", rep["fatal"])
        return 2
    for r in rep["results"]:
        print(f"{r['question_id']}: {'PASS' if r['ok'] else 'FAIL'}")
        for p in r["problems"]:
            print("    -", p)
        for w in r["warnings"]:
            print("    ~ warning:", w)
    for w in rep["warnings"]:
        print("warning:", w)
    print(f"brief: {'PASS' if rep['brief_ok'] else 'FAIL (missing or empty)'}")
    n = len(rep["results"])
    passed = sum(1 for r in rep["results"] if r["ok"])
    print(f"summary: {passed}/{n} questions pass, brief {'ok' if rep['brief_ok'] else 'missing'}, "
          f"{rep['failures']} failure(s)")
    return 1 if rep["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
