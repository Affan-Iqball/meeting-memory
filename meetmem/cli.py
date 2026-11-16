"""Command line.

    python3 -m meetmem ingest data/transcripts          # a directory (all *.txt) or one file
    python3 -m meetmem search "query" --team TS3004 [--as-of 3] [--meeting TS3004b] [--limit 8]
    python3 -m meetmem stats
    python3 -m meetmem speakers                          # role-consistency report
    python3 -m meetmem speakers --team TS3006 [--as-of 3] [--json]   # per-meeting speaker grid
    python3 -m meetmem.check answers.json                # submission validator

Event ledger (see meetmem/events.py, meetmem/tools.py):
    python3 -m meetmem extract-plan TS3004
    python3 -m meetmem extract-prompt TS3004b --part 1 > prompt.txt
    python3 -m meetmem submit-events TS3004b out.json --part 1 --source haiku-1
    python3 -m meetmem topics   --team TS3004 [--as-of 3]
    python3 -m meetmem timeline --team TS3004 [--topic speech-recognition] [--kinds decided rejected] [--as-of 3]
    python3 -m meetmem facts "LCD price" --team TS3004 [--kind number] [--as-of 3]
    python3 -m meetmem actions  --team IS1008 [--open] [--as-of 3]
    python3 -m meetmem turns TS3004c 560 570 [--team TS3004 --as-of 3]
    python3 -m meetmem answer-submit --run r1 Q01 ans.json --source opus [--team TS3006]
    python3 -m meetmem brief-submit --run r1 TS3012 --as-of 3 brief.json --source opus
    python3 -m meetmem export-answers --run r1 [--brief-team TS3012] > answers.json
LLM agent (Claude Code CLI in print mode, see meetmem/agent.py):
    python3 -m meetmem ask Q13 [--run NAME] [--model sonnet] [--force]
    python3 -m meetmem ask --team TS3004 "free text question" [--run NAME]
    python3 -m meetmem ask-all [--questions data/questions.json] --run official
    python3 -m meetmem brief TS3012 --as-of 3 --run official
    python3 -m meetmem extract TS3004 --model haiku
    python3 -m meetmem usage [--run NAME]
Global option: --db PATH (default $MEETMEM_DB or <project>/memory.db).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

from . import store


def cmd_ingest(conn, args) -> int:
    path = args.path
    files = sorted(glob.glob(os.path.join(path, "*.txt"))) if os.path.isdir(path) else [path]
    t0 = time.perf_counter()
    bad = 0
    tot = {"turns": 0, "words": 0, "passages": 0}
    for f in files:
        r = store.ingest_file(conn, f)
        for k in tot:
            tot[k] += r[k]
        if r["problems"]:
            bad += 1
            print(f"PARSE PROBLEMS in {f}:", *r["problems"], sep="\n  ")
    dt = time.perf_counter() - t0
    print(f"ingested {len(files)} file(s): {tot['turns']} turns, {tot['words']} words, "
          f"{tot['passages']} passages; {bad} with parse problems; wall time {dt:.2f}s")
    return 1 if bad else 0


def cmd_search(conn, args) -> int:
    from .search import search_passages
    hits = search_passages(args.query, team=args.team, meeting_id=args.meeting, as_of_ord=args.as_of,
                           limit=args.limit, conn=conn)
    for h in hits:
        print(f"== {h['passage_id']}  ord={h['ord']}  turns t{h['first_turn']}-t{h['last_turn']}  score={h['score']:.2f}")
        print(h["text_display"])
        print()
    if not hits:
        print("no hits")
    return 0


def cmd_stats(conn, args) -> int:
    q = lambda s: conn.execute(s).fetchone()[0]
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    out = {
        "db": db,
        "meetings": q("SELECT count(*) FROM meetings"),
        "teams": q("SELECT count(DISTINCT team) FROM meetings"),
        "turns": q("SELECT count(*) FROM turns"),
        "words": q("SELECT coalesce(sum(n_words),0) FROM meetings"),
        "passages": q("SELECT count(*) FROM passages"),
        "avg_passage_words": round(q("SELECT avg(n_words) FROM passages") or 0, 1),
        "speaker_rows_regex": q("SELECT count(*) FROM speakers WHERE source='regex'"),
        "meetings_with_parse_problems": q("SELECT count(*) FROM meetings WHERE parse_problems IS NOT NULL"),
        "db_bytes": os.path.getsize(db) if db and os.path.exists(db) else None,
        "teams_not_4_meetings": [dict(r) for r in conn.execute(
            "SELECT team, group_concat(letter, '') AS letters FROM meetings GROUP BY team HAVING count(*) != 4")],
    }
    print(json.dumps(out, indent=1))
    return 0


def cmd_speakers(conn, args) -> int:
    if args.team:
        from .events import SpeakerView
        v = SpeakerView(conn, args.team, as_of_ord=args.as_of)
        print(json.dumps(v.grid(), ensure_ascii=False, indent=1) if args.json else v.grid_text())
        return 0
    from .speakers import role_consistency
    rep = role_consistency(conn)
    print(json.dumps({k: v for k, v in rep.items() if k != "per_team"}, indent=1))
    if args.verbose:
        for t, v in rep["per_team"].items():
            print(t, v)
    return 0


def _load_json(path):
    try:
        with (sys.stdin if path == "-" else open(path, encoding="utf-8")) as fh:
            return json.load(fh), None
    except (OSError, json.JSONDecodeError) as e:
        return None, f"cannot read JSON from {path}: {e}"


def _out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def cmd_extract_plan(conn, args) -> int:
    from .tools import extract_plan
    _out(extract_plan(conn, args.team))
    return 0


def cmd_extract_prompt(conn, args) -> int:
    from .tools import extract_prompt
    print(extract_prompt(conn, args.meeting, args.part))
    return 0


def cmd_submit_events(conn, args) -> int:
    from .events import submit_events
    data, err = _load_json(args.file)
    if err:
        _out({"ok": False, "error": err + " -- reply must be ONE JSON object"}); return 2
    rep = submit_events(conn, args.meeting, data, part=args.part, source=args.source)
    rep["ok"] = True
    _out(rep)
    return 0


def cmd_topics(conn, args) -> int:
    from .tools import topics_view, format_topics
    rows = topics_view(conn, args.team, args.as_of)
    print(json.dumps(rows, ensure_ascii=False) if args.json else format_topics(rows))
    return 0


def cmd_timeline(conn, args) -> int:
    from .tools import timeline
    print(timeline(conn, args.team, args.as_of, topic=args.topic, kinds=args.kinds, per_topic_cap=args.cap))
    return 0


def cmd_facts(conn, args) -> int:
    from .tools import facts, format_events
    rows = facts(conn, args.query, args.team, args.as_of, kinds=args.kind, limit=args.limit)
    print(format_events(conn, args.team, rows, args.as_of))
    return 0


def cmd_actions(conn, args) -> int:
    from .tools import actions, format_actions
    rows = actions(conn, args.team, args.as_of, open_only=args.open)
    print(json.dumps(rows, ensure_ascii=False) if args.json else format_actions(rows))
    return 0


def cmd_turns(conn, args) -> int:
    from .tools import read_turns
    from .search import format_turns
    rows = read_turns(conn, args.meeting, args.first, args.last, team=args.team, as_of_ord=args.as_of)
    print(format_turns(rows) if rows else "no turns in that range")
    return 0


def cmd_answer_submit(conn, args) -> int:
    from .tools import answer_submit
    data, err = _load_json(args.file)
    if err:
        _out({"ok": False, "error": err}); return 2
    rep = answer_submit(conn, args.run, args.question_id, data, source=args.source, team=args.team)
    _out(rep)
    return 0 if rep["ok"] else 1


def cmd_brief_submit(conn, args) -> int:
    from .tools import brief_submit
    data, err = _load_json(args.file)
    if err:
        _out({"ok": False, "error": err}); return 2
    rep = brief_submit(conn, args.run, args.team, args.as_of, data, source=args.source)
    if rep["ok"]:
        print(rep["text"])
        for w in rep["warnings"]:
            print("warning:", w, file=sys.stderr)
        return 0
    _out(rep)
    return 1


def cmd_export_answers(conn, args) -> int:
    from .tools import export_answers
    from .tools import QUESTIONS_PATH
    sub, notes = export_answers(conn, args.run, brief_team=args.brief_team, questions_path=args.questions or QUESTIONS_PATH)
    print(json.dumps(sub, ensure_ascii=False, indent=1))
    for n in notes:
        print("note:", n, file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m meetmem")
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest"); p.add_argument("path")
    p = sub.add_parser("search"); p.add_argument("query"); p.add_argument("--team")
    p.add_argument("--meeting"); p.add_argument("--as-of", type=int, dest="as_of"); p.add_argument("--limit", type=int, default=8)
    sub.add_parser("stats")
    p = sub.add_parser("speakers"); p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--team", help="per-meeting grid of who each letter is in each meeting")
    p.add_argument("--as-of", type=int, dest="as_of"); p.add_argument("--json", action="store_true")

    def asof(p):
        p.add_argument("--as-of", type=int, dest="as_of", help="hard cutoff: only meetings with ord <= this")

    p = sub.add_parser("extract-plan"); p.add_argument("team")
    p = sub.add_parser("extract-prompt"); p.add_argument("meeting"); p.add_argument("--part", type=int, default=1)
    p = sub.add_parser("submit-events"); p.add_argument("meeting"); p.add_argument("file")
    p.add_argument("--part", type=int, default=1); p.add_argument("--source", required=True)
    p = sub.add_parser("topics"); p.add_argument("--team", required=True); asof(p); p.add_argument("--json", action="store_true")
    p = sub.add_parser("timeline"); p.add_argument("--team", required=True); asof(p); p.add_argument("--topic")
    p.add_argument("--kinds", nargs="+"); p.add_argument("--cap", type=int, default=8, help="events per topic without --topic")
    p = sub.add_parser("facts"); p.add_argument("query"); p.add_argument("--team", required=True); asof(p)
    p.add_argument("--kind", nargs="+"); p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("actions"); p.add_argument("--team", required=True); asof(p)
    p.add_argument("--open", action="store_true"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("turns"); p.add_argument("meeting"); p.add_argument("first", type=int); p.add_argument("last", type=int)
    p.add_argument("--team"); asof(p)
    p = sub.add_parser("answer-submit"); p.add_argument("--run", required=True); p.add_argument("question_id")
    p.add_argument("file"); p.add_argument("--source", required=True); p.add_argument("--team")
    p = sub.add_parser("brief-submit"); p.add_argument("--run", required=True); p.add_argument("team")
    p.add_argument("--as-of", type=int, dest="as_of", required=True); p.add_argument("file")
    p.add_argument("--source", default="unknown")
    p = sub.add_parser("export-answers"); p.add_argument("--run", required=True); p.add_argument("--brief-team")
    p.add_argument("--questions", default=None, help="questions file (default data/questions.json)")
    from .agent import add_subparsers as agent_subparsers
    agent_cmds = agent_subparsers(sub)
    args = ap.parse_args(argv)
    conn = store.connect(args.db)
    cmds = {"ingest": cmd_ingest, "search": cmd_search, "stats": cmd_stats, "speakers": cmd_speakers,
            "extract-plan": cmd_extract_plan, "extract-prompt": cmd_extract_prompt, "submit-events": cmd_submit_events,
            "topics": cmd_topics, "timeline": cmd_timeline, "facts": cmd_facts, "actions": cmd_actions,
            "turns": cmd_turns, "answer-submit": cmd_answer_submit, "brief-submit": cmd_brief_submit,
            "export-answers": cmd_export_answers, **agent_cmds}
    from .events import SubmitError
    from .tools import ToolError
    from .agent import BackendError
    try:
        return cmds[args.cmd](conn, args)
    except (SubmitError, ToolError, ValueError, BackendError) as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stdout)
        return 2


if __name__ == "__main__":
    sys.exit(main())
