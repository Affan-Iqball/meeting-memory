"""A small local web interface over the meeting memory.

    python3 -m meetmem.web            # http://127.0.0.1:8765
    python3 -m meetmem.web --port 9000 --run official

Read-only except "Ask", which runs the answering agent (needs a Claude login
or ANTHROPIC_API_KEY). Standard library only: the page is meetmem/web_index.html
and every view calls the same functions the agent's tools use, so what a person
sees here is exactly what the agent sees.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import events as ev, search, store, tools

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web_index.html"


def _int(v):
    return None if v in (None, "", "all") else int(v)


class Api:
    def __init__(self, db=None, run="official"):
        self.db, self.run = db, run
        self._local = threading.local()

    @property
    def conn(self):
        # one connection per server thread: sqlite connections cannot cross threads
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = store.connect(self.db)
            c.row_factory = sqlite3.Row
        return c

    # ---- views -------------------------------------------------------
    def teams(self, q):
        out = []
        for t in [r[0] for r in self.conn.execute("SELECT DISTINCT team FROM meetings ORDER BY team")]:
            ms = [dict(r) for r in self.conn.execute(
                "SELECT meeting_id, letter, ord, n_turns, n_words FROM meetings WHERE team=? ORDER BY ord", (t,))]
            n_ev = self.conn.execute("SELECT COUNT(*) FROM events WHERE team=?", (t,)).fetchone()[0]
            out.append({"team": t, "meetings": ms, "n_events": n_ev})
        qs = {}
        for path in (tools.QUESTIONS_PATH, tools.GOLD_PATH):
            try:
                for x in json.load(open(path))["questions"]:
                    qs[x["question_id"]] = {"team": x["team"], "question": x["question"]}
            except (OSError, KeyError, ValueError):
                pass
        return {"teams": out, "questions": qs, "run": self.run}

    def topics(self, q):
        team, cut = q["team"], _int(q.get("as_of"))
        return tools.topics_view(self.conn, team, cut)

    def events(self, q):
        team, cut = q["team"], _int(q.get("as_of"))
        view = ev.SpeakerView(self.conn, team, as_of_ord=cut)
        rows = ev.team_events(self.conn, team, as_of_ord=cut, topic=q.get("topic") or None)
        st = ev.derive_status(rows) if q.get("topic") else None
        keep = ("event_id", "meeting_id", "ord", "at_turn", "time_str", "topic_slug", "kind", "summary",
                "speaker", "owner", "value", "evidence_turn", "evidence_quote", "resolves_event_id")
        out = []
        for e in rows:
            d = {k: e.get(k) for k in keep}
            d["speaker_label"] = view.label(e["meeting_id"], e["speaker"]) if e["speaker"] else None
            d["time_str"] = tools._hms(e.get("start_sec")) if e.get("start_sec") is not None else None
            out.append(d)
        return {"status": st, "events": out}

    def actions(self, q):
        return tools.actions(self.conn, q["team"], _int(q.get("as_of")))

    def speakers(self, q):
        return ev.speaker_table(self.conn, q["team"], as_of_ord=_int(q.get("as_of")))

    def turns(self, q):
        mid = q["meeting"]
        focus = int(q.get("focus", 0))
        span = int(q.get("span", 12))
        n = self.conn.execute("SELECT n_turns FROM meetings WHERE meeting_id=?", (mid,)).fetchone()
        if not n:
            raise tools.ToolError(f"unknown meeting {mid}")
        first, last = max(0, focus - span), min(n[0] - 1, focus + span)
        return {"meeting_id": mid, "n_turns": n[0], "focus": focus,
                "turns": tools.read_turns(self.conn, mid, first, last)}

    def search(self, q):
        team, cut = q.get("team") or None, _int(q.get("as_of"))
        text = q.get("q", "")
        passages = search.search_passages(text, team=team, as_of_ord=cut if team else None, limit=10,
                                          conn=self.conn)
        facts = tools.facts(self.conn, text, team, cut, limit=20) if team else []
        return {"passages": passages, "facts": facts}

    def answers(self, q):
        run = q.get("run") or self.run
        rows = self.conn.execute("SELECT question_id, team, answer, citations_json, source FROM answers "
                                 "WHERE run=? ORDER BY question_id", (run,)).fetchall()
        return [{"question_id": r[0], "team": r[1], "answer": r[2], "citations": json.loads(r[3]),
                 "source": r[4]} for r in rows]

    def runs(self, q):
        return [r[0] for r in self.conn.execute("SELECT DISTINCT run FROM answers ORDER BY run")]

    def brief(self, q):
        run = q.get("run") or self.run
        r = self.conn.execute("SELECT team, as_of_ord, sections_json FROM briefs WHERE run=? AND team=?",
                              (run, q["team"])).fetchone()
        return None if r is None else {"team": r[0], "as_of": r[1], "sections": json.loads(r[2])}

    def ask(self, body):
        from . import agent  # imported lazily: needs the claude CLI only when used
        team, question = body.get("team"), (body.get("question") or "").strip()
        if not team or not question:
            raise tools.ToolError("pick a team and type a question")
        return agent.ask(self.conn, team=team, question=question, run="web", force=True)


ROUTES = {"teams", "topics", "events", "actions", "speakers", "turns", "search", "answers", "runs", "brief"}


def make_handler(api: Api):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, payload, ctype="application/json"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *a):  # quiet
            pass

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html")
            name = u.path.removeprefix("/api/")
            if u.path.startswith("/api/") and name in ROUTES:
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                try:
                    return self._send(200, getattr(api, name)(q))
                except (tools.ToolError, KeyError, ValueError) as e:
                    return self._send(400, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/ask":
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                return self._send(200, api.ask(body))
            except Exception as e:  # surface any backend failure (no login, usage limit) to the page
                return self._send(400, {"error": f"{type(e).__name__}: {e}"})
    return H


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m meetmem.web")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--db")
    p.add_argument("--run", default="official", help="which answer run to show first")
    a = p.parse_args(argv)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(Api(a.db, a.run)))
    print(f"meeting memory: http://{a.host}:{a.port}  (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
