"""A small local web interface over the meeting memory.

    python3 -m meetmem.web            # http://127.0.0.1:8765
    python3 -m meetmem.web --port 9000 --run official

Browsing is read-only and free. The Setup tab connects a model (an Anthropic API
key, or the Claude subscription login) and the Run tab starts model jobs (ask, answer
all, brief, extract) in the background, one at a time, with live logs and cost.
Settings live in ~/.config/meetmem/settings.json (mode 600), never in the project.
Standard library only; it listens on 127.0.0.1 and refuses cross-site POSTs.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import events as ev, search, settings, store, tools

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

    # ---- setup ------------------------------------------------------
    def settings(self, q):
        return {**settings.status(with_version=True), "estimates": ESTIMATES}

    def jobs(self, q):
        return JOBS.list()

    def usage(self, q):
        from . import agent
        return {"text": agent.usage(self.conn, q.get("run") or None)}

    def post_settings(self, body):
        if body.get("clear_key"):
            settings.set_key(None, remember=False)
        elif body.get("api_key"):
            k = str(body["api_key"]).strip()
            if not k.startswith("sk-") or len(k) < 20:
                raise ValueError("that does not look like an Anthropic API key (it should start with sk-)")
            settings.set_key(k, remember=bool(body.get("remember")))
        settings.set_models(answer_model=body.get("answer_model"), extract_model=body.get("extract_model"),
                            claude_bin=body.get("claude_bin"))
        return settings.status(with_version=True)

    def post_jobs(self, body):
        kind = body.get("kind")
        if kind not in JOB_KINDS:
            raise ValueError(f"unknown job kind {kind!r}")
        return JOBS.submit(kind, {k: v for k, v in body.items() if k != "kind"}, self.db)


ROUTES = {"teams", "topics", "events", "actions", "speakers", "turns", "search", "answers", "runs", "brief",
          "settings", "jobs", "usage"}
POST_ROUTES = {"settings": "post_settings", "jobs": "post_jobs"}

# rough per-call costs at API list prices, from measured tokens (see WRITEUP); shown before paid runs
ESTIMATES = {"answer": {"haiku": 0.02, "sonnet": 0.12, "opus": 0.24},
             "brief": {"haiku": 0.04, "sonnet": 0.25, "opus": 0.50},
             "extract_per_meeting": {"haiku": 0.0044, "sonnet": 0.09, "opus": 0.18}}


# --------------------------------------------------------------------------- background jobs

JOB_KINDS = {"test", "ask", "ask_all", "brief", "extract", "export", "ingest"}
FREE_KINDS = {"export", "ingest"}


class Jobs:
    """One worker thread runs jobs in order, so model calls and database writes never overlap."""

    def __init__(self):
        self._q, self._jobs, self._ids, self._lock = queue.Queue(), {}, itertools.count(1), threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, kind, params, db):
        jid = next(self._ids)
        job = {"id": jid, "kind": kind, "params": params, "status": "queued", "log": [], "result": None,
               "error": None, "cost_usd": 0.0, "tokens": 0, "queued_at": time.time(), "started_at": None,
               "finished_at": None}
        with self._lock:
            self._jobs[jid] = job
        self._q.put((jid, db))
        return job

    def list(self):
        with self._lock:
            return sorted((dict(j, log=j["log"][-200:]) for j in self._jobs.values()), key=lambda j: -j["id"])

    def _log(self, job, msg):
        with self._lock:
            job["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")

    def _worker(self):
        while True:
            jid, db = self._q.get()
            job = self._jobs[jid]
            job["status"], job["started_at"] = "running", time.time()
            conn = store.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                job["result"] = run_job(conn, job, lambda m: self._log(job, m))
                job["status"] = "done"
            except Exception as e:  # report every failure on the page
                from . import agent
                job["status"] = "stopped: usage limit" if isinstance(e, agent.UsageLimit) else "failed"
                job["error"] = f"{type(e).__name__}: {e}"
                self._log(job, job["error"])
                if not isinstance(e, (agent.UsageLimit, agent.BackendError, tools.ToolError, ValueError)):
                    self._log(job, traceback.format_exc(limit=3))
            finally:
                job["finished_at"] = time.time()
                try:
                    from . import agent
                    if job["kind"] == "test" and isinstance(job["result"], dict):
                        job["cost_usd"] = float(job["result"].get("cost_usd") or 0)
                    elif job["kind"] not in FREE_KINDS:
                        agent.ensure_table(conn)  # jobs run one at a time, so this window is this job's calls
                        r = conn.execute("SELECT sum(cost_usd), sum(input_tokens+output_tokens+cache_tokens) "
                                         "FROM llm_runs WHERE at >= ? AND at <= ?",
                                         (job["started_at"], job["finished_at"])).fetchone()
                        job["cost_usd"], job["tokens"] = float(r[0] or 0), int(r[1] or 0)
                except sqlite3.Error:
                    pass
                conn.close()


def run_job(conn, job, log):
    from . import agent
    settings.apply()
    p, m = job["params"], settings.models()
    kind = job["kind"]
    if kind == "test":
        return test_connection(log)
    if kind == "ask":
        team, question = p.get("team"), (p.get("question") or "").strip()
        if not team or not question:
            raise tools.ToolError("pick a team and type a question")
        log(f"asking {m['answer_model']} about {team}: {question}")
        r = agent.ask(conn, team=team, question=question, run=p.get("run") or "web", model=m["answer_model"],
                      force=True)
        log(f"status: {r['status']}" + (f" ({r.get('error')})" if r.get("error") else ""))
        return r
    if kind == "ask_all":
        run = (p.get("run") or "live").strip()
        log(f"answering the 25 official questions into run '{run}' with {m['answer_model']} (answered ones are skipped)")
        counts = agent.ask_all(conn, tools.QUESTIONS_PATH, run, model=m["answer_model"], force=bool(p.get("force")),
                               log=log)
        log(f"finished: {counts}")
        return counts
    if kind == "brief":
        team, as_of, run = p.get("team") or "TS3012", int(p.get("as_of") or 3), (p.get("run") or "live").strip()
        log(f"writing the brief for {team} as of meeting {as_of} into run '{run}' with {m['answer_model']}")
        r = agent.brief(conn, team, as_of, run, model=m["answer_model"], force=True)
        log(f"status: {r['status']}" + (f" ({r.get('error')})" if r.get("error") else ""))
        return r
    if kind == "extract":
        team = p.get("team")
        tools._check_team(conn, team)
        log(f"extracting {team} with {m['extract_model']} (parts already extracted are skipped)")
        counts = agent.extract(conn, team, model=m["extract_model"], force=bool(p.get("force")), log=log)
        log(f"finished: {counts}")
        return counts
    if kind == "export":
        run = (p.get("run") or "official").strip()
        data, notes = tools.export_answers(conn, run, brief_team="TS3012")
        out_dir = os.path.join(store.PROJECT_ROOT, "runs", run)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "answers_export.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
        for n in notes:
            log(n)
        log(f"wrote {os.path.relpath(path, store.PROJECT_ROOT)}; checking it word for word…")
        chk = subprocess.run([sys.executable, "-m", "meetmem.check", path], capture_output=True, text=True,
                             cwd=store.PROJECT_ROOT, timeout=300)
        tail = (chk.stdout or chk.stderr).strip().splitlines()[-1:] or [""]
        log(tail[0])
        return {"path": os.path.relpath(path, store.PROJECT_ROOT), "check": tail[0], "ok": chk.returncode == 0}
    if kind == "ingest":
        log("re-ingesting data/transcripts (code only; existing events are kept)")
        r = subprocess.run([sys.executable, "-m", "meetmem", "ingest", os.path.join(store.PROJECT_ROOT, "data", "transcripts")],
                           capture_output=True, text=True, cwd=store.PROJECT_ROOT, timeout=600,
                           env={**os.environ, "MEETMEM_DB": conn.execute("PRAGMA database_list").fetchone()[2]})
        for line in (r.stdout or r.stderr).strip().splitlines()[-3:]:
            log(line)
        return {"ok": r.returncode == 0}
    raise ValueError(kind)


def test_connection(log):
    """One tiny call (Haiku, no tools) to prove the CLI and the sign-in work."""
    from . import agent
    b = settings.detect_claude()
    if not b:
        raise agent.BackendError("the Claude command-line tool was not found: install it or set its path in Setup")
    key, src = settings.current_key()
    log(f"Claude tool: {b}")
    log(f"sign-in: {'API key (' + src + ')' if key else 'subscription login'}")
    cmd = [b, "-p", "--output-format", "json", "--model", "haiku", "--max-turns", "1",
           "--no-session-persistence", "--disable-slash-commands", "--tools", ""]
    if key:
        cmd.insert(2, "--bare")
    t0 = time.time()
    p = subprocess.run(cmd, input="Reply with exactly the word OK.", capture_output=True, text=True, timeout=180)
    res = agent.parse_result(p.stdout)
    ms = int((time.time() - t0) * 1000)
    if res is None:
        raise agent.BackendError(f"no answer from the Claude tool (exit {p.returncode}): {(p.stderr or p.stdout).strip()[-400:]}")
    if agent.is_usage_limit(res, p.stdout):
        raise agent.BackendError(f"the Claude account hit its usage limit ({str(res.get('result') or '')[:200]}). "
                                 "Add an API key in step 2, or wait for the reset and test again.")
    if res.get("is_error"):
        raise agent.BackendError(f"{res.get('api_error') or 'error'}: {str(res.get('result') or '')[:300]}")
    log(f"reply: {str(res.get('result'))[:60]!r} in {ms} ms, cost ${float(res.get('total_cost_usd') or 0):.4f}")
    return {"ok": True, "reply": res.get("result"), "ms": ms, "cost_usd": res.get("total_cost_usd")}


JOBS = Jobs()


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
            # only our own page may change settings or start jobs: a custom header cannot be sent
            # cross-site without a CORS preflight, which this server never answers
            host = (self.headers.get("Host") or "").split(":")[0]
            if self.headers.get("X-Meetmem") != "1" or host not in ("127.0.0.1", "localhost"):
                return self._send(403, {"error": "forbidden"})
            name = urlparse(self.path).path.removeprefix("/api/")
            if name not in POST_ROUTES:
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                return self._send(200, getattr(api, POST_ROUTES[name])(body))
            except Exception as e:  # show any failure on the page
                return self._send(400, {"error": f"{type(e).__name__}: {e}"})
    return H


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m meetmem.web")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--db")
    p.add_argument("--run", default="official", help="which answer run to show first")
    a = p.parse_args(argv)
    settings.apply()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(Api(a.db, a.run)))
    print(f"meeting memory: http://{a.host}:{a.port}  (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
