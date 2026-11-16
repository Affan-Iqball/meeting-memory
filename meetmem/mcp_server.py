"""Minimal MCP server over stdio, pure stdlib (JSON-RPC 2.0, one JSON object per line).

    python3 -m meetmem.mcp_server            # speaks MCP on stdin/stdout; logs go to stderr

Methods: initialize, notifications/initialized, ping, tools/list, tools/call.
Every tool wraps a function in meetmem.tools / meetmem.events / meetmem.search
directly (no shelling out). Tool errors come back as a normal result with
``isError: true`` and the reason, so the model can fix the call and retry.

Environment:
    MEETMEM_DB            database path (default <project>/memory.db)
    MEETMEM_AS_OF_TEAM    with MEETMEM_AS_OF_ORD: hard clamp. Every call must be about this
    MEETMEM_AS_OF_ORD     team, and sees only its meetings with ord <= this, whatever the model asks.
    MEETMEM_RUN           run name that submit_answer / submit_brief write under (default "mcp")
    MEETMEM_SOURCE        source tag stored with submissions (default "mcp")
    MEETMEM_QUESTION_ID   if set, submit_answer only accepts this question id
    MEETMEM_TEAM          default team for submit_answer on ad-hoc question ids
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback

from . import events as ev, store, tools
from .search import MAX_TURN_RANGE, format_turns, search_passages

SERVER_NAME = "meetmem"
SERVER_VERSION = "0.1"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_WINDOW = 15
MAX_ANSWER_SENTENCES = 3

_TEAM = {"type": "string", "description": "team id, e.g. TS3004"}
_ASOF = {"type": "integer", "minimum": 1,
         "description": "only meetings 1..as_of of the team (1 = first meeting). Omit to see all."}
_CITS = {
    "type": "array", "minItems": 1, "maxItems": 3,
    "description": "1-3 citations. Never type a quote: point at a turn and the code copies the quote.",
    "items": {"type": "object", "required": ["meeting_id", "turn_idx"], "additionalProperties": False, "properties": {
        "meeting_id": {"type": "string", "description": "e.g. TS3004c"},
        "turn_idx": {"type": "integer", "description": "turn id as printed [tNNN ...] by the turns tool"},
        "contains": {"type": "string",
                     "description": "a few words copied from that turn; the quote is cut around them"}}}}

TOOLS = [
    {"name": "meetings", "description": "List the team's meetings in order (ord 1..n), with turn counts. "
                                        "The last one listed is the team's final meeting.",
     "inputSchema": {"type": "object", "required": ["team"], "properties": {"team": _TEAM}}},
    {"name": "topics", "description": "All ledger topics of a team with code-computed status, state, "
                                      "reversal count and last-reversal meeting. Start here.",
     "inputSchema": {"type": "object", "required": ["team"], "properties": {"team": _TEAM, "as_of": _ASOF}}},
    {"name": "timeline", "description": "Chronological events per topic: status sequence (meeting letter:kind), "
                                        "reversals, and one line per event with speaker (resolved for that "
                                        "meeting) and an evidence turn id to read with `turns`.",
     "inputSchema": {"type": "object", "required": ["team"], "properties": {
         "team": _TEAM, "as_of": _ASOF,
         "topic": {"type": "string", "description": "topic slug from `topics`; omit for all topics (capped)"},
         "kinds": {"type": "array", "items": {"type": "string", "enum": list(ev.KINDS)}},
         "cap": {"type": "integer", "minimum": 1, "description": "events per topic when no topic is given (default 8)"}}}},
    {"name": "facts", "description": "Keyword search over ledger events (summaries, values, topic names). "
                                     "Digits and spelled-out numbers both match; L_C_D_ = LCD.",
     "inputSchema": {"type": "object", "required": ["team", "query"], "properties": {
         "team": _TEAM, "query": {"type": "string"}, "as_of": _ASOF,
         "kinds": {"type": "array", "items": {"type": "string", "enum": list(ev.KINDS)}},
         "limit": {"type": "integer", "minimum": 1, "maximum": 50}}}},
    {"name": "actions", "description": "Commitments (\"I'll check / find out ...\") with owner, and whether and "
                                       "where they were reported back.",
     "inputSchema": {"type": "object", "required": ["team"], "properties": {
         "team": _TEAM, "as_of": _ASOF, "open_only": {"type": "boolean"}}}},
    {"name": "search", "description": "Full-text search over the raw transcripts of one team (BM25 over ~300-word "
                                      "passages). Use several different wordings. Returns turn ids.",
     "inputSchema": {"type": "object", "required": ["team", "query"], "properties": {
         "team": _TEAM, "query": {"type": "string"}, "as_of": _ASOF,
         "meeting_id": {"type": "string", "description": "restrict to one meeting"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 20}}}},
    {"name": "turns", "description": "Read the real transcript turns. Give `around` (a turn id) for a window of "
                                     f"`radius` turns either side (default {DEFAULT_WINDOW}), or first/last. "
                                     f"Max {MAX_TURN_RANGE} turns per call.",
     "inputSchema": {"type": "object", "required": ["meeting_id"], "properties": {
         "meeting_id": {"type": "string"}, "around": {"type": "integer"},
         "radius": {"type": "integer", "minimum": 1, "maximum": MAX_TURN_RANGE // 2},
         "first": {"type": "integer"}, "last": {"type": "integer"}}}},
    {"name": "speakers", "description": "Per-meeting grid of who each speaker letter is (name, role) as stated in "
                                        "each meeting. A letter can be a different person in another meeting.",
     "inputSchema": {"type": "object", "required": ["team"], "properties": {"team": _TEAM, "as_of": _ASOF}}},
    {"name": "submit_answer", "description": "Submit the final answer (1-3 plain sentences) with 1-3 citations. "
                                             "The code copies and verifies each quote; on rejection fix the "
                                             "named citation and resubmit.",
     "inputSchema": {"type": "object", "required": ["question_id", "answer", "citations"], "properties": {
         "question_id": {"type": "string"}, "answer": {"type": "string"}, "citations": _CITS,
         "team": {"type": "string", "description": "only for ad-hoc questions not in questions.json"}}}},
    {"name": "submit_brief", "description": "Submit the pre-meeting brief: sections, each with points, each point "
                                            "with 1-3 citations from meetings <= as_of.",
     "inputSchema": {"type": "object", "required": ["team", "as_of", "sections"], "properties": {
         "team": _TEAM, "as_of": {"type": "integer", "minimum": 1},
         "sections": {"type": "array", "minItems": 1, "items": {
             "type": "object", "required": ["heading", "points"], "properties": {
                 "heading": {"type": "string"},
                 "points": {"type": "array", "items": {
                     "type": "object", "required": ["text", "citations"], "properties": {
                         "text": {"type": "string"}, "citations": _CITS}}}}}}}}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


class Refused(ValueError):
    pass


class Server:
    def __init__(self, conn=None, env=None):
        self.env = dict(os.environ if env is None else env)
        self.conn = conn or store.connect(self.env.get("MEETMEM_DB") or None)
        self.clamp_team = self.env.get("MEETMEM_AS_OF_TEAM") or None
        o = self.env.get("MEETMEM_AS_OF_ORD")
        self.clamp_ord = int(o) if o not in (None, "") else None
        if (self.clamp_team is None) != (self.clamp_ord is None):
            raise SystemExit("set both MEETMEM_AS_OF_TEAM and MEETMEM_AS_OF_ORD, or neither")
        self.run = self.env.get("MEETMEM_RUN") or "mcp"
        self.source = self.env.get("MEETMEM_SOURCE") or "mcp"
        self.only_qid = self.env.get("MEETMEM_QUESTION_ID") or None
        self.default_team = self.env.get("MEETMEM_TEAM") or None
        self.initialized = False

    # ------------------------------------------------------------------ clamp
    def _team(self, team):
        team = str(team or "").strip().upper()
        if not team:
            raise Refused("team is required")
        if self.clamp_team and team != self.clamp_team:
            raise Refused(f"this session is limited to team {self.clamp_team}")
        return team

    def _asof(self, team, as_of):
        a = None if as_of in (None, "") else int(as_of)
        if self.clamp_team and team == self.clamp_team:
            a = self.clamp_ord if a is None else min(a, self.clamp_ord)
        return a

    def _meeting(self, meeting_id):
        mid = str(meeting_id or "").strip()
        r = self.conn.execute("SELECT team, ord FROM meetings WHERE meeting_id=?", (mid,)).fetchone()
        if r is None:
            raise Refused(f"unknown meeting {mid!r}")
        team = self._team(r[0])
        a = self._asof(team, None)
        if a is not None and r[1] > a:
            raise Refused(f"{mid} is after the as-of cutoff (meeting {a} of {team}) and cannot be read in this session")
        return mid, team, a

    # ------------------------------------------------------------------ tools
    def t_meetings(self, a):
        team = self._team(a.get("team")); cut = self._asof(team, None)
        tools._check_team(self.conn, team)
        ms = [m for m in store.team_meetings(self.conn, team) if cut is None or m["ord"] <= cut]
        lines = [f"{m['meeting_id']}  ord {m['ord']}  {m['n_turns']} turns (t0-t{m['n_turns'] - 1})  {m['n_words']} words"
                 for m in ms]
        tail = (f"(as-of cutoff: meeting {cut}; later meetings are hidden)" if cut is not None
                else f"(final meeting: {ms[-1]['meeting_id']})" if ms else "")
        return "\n".join(lines + [tail])

    def t_topics(self, a):
        team = self._team(a.get("team"))
        return tools.format_topics(tools.topics_view(self.conn, team, self._asof(team, a.get("as_of"))))

    def t_timeline(self, a):
        team = self._team(a.get("team"))
        return tools.timeline(self.conn, team, self._asof(team, a.get("as_of")), topic=a.get("topic") or None,
                              kinds=a.get("kinds") or None, per_topic_cap=int(a.get("cap") or 8))

    def t_facts(self, a):
        team = self._team(a.get("team")); cut = self._asof(team, a.get("as_of"))
        rows = tools.facts(self.conn, str(a.get("query") or ""), team, cut, kinds=a.get("kinds") or None,
                           limit=int(a.get("limit") or 20))
        return tools.format_events(self.conn, team, rows, cut)

    def t_actions(self, a):
        team = self._team(a.get("team"))
        return tools.format_actions(tools.actions(self.conn, team, self._asof(team, a.get("as_of")),
                                                  open_only=bool(a.get("open_only"))))

    def t_search(self, a):
        team = self._team(a.get("team")); cut = self._asof(team, a.get("as_of"))
        mid = a.get("meeting_id") or None
        if mid:
            mid, mteam, _ = self._meeting(mid)
            if mteam != team:
                raise Refused(f"{mid} belongs to {mteam}, not {team}")
        hits = search_passages(str(a.get("query") or ""), team=team, meeting_id=mid, as_of_ord=cut,
                               limit=min(int(a.get("limit") or 6), 20), conn=self.conn)
        if not hits:
            return "no hits; try other wordings (synonyms, spelled-out numbers, the feature's other names)"
        return "\n\n".join(f"== {h['meeting_id']} turns t{h['first_turn']}-t{h['last_turn']} score {h['score']:.1f}\n"
                           f"{h['text_display']}" for h in hits)

    def t_turns(self, a):
        mid, team, cut = self._meeting(a.get("meeting_id"))
        if a.get("around") is not None:
            r = int(a.get("radius") or DEFAULT_WINDOW)
            first, last = int(a["around"]) - r, int(a["around"]) + r
        elif a.get("first") is not None:
            first = int(a["first"]); last = int(a["last"]) if a.get("last") is not None else first + 2 * DEFAULT_WINDOW
        else:
            raise Refused("give around (a turn id) or first/last")
        first = max(0, first)
        last = min(last, first + MAX_TURN_RANGE - 1)
        rows = tools.read_turns(self.conn, mid, first, last, team=team, as_of_ord=cut)
        if not rows:
            return f"no turns t{first}-t{last} in {mid}"
        n = self.conn.execute("SELECT n_turns FROM meetings WHERE meeting_id=?", (mid,)).fetchone()[0]
        return f"{mid} t{first}-t{min(last, n - 1)} of t0-t{n - 1}\n" + format_turns(rows)

    def t_speakers(self, a):
        team = self._team(a.get("team")); tools._check_team(self.conn, team)
        return ev.SpeakerView(self.conn, team, as_of_ord=self._asof(team, a.get("as_of"))).grid_text()

    def t_submit_answer(self, a):
        qid = str(a.get("question_id") or "").strip()
        if self.only_qid and qid != self.only_qid:
            raise Refused(f"this session answers {self.only_qid} only; use question_id {self.only_qid!r}")
        answer = str(a.get("answer") or "").strip()
        n_sent = len(re.findall(r"[.!?](\s|$)", answer))
        if n_sent > MAX_ANSWER_SENTENCES:
            return {"ok": False, "problems": [f"answer has {n_sent} sentences; rewrite it in at most "
                                              f"{MAX_ANSWER_SENTENCES} plain sentences and resubmit"]}
        team = a.get("team") or None
        q = tools.lookup_question(qid)
        if q is not None:
            team = q["team"]
        team = self._team(team or self.default_team)
        cut = self._asof(team, None)
        if cut is not None:  # clamped session: no citation may come from after the cutoff
            _, probs, _ = tools.resolve_citations(self.conn, a.get("citations"), team, as_of_ord=cut)
            if probs:
                return {"ok": False, "problems": probs}
        rep = tools.answer_submit(self.conn, self.run, qid, {"answer": answer, "citations": a.get("citations")},
                                  source=self.source, team=team)
        if not rep["ok"]:
            rep["hint"] = ("re-point each rejected citation at a turn with >= 6 words that you have read with "
                           "`turns`, then resubmit all citations")
        return rep

    def t_submit_brief(self, a):
        team = self._team(a.get("team"))
        as_of = self._asof(team, a.get("as_of"))
        if as_of is None:
            raise Refused("as_of is required")
        data = {"sections": a.get("sections")}
        rep = tools.brief_submit(self.conn, self.run, team, as_of, data, source=self.source)
        if rep["ok"]:
            n = sum(len(str(p.get("text", "")).split()) for s in a.get("sections") or [] for p in s.get("points") or []
                    if isinstance(p, dict))
            if not 250 <= n <= 450:
                rep["warnings"].append(f"point text is {n} words; target 250-450 (resubmit to replace)")
            rep["words"] = n
        return rep

    # ------------------------------------------------------------------ JSON-RPC
    def call_tool(self, name, args):
        if name not in TOOL_NAMES:
            return {"content": [{"type": "text", "text": f"unknown tool {name!r}"}], "isError": True}
        try:
            out = getattr(self, "t_" + name)(args or {})
        except (Refused, tools.ToolError, ev.SubmitError, ValueError, TypeError, KeyError) as e:
            return {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
        if isinstance(out, dict):
            text = json.dumps(out, ensure_ascii=False, indent=1)
            return {"content": [{"type": "text", "text": text}], "isError": out.get("ok") is False}
        return {"content": [{"type": "text", "text": str(out)}], "isError": False}

    def handle(self, msg):
        """One JSON-RPC message -> response dict, or None for notifications."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        is_note = "id" not in msg
        if method == "notifications/initialized":
            self.initialized = True
            return None
        if is_note:
            return None
        if method == "initialize":
            want = params.get("protocolVersion")
            ver = want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _ok(mid, {"protocolVersion": ver, "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                             "instructions": "Meeting-memory tools. The ledger is an index; always read the real "
                                             "turns before answering, and cite by turn id."
                                             + (f" Clamped: team {self.clamp_team}, meetings 1..{self.clamp_ord}."
                                                if self.clamp_team else "")})
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": TOOLS})
        if method == "tools/call":
            return _ok(mid, self.call_tool(params.get("name"), params.get("arguments") or {}))
        return _err(mid, -32601, f"method not found: {method}")


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(stdin=None, stdout=None, server=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = server or Server()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            resp = _err(None, -32700, f"parse error: {e}")
        else:
            try:
                resp = server.handle(msg)
            except Exception as e:  # never die on one bad call
                traceback.print_exc(file=sys.stderr)
                resp = _err(msg.get("id") if isinstance(msg, dict) else None, -32603, f"internal error: {e}")
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


def main():
    serve()


if __name__ == "__main__":
    main()
