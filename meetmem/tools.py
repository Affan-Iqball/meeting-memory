"""Pure tool functions over the event ledger (the CLI and a future MCP server
both wrap these). Every reader takes ``team`` and an optional ``as_of_ord``
hard cutoff that is applied in SQL.

Writing:  extract_plan, extract_prompt, (events.submit_events),
          answer_submit, brief_submit, export_answers
Reading:  topics_view, timeline, facts, actions, read_turns
Each ``*_text`` / ``format_*`` helper renders compact plain text for an LLM.
"""
from __future__ import annotations

import json
import os
import time

from . import cite, events as ev, store
from .search import MAX_TURN_RANGE, build_fts_query

PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "extract.md")
QUESTIONS_PATH = os.path.join(store.PROJECT_ROOT, "data", "questions.json")
GOLD_PATH = os.path.join(store.PROJECT_ROOT, "eval", "gold.json")
MAX_CITATIONS = 3


class ToolError(ValueError):
    pass


def _team_of(conn, meeting_id):
    r = conn.execute("SELECT team, ord FROM meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
    if r is None:
        raise ToolError(f"unknown meeting {meeting_id!r}")
    return r[0], r[1]


def _check_team(conn, team):
    if not conn.execute("SELECT 1 FROM meetings WHERE team=? LIMIT 1", (team,)).fetchone():
        raise ToolError(f"unknown team {team!r}")


def _hms(sec):
    if sec is None:
        return "--:--:--"
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# =========================================================================== writing side

def extract_plan(conn, team) -> dict:
    _check_team(conn, team)
    runs = {(r["meeting_id"], r["part"]): dict(r) for r in conn.execute(
        "SELECT r.* FROM extraction_runs r JOIN meetings m ON m.meeting_id=r.meeting_id WHERE m.team=?", (team,))}
    out, nxt = [], None
    for m in store.team_meetings(conn, team):
        parts = []
        for p in ev.meeting_parts(conn, m["meeting_id"]):
            r = runs.get((m["meeting_id"], p["part"]))
            parts.append({"part": p["part"], "turns": f"t{p['first_turn']}-t{p['last_turn']}", "n_words": p["n_words"],
                          "extracted": None if r is None else {
                              "source": r["source"], "accepted": r["n_accepted"], "rejected": r["n_rejected"],
                              "warnings": r["n_warnings"], "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["submitted_at"]))}})
            if r is None and nxt is None:
                nxt = {"meeting_id": m["meeting_id"], "part": p["part"]}
        out.append({"meeting_id": m["meeting_id"], "ord": m["ord"], "n_words": m["n_words"], "n_turns": m["n_turns"],
                    "n_parts": len(parts), "parts": parts})
    return {"team": team, "meetings": out, "next": nxt,
            "rule": f"meetings > {ev.SPLIT_THRESHOLD_WORDS} words are split into ~{ev.PART_TARGET_WORDS}-word parts "
                    f"with a {ev.PART_OVERLAP_TURNS}-turn overlap; extract parts in order"}


def context_text(conn, team, before) -> str:
    """Speaker table, topics with current status and open items, all strictly
    before ``before=(ord, meeting_id, part)``."""
    lines = ["SPEAKERS (letter: name, role; from self-introductions and earlier extraction):"]
    # merge_variants=False keeps the extraction prompt byte-identical for the live workers
    table = ev.speaker_table(conn, team, before=before, merge_variants=False)
    if not table:
        lines.append("  (none known yet)")
    for letter in sorted(table):
        d = table[letter]
        src = ", ".join(f"{mid}" for mid in sorted(d["per_meeting"]))
        lines.append(f"  {letter}: name {d['name'] or '?'}, role {d['role'] or '?'}  (seen in {src})"
                     + (f"  CONFLICT: {'; '.join(d['conflicts'])}" if d["conflicts"] else ""))
    topics = ev.team_topics(conn, team)
    stats = ev.topic_statuses(conn, team, before=before)
    lines.append("")
    lines.append("TOPICS (reuse these slugs; status = latest status event so far):")
    if not topics:
        lines.append("  (none yet)")
    for slug, t in topics.items():
        st = stats.get(slug)
        if st is None and t["first_meeting_id"] and _team_of(conn, t["first_meeting_id"])[1] >= before[0]:
            continue  # created by a later part/meeting: not visible yet
        al = f" (aka {', '.join(t['aliases'])})" if t["aliases"] else ""
        s = "no status yet" if not st or not st["status"] else \
            f"{st['status']} [{st['state']}], {st['reversals']} reversal(s)"
        lines.append(f"  {slug}: {t['label']}{al} -- {s}")
    resolved = ev.resolved_ids(conn, team, before=before)
    open_items = []
    for e in ev.team_events(conn, team, before=before):
        if e["event_id"] in resolved:
            continue
        if e["kind"] == "action_assigned":
            open_items.append(f"  #{e['event_id']} {e['meeting_id']} t{e['at_turn']} action, owner "
                              f"{ev.speaker_label(table, e['owner'])}: {e['summary']} [topic {e['topic_slug']}]")
    for slug, st in stats.items():
        if st["status"] == "deferred":
            open_items.append(f"  #{st['status_event']} deferred question on {slug}")
    lines.append("")
    lines.append("OPEN ITEMS (if this part reports back on one, emit action_reported or info with resolves = its #id):")
    lines += open_items or ["  (none)"]
    return "\n".join(lines)


def extract_prompt(conn, meeting_id, part=1) -> str:
    m = ev.meeting_info(conn, meeting_id)
    parts = ev.meeting_parts(conn, meeting_id)
    p = ev.get_part(conn, meeting_id, part)
    rows = conn.execute("SELECT turn_idx, speaker, time_str, text FROM turns WHERE meeting_id=? AND turn_idx BETWEEN ? AND ? "
                        "ORDER BY turn_idx", (meeting_id, p["first_turn"], p["last_turn"])).fetchall()
    letters = sorted({r[0] for r in conn.execute("SELECT DISTINCT speaker FROM turns WHERE meeting_id=?", (meeting_id,))})
    note = ""
    if int(part) > 1:
        prev = parts[int(part) - 2]
        note = (f"- This part starts with turns t{p['first_turn']}..t{prev['last_turn']} that overlap the previous part; "
                f"events there may already be stored and duplicates are dropped automatically.\n")
    with open(PROMPT_PATH, encoding="utf-8") as fh:
        tpl = fh.read()
    subs = {
        "TEAM": m["team"], "MEETING_ID": meeting_id, "ORD": str(m["ord"]),
        "N_MEETINGS": str(len(store.team_meetings(conn, m["team"]))), "PART": str(int(part)),
        "N_PARTS": str(len(parts)), "FIRST_TURN": str(p["first_turn"]), "LAST_TURN": str(p["last_turn"]),
        "KINDS": "\n".join(f"- {k}: {ev.KIND_DEFS[k]}" for k in ev.KINDS),
        "KIND_LIST": ", ".join(ev.KINDS), "LETTERS": ", ".join(letters), "PART_NOTE": note,
        "CONTEXT": context_text(conn, m["team"], (m["ord"], meeting_id, int(part))),
        "TRANSCRIPT": "\n".join(store.display_line(r[0], r[1], r[2], r[3]) for r in rows),
    }
    for k, v in subs.items():
        tpl = tpl.replace("{{" + k + "}}", v)
    return tpl


# ---------------------------------------------------------------- citations, answers, briefs

def resolve_citations(conn, cits, team, as_of_ord=None):
    """[{meeting_id, turn_idx, contains}] -> (resolved, problems, warnings)."""
    out, problems, warnings = [], [], []
    if not isinstance(cits, list) or not (1 <= len(cits) <= MAX_CITATIONS):
        problems.append(f"need 1-{MAX_CITATIONS} citations, got {len(cits) if isinstance(cits, list) else 'none'}")
        cits = cits if isinstance(cits, list) else []
    for k, c in enumerate(cits[:MAX_CITATIONS] if isinstance(cits, list) else []):
        tag = f"citation {k + 1}"
        if not isinstance(c, dict):
            problems.append(f"{tag}: must be an object"); continue
        mid = str(c.get("meeting_id", ""))
        r = conn.execute("SELECT team, ord FROM meetings WHERE meeting_id=?", (mid,)).fetchone()
        if r is None:
            problems.append(f"{tag}: unknown meeting {mid!r}"); continue
        if r[0] != team:
            problems.append(f"{tag}: {mid} belongs to {r[0]}, not {team}"); continue
        if as_of_ord is not None and r[1] > int(as_of_ord):
            problems.append(f"{tag}: {mid} is meeting {r[1]} of {team}, after the as-of cutoff {as_of_ord}"); continue
        ti = ev._as_int(c.get("turn_idx"))
        if ti is None:
            problems.append(f"{tag}: turn_idx missing or not an integer"); continue
        contains = c.get("contains") or c.get("quote")
        try:
            quote, w = ev.resolve_evidence(conn, mid, ti, contains if isinstance(contains, str) else None)
        except cite.QuoteError as qe:
            problems.append(f"{tag}: {qe}"); continue
        warnings += [f"{tag}: {x}" for x in w]
        if not (isinstance(contains, str) and contains.strip()):
            n_turn = conn.execute("SELECT n_words FROM turns WHERE meeting_id=? AND turn_idx=?", (mid, ti)).fetchone()
            if n_turn and n_turn[0] > ev.QUOTE_MAX:
                warnings.append(f"{tag}: no 'contains' given for a {n_turn[0]}-word turn; the quote is the turn's "
                                "first words and may not support the claim - add 'contains'")
        ok, idx, reason = cite.verify_quote(mid, quote, conn=conn)
        assert ok and len(quote.split()) >= cite.MIN_WORDS, reason
        out.append({"meeting_id": mid, "turn_idx": ti, "quote": quote})
    return out, problems, warnings


def lookup_question(question_id, questions_path=QUESTIONS_PATH, gold_path=GOLD_PATH):
    paths = [gold_path, questions_path] if str(question_id).upper().startswith("G") else [questions_path]
    for p in paths:
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
            for q in (d.get("questions", []) if isinstance(d, dict) else d):
                if isinstance(q, dict) and q.get("question_id") == question_id:
                    return q
    return None


def answer_submit(conn, run, question_id, data, source="unknown", team=None,
                  questions_path=QUESTIONS_PATH, gold_path=GOLD_PATH) -> dict:
    if team is None:
        q = lookup_question(question_id, questions_path, gold_path)
        if q is None:
            raise ToolError(f"question {question_id!r} not found (questions.json / eval/gold.json); pass --team")
        team = q["team"]
    if not isinstance(data, dict):
        raise ToolError("input must be an object {answer, citations}")
    answer = str(data.get("answer") or "").strip()
    problems = [] if answer else ["answer is empty"]
    cits, p2, warns = resolve_citations(conn, data.get("citations"), team)
    problems += p2
    if problems:
        return {"ok": False, "question_id": question_id, "team": team, "problems": problems, "warnings": warns}
    if len({c["meeting_id"] for c in cits}) < 2:
        warns.append("all citations come from one meeting (questions are designed to need >= 2)")
    with conn:
        conn.execute("INSERT OR REPLACE INTO answers VALUES (?,?,?,?,?,?,?)",
                     (run, question_id, team, answer, json.dumps(cits, ensure_ascii=False), time.time(), source))
    return {"ok": True, "run": run, "question_id": question_id, "team": team, "answer": answer,
            "citations": cits, "warnings": warns}


def render_brief(team, as_of_ord, sections) -> str:
    out = [f"Brief for team {team} (based on meetings 1-{as_of_ord})", ""]
    for s in sections:
        out.append(s["heading"])
        for p in s["points"]:
            cits = " ".join(f'[{c["meeting_id"]}: "{c["quote"]}"]' for c in p["citations"])
            out.append(f"- {p['text']}" + (f" {cits}" if cits else ""))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def brief_submit(conn, run, team, as_of_ord, data, source="unknown") -> dict:
    _check_team(conn, team)
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list) or not data["sections"]:
        raise ToolError("input must be {\"sections\": [{\"heading\": ..., \"points\": [{\"text\", \"citations\"}]}]}")
    problems, warnings, sections, allc = [], [], [], []
    for si, s in enumerate(data["sections"]):
        if not isinstance(s, dict) or not str(s.get("heading") or "").strip() or not isinstance(s.get("points"), list):
            problems.append(f"section {si + 1}: needs heading and points[]"); continue
        pts = []
        for pi, p in enumerate(s["points"]):
            tag = f"section {si + 1} point {pi + 1}"
            if not isinstance(p, dict) or not str(p.get("text") or "").strip():
                problems.append(f"{tag}: needs text"); continue
            raw = p.get("citations") or []
            if not raw:
                warnings.append(f"{tag}: no citation")
                pts.append({"text": p["text"].strip(), "citations": []}); continue
            cits, pr, w = resolve_citations(conn, raw, team, as_of_ord=as_of_ord)
            problems += [f"{tag}: {x}" for x in pr]
            warnings += [f"{tag}: {x}" for x in w]
            pts.append({"text": p["text"].strip(), "citations": cits})
            allc += cits
        sections.append({"heading": str(s["heading"]).strip(), "points": pts})
    if problems:
        return {"ok": False, "problems": problems, "warnings": warnings}
    text = render_brief(team, as_of_ord, sections)
    with conn:
        conn.execute("INSERT OR REPLACE INTO briefs VALUES (?,?,?,?,?,?,?,?)",
                     (run, team, int(as_of_ord), text, json.dumps(sections, ensure_ascii=False),
                      json.dumps(allc, ensure_ascii=False), time.time(), source))
    return {"ok": True, "run": run, "team": team, "as_of": int(as_of_ord), "text": text, "warnings": warnings}


def export_answers(conn, run, brief_team=None, questions_path=QUESTIONS_PATH) -> tuple[dict, list]:
    with open(questions_path, encoding="utf-8") as fh:
        qs = json.load(fh)
    official = [q["question_id"] for q in qs["questions"]]
    rows = {r["question_id"]: r for r in conn.execute("SELECT * FROM answers WHERE run=?", (run,))}
    notes = [f"skipped non-official question {k}" for k in rows if k not in official]
    notes += [f"missing answer for {k}" for k in official if k not in rows]
    answers = [{"question_id": k, "answer": rows[k]["answer"],
                "citations": [{"meeting_id": c["meeting_id"], "quote": c["quote"]}
                              for c in json.loads(rows[k]["citations_json"])]}
               for k in official if k in rows]
    brief_team = brief_team or (qs.get("brief_task") or {}).get("team")
    b = conn.execute("SELECT text FROM briefs WHERE run=? AND team=?", (run, brief_team)).fetchone()
    if b is None:
        notes.append(f"no brief for {brief_team} in run {run}")
    return {"answers": answers, "brief": b[0] if b else ""}, notes


# =========================================================================== reading side

def topics_view(conn, team, as_of_ord=None) -> list[dict]:
    _check_team(conn, team)
    stats = ev.topic_statuses(conn, team, as_of_ord=as_of_ord)
    out = []
    for slug, t in ev.team_topics(conn, team).items():
        st = stats.get(slug)
        if st is None:
            if as_of_ord is not None:
                continue  # no events visible as of the cutoff
            st = ev.derive_status([])
        out.append({"slug": slug, "label": t["label"], "aliases": t["aliases"], **{k: st[k] for k in (
            "status", "state", "firm", "reversals", "reopenings", "last_reversal_meeting", "n_events")}})
    return out


def format_topics(rows) -> str:
    if not rows:
        return "no topics"
    lines = []
    for r in rows:
        al = f" (aka {', '.join(r['aliases'])})" if r["aliases"] else ""
        st = r["status"] or "no status event"
        if r["state"] == "open" and r["firm"]:
            st += f" [open; last settled: {r['firm']}]"
        elif r["state"]:
            st += f" [{r['state']}]"
        rev = f", {r['reversals']} reversal(s)" + (f", last in {r['last_reversal_meeting']}" if r["reversals"] else "")
        lines.append(f"{r['slug']}: {r['label']}{al} -- {st}{rev}, {r['n_events']} event(s)")
    return "\n".join(lines)


def _event_line(e, view) -> str:
    """``view`` is an ev.SpeakerView: names/roles are resolved for the event's own meeting."""
    val = f" | value {e['value']}" if e["value"] else ""
    own = f" | owner {view.label(e['meeting_id'], e['owner'])}" if e["owner"] else ""
    res = f" | resolves #{e['resolves_event_id']}" if e["resolves_event_id"] is not None else ""
    return (f"  #{e['event_id']} {e['meeting_id']} {_hms(e['start_sec'])} {view.label(e['meeting_id'], e['speaker'])} "
            f"{e['kind']}: {e['summary']}{val}{own}{res} -> evidence {e['meeting_id']} t{e['evidence_turn']}"
            + (f" (at t{e['at_turn']})" if e["at_turn"] != e["evidence_turn"] else ""))


def timeline(conn, team, as_of_ord=None, topic=None, kinds=None, per_topic_cap=8) -> str:
    _check_team(conn, team)
    table = ev.SpeakerView(conn, team, as_of_ord=as_of_ord)
    topics = ev.team_topics(conn, team)
    all_evs = ev.team_events(conn, team, as_of_ord=as_of_ord, topic=topic)
    by = {}
    for e in all_evs:
        by.setdefault(e["topic_slug"], []).append(e)
    if not by:
        return f"no events for {team}" + (f" topic {topic}" if topic else "") + \
               (f" as of meeting {as_of_ord}" if as_of_ord is not None else "")
    cut = f"as of meeting {as_of_ord}" if as_of_ord is not None else "as of the latest meeting"
    order = sorted(by, key=lambda s: (by[s][0]["ord"], by[s][0]["at_turn"]))
    out = []
    for slug in order:
        evs = by[slug]
        st = ev.derive_status(evs)
        t = topics.get(slug, {"label": slug, "aliases": []})
        al = f" (aka {', '.join(t['aliases'])})" if t["aliases"] else ""
        seq = " -> ".join(f"{s['meeting_id'][-1]}:{s['kind']}" for s in st["sequence"]) or "none"
        cur = st["status"] or "no status event"
        if st["state"] == "open" and st["firm"]:
            cur += f" [open; last settled: {st['firm']}]"
        elif st["state"]:
            cur += f" [{st['state']}]"
        out.append(f"== {slug}: {t['label']}{al}")
        out.append(f"   status {cut}: {cur} | sequence: {seq} | reversals: {st['reversals']}"
                   + (f" (last in {st['last_reversal_meeting']})" if st["reversals"] else "")
                   + (f" | reopenings: {st['reopenings']}" if st["reopenings"] else ""))
        shown = [e for e in evs if not kinds or e["kind"] in kinds]
        omitted = 0
        if topic is None and per_topic_cap and len(shown) > per_topic_cap:
            omitted = len(shown) - per_topic_cap
            shown = shown[-per_topic_cap:]
            out.append(f"   ({omitted} earlier event(s) omitted; use --topic {slug} to see all)")
        out += [_event_line(e, table) for e in shown]
    return "\n".join(out)


def facts(conn, query, team, as_of_ord=None, kinds=None, limit=20) -> list[dict]:
    """Keyword search (same normaliser as passages) over event summaries,
    values, topic slugs/labels/aliases. Topic matches pull in that topic's events."""
    _check_team(conn, team)
    expr = build_fts_query(query)
    if expr is None:
        return []
    scoped = f'({expr}) AND scope : "team{team.lower()}"'
    hits = {}
    for r in conn.execute("SELECT rowid, -bm25(events_fts, 1.0, 0.0) AS s FROM events_fts WHERE events_fts MATCH ? "
                          "ORDER BY bm25(events_fts, 1.0, 0.0) LIMIT 200", (scoped,)):
        hits[r[0]] = r[1]
    topic_slugs = [r[0] for r in conn.execute(
        "SELECT t.slug FROM topics_fts f JOIN topics t ON t.topic_id=f.rowid WHERE topics_fts MATCH ? AND t.team=?",
        (scoped, team))]
    sql = "SELECT * FROM events WHERE team=?"
    args = [team]
    conds = []
    if hits:
        conds.append(f"event_id IN ({','.join('?' * len(hits))})"); args += list(hits)
    if topic_slugs:
        conds.append(f"topic_slug IN ({','.join('?' * len(topic_slugs))})"); args += topic_slugs
    if not conds:
        return []
    sql += " AND (" + " OR ".join(conds) + ")"
    if as_of_ord is not None:
        sql += " AND ord <= ?"; args.append(int(as_of_ord))
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"; args += list(kinds)
    rows = [dict(r) for r in conn.execute(sql, args)]
    for r in rows:
        r["score"] = hits.get(r["event_id"], 0.0) + (1.0 if r["topic_slug"] in topic_slugs else 0.0)
    rows.sort(key=lambda r: (-r["score"], r["ord"], r["at_turn"]))
    rows = rows[:max(1, int(limit))]
    rows.sort(key=lambda r: (r["ord"], r["at_turn"], r["event_id"]))
    return rows


def format_events(conn, team, rows, as_of_ord=None) -> str:
    if not rows:
        return "no matching events"
    table = ev.SpeakerView(conn, team, as_of_ord=as_of_ord)
    return "\n".join(f"[{e['topic_slug']}]" + _event_line(e, table)[1:] for e in rows)


def actions(conn, team, as_of_ord=None, open_only=False) -> list[dict]:
    _check_team(conn, team)
    view = ev.SpeakerView(conn, team, as_of_ord=as_of_ord)
    resolved = ev.resolved_ids(conn, team, as_of_ord=as_of_ord)
    out = []
    for e in ev.team_events(conn, team, as_of_ord=as_of_ord, kinds=["action_assigned"]):
        reps = resolved.get(e["event_id"], [])
        if open_only and reps:
            continue
        out.append({"event_id": e["event_id"], "meeting_id": e["meeting_id"], "at_turn": e["at_turn"],
                    "evidence_turn": e["evidence_turn"], "topic": e["topic_slug"], "summary": e["summary"],
                    "owner": e["owner"], "owner_label": view.label(e["meeting_id"], e["owner"]),
                    "speaker": e["speaker"], "status": "closed" if reps else "open",
                    "reported": [{"event_id": r["event_id"], "meeting_id": r["meeting_id"], "kind": r["kind"],
                                  "summary": r["summary"], "evidence_turn": r["evidence_turn"],
                                  "speaker_label": view.label(r["meeting_id"], r["speaker"])} for r in reps]})
    return out


def format_actions(rows) -> str:
    if not rows:
        return "no actions"
    lines = []
    for a in rows:
        lines.append(f"#{a['event_id']} {a['meeting_id']} t{a['at_turn']} owner {a['owner_label']} [{a['topic']}]: "
                     f"{a['summary']} -> evidence {a['meeting_id']} t{a['evidence_turn']} -- {a['status'].upper()}")
        for r in a["reported"]:
            lines.append(f"    reported #{r['event_id']} {r['meeting_id']} by {r['speaker_label']} ({r['kind']}): "
                         f"{r['summary']} -> evidence {r['meeting_id']} t{r['evidence_turn']}")
    return "\n".join(lines)


def read_turns(conn, meeting_id, first, last, team=None, as_of_ord=None) -> list[dict]:
    mteam, mord = _team_of(conn, meeting_id)
    if team and team != mteam:
        raise ToolError(f"{meeting_id} belongs to {mteam}, not {team}")
    if as_of_ord is not None and mord > int(as_of_ord):
        raise ToolError(f"{meeting_id} is meeting {mord} of {mteam}, after the as-of cutoff {as_of_ord}")
    first, last = int(first), int(last)
    if last < first:
        raise ToolError("LAST < FIRST")
    if last - first + 1 > MAX_TURN_RANGE:
        raise ToolError(f"range too large ({last - first + 1} turns, max {MAX_TURN_RANGE})")
    return [dict(r) for r in conn.execute(
        "SELECT meeting_id, turn_idx, speaker, start_sec, time_str, text, n_words FROM turns "
        "WHERE meeting_id=? AND turn_idx BETWEEN ? AND ? ORDER BY turn_idx", (meeting_id, first, last))]
