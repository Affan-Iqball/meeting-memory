"""Answering layer tests: per-meeting speakers, the stdio MCP server, and the
Claude Code backend driven by a FAKE `claude` executable (no real model call).

    python3 -m unittest discover -s tests -v

Temp database only; memory.db is never opened.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from meetmem import agent, cli, events as ev, store, tools  # noqa: E402

TRANSCRIPTS = os.path.join(ROOT, "data", "transcripts")
TEAMS = ("TS3004", "IS1008", "TS3006", "IS1001", "TS3012", "IS1005")
TMP = DB = CONN = None
SR = "speech-recognition"


def setUpModule():
    global TMP, DB, CONN
    TMP = tempfile.mkdtemp(prefix="meetmem_answering_")
    DB = os.path.join(TMP, "t.db")
    CONN = store.connect(DB)
    for f in sorted(os.listdir(TRANSCRIPTS)):
        if f.startswith(TEAMS):
            store.ingest_file(CONN, os.path.join(TRANSCRIPTS, f))
    # a small TS3004 speech-recognition ledger (hand-written, as in test_events)
    sub("TS3004b", {"topics": [{"slug": SR, "label": "Speech recognition", "aliases": ["voice recognition"]}],
                    "events": [evt(SR, "rejected", 67, "D proposes skipping speech recognition", "skip speech recognition", "D")]})
    sub("TS3004c", {"events": [evt(SR, "reinstated", 567, "A asks to put speech recognition back in", None, "A")]}, part=2)


def tearDownModule():
    CONN.close()
    shutil.rmtree(TMP, ignore_errors=True)


def evt(topic, kind, at, summary, contains=None, speaker=None, **kw):
    d = {"topic": topic, "kind": kind, "summary": summary, "at_turn": at,
         "evidence": {"turn_idx": at, "contains": contains}}
    if speaker:
        d["speaker"] = speaker
    d.update(kw)
    return d


def sub(mid, data, part=1):
    rep = ev.submit_events(CONN, mid, data, part=part, source="test")
    assert not rep["rejected"], rep["rejected"]
    return rep


def long_turn(mid, speaker, min_words=10):
    r = CONN.execute("SELECT turn_idx FROM turns WHERE meeting_id=? AND speaker=? AND n_words>=? ORDER BY turn_idx LIMIT 1",
                     (mid, speaker, min_words)).fetchone()
    assert r, (mid, speaker)
    return r[0]


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--db", DB, *map(str, argv)])
    return code, out.getvalue(), err.getvalue()


def dump(obj, path):
    with open(path, "w") as fh:
        json.dump(obj, fh)
    return path


# =========================================================================== speakers

class TestSpeakerIdentity(unittest.TestCase):
    def test_same_person(self):
        self.assertTrue(ev.same_person("Paul", "Paul Wiezer"))
        self.assertTrue(ev.same_person("shrida daseri", "Shrida"))
        self.assertFalse(ev.same_person("Paul", "Pauline"))
        self.assertFalse(ev.same_person("Shrida", "Sridhar"))
        g = ev.group_names(["Paul", "Paul Wiezer"])
        self.assertEqual(g, {"Paul Wiezer": {"Paul", "Paul Wiezer"}})
        g = ev.group_names(["Paul", "Paul Wiezer", "Paul Smith"])  # ambiguous: "Paul" stays on its own
        self.assertIn("Paul", g)

    def test_first_name_variant_is_not_a_conflict(self):
        sub("IS1008b", {"speakers": [{"speaker": "A", "name": "Shrida Daseri", "role": "project manager", "turn_idx": 2}]})
        try:
            st = ev.speaker_table(CONN, "IS1008")
            self.assertEqual(st["A"]["name"], "Shrida Daseri")
            self.assertFalse(st["A"]["conflicts"], st["A"])
            self.assertIn("Shrida", st["A"]["names"])  # raw variants kept for owner mapping on the submit path
            v = ev.SpeakerView(CONN, "IS1008")
            self.assertNotIn("conflict", v.label("IS1008a", "A"))
            self.assertTrue(v.label("IS1008a", "A").startswith("A (Shrida Daseri"), v.label("IS1008a", "A"))
            self.assertFalse(v.conflicts("A"))
        finally:
            sub("IS1008b", {})

    def test_ts3006_letter_is_a_different_person_per_meeting(self):
        # TS3006c B "May" comes from the regex self-intro pass at ingest
        self.assertTrue(CONN.execute("SELECT 1 FROM speakers WHERE meeting_id='TS3006c' AND speaker='B' AND name='May'").fetchone())
        sub("TS3006a", {"speakers": [{"speaker": "B", "name": "Julian", "role": "user interface designer", "turn_idx": 133}]})
        tb = long_turn("TS3006c", "B")
        ta = long_turn("TS3006a", "B")
        sub("TS3006c", {"events": [evt("lcd-screen", "proposed", tb, "B suggests the screen", speaker="B")]})
        sub("TS3006a", {"speakers": [{"speaker": "B", "name": "Julian", "role": "user interface designer", "turn_idx": 133}],
                        "events": [evt("lcd-screen", "info", ta, "B talks", speaker="B")]})
        v = ev.SpeakerView(CONN, "TS3006")
        self.assertEqual(v.label("TS3006a", "B"), "B (Julian, user interface designer; elsewhere B = May in TS3006c)")
        lc = v.label("TS3006c", "B")
        self.assertTrue(lc.startswith("B (May"), lc)
        self.assertNotIn("user interface designer", lc)  # Julian's role is not lent to May
        self.assertIn("Julian in TS3006a", lc)
        lb = v.label("TS3006b", "B")  # nothing stated in b: fallback, marked as such
        self.assertIn("Julian or May?", lb)
        self.assertIn("from other meetings", lb)
        self.assertTrue(any("name differs" in c for c in v.conflicts("B")))
        out = tools.timeline(CONN, "TS3006", topic="lcd-screen")
        self.assertIn("TS3006a 00:", out)
        self.assertIn("B (Julian, user interface designer;", out)
        self.assertIn("B (May;", out)
        code, grid, _ = run_cli("speakers", "--team", "TS3006")
        self.assertEqual(code, 0)
        self.assertIn("TS3006a", grid.splitlines()[3])
        self.assertIn("Julian, user interface designer", grid)
        self.assertIn("May", grid)
        self.assertIn("B: name differs: Julian (TS3006a); May (TS3006c)", grid)
        code, js, _ = run_cli("speakers", "--team", "TS3006", "--json")
        g = json.loads(js)
        self.assertIsNone(g["letters"]["B"]["per_meeting"]["TS3006b"])

    def test_is1001_roles_swap_between_meetings(self):
        sub("IS1001b", {"speakers": [{"speaker": "B", "role": "user interface designer", "turn_idx": 5},
                                     {"speaker": "C", "role": "industrial designer", "turn_idx": 5}]})
        tc = long_turn("IS1001c", "B")
        sub("IS1001c", {"speakers": [{"speaker": "B", "role": "industrial designer", "turn_idx": 5},
                                     {"speaker": "C", "role": "user interface designer", "turn_idx": 5}],
                        "events": [evt("prototype", "action_assigned", tc, "B will build the prototype", speaker="B", owner="B")]})
        v = ev.SpeakerView(CONN, "IS1001")
        self.assertEqual(v.label("IS1001b", "B"), "B (user interface designer; elsewhere B = industrial designer in IS1001c)")
        self.assertEqual(v.label("IS1001c", "C"), "C (user interface designer; elsewhere C = industrial designer in IS1001b)")
        # nothing stated in d: both candidates, in meeting order, marked as a fallback
        self.assertEqual(v.label("IS1001d", "B"), "B (user interface designer or industrial designer?, from other meetings)")
        acts = tools.actions(CONN, "IS1001")
        self.assertTrue(acts[0]["owner_label"].startswith("B (industrial designer;"), acts[0])
        code, grid, _ = run_cli("speakers", "--team", "IS1001")
        self.assertIn("B: role differs", grid)
        self.assertIn("C: role differs", grid)

    def test_extraction_prompt_context_unchanged(self):
        # the live workers' prompt keeps the old (unmerged) speaker table
        sub("TS3004a", {"speakers": [{"speaker": "B", "name": "Paul", "turn_idx": 5}]})
        sub("TS3004c", {"speakers": [{"speaker": "B", "name": "Paul Wiezer", "turn_idx": 567}],  # part 2 is t265-t605
                        "events": [evt(SR, "reinstated", 567, "A asks to put speech recognition back in", None, "A")]}, part=2)
        p = tools.extract_prompt(CONN, "TS3004d", 1)
        self.assertIn("CONFLICT", p)
        st = ev.speaker_table(CONN, "TS3004")
        self.assertEqual((st["B"]["name"], st["B"]["conflicts"]), ("Paul Wiezer", []))
        self.assertEqual(ev.SpeakerView(CONN, "TS3004").label("TS3004a", "B"), "B (Paul Wiezer)")


# =========================================================================== MCP server

class McpClient:
    def __init__(self, env_extra=None):
        env = dict(os.environ, MEETMEM_DB=DB, PYTHONPATH=ROOT, **(env_extra or {}))
        for k in ("MEETMEM_AS_OF_TEAM", "MEETMEM_AS_OF_ORD", "MEETMEM_QUESTION_ID"):
            if k not in (env_extra or {}):
                env.pop(k, None)
        self.p = subprocess.Popen([sys.executable, "-m", "meetmem.mcp_server"], cwd=ROOT, env=env, text=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.n = 0

    def request(self, method, params=None):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        resp = json.loads(self.p.stdout.readline())
        assert resp["id"] == self.n, resp
        return resp

    def notify(self, method):
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.p.stdin.flush()

    def call(self, name, **args):
        r = self.request("tools/call", {"name": name, "arguments": args})["result"]
        return r["content"][0]["text"], r["isError"]

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)
        self.p.stdout.close(); self.p.stderr.close()


class TestMcpServer(unittest.TestCase):
    def test_protocol_and_tools(self):
        c = McpClient()
        try:
            init = c.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "test", "version": "0"}})["result"]
            self.assertEqual(init["protocolVersion"], "2025-06-18")
            self.assertIn("tools", init["capabilities"])
            c.notify("notifications/initialized")  # no response expected
            self.assertEqual(c.request("ping")["result"], {})
            names = {t["name"] for t in c.request("tools/list")["result"]["tools"]}
            self.assertEqual(names, {"meetings", "topics", "timeline", "facts", "actions", "search", "turns",
                                     "speakers", "submit_answer", "submit_brief"})
            for t in c.request("tools/list")["result"]["tools"]:
                self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertEqual(c.request("nope")["error"]["code"], -32601)
            text, err = c.call("timeline", team="TS3004", topic=SR)
            self.assertFalse(err)
            self.assertIn("sequence: b:rejected -> c:reinstated", text)
            self.assertIn("-> evidence TS3004c t567", text)
            text, err = c.call("turns", meeting_id="TS3004c", around=567, radius=3)
            self.assertIn("[t567 A 00:39:13]", text)
            self.assertEqual(len([l for l in text.splitlines() if l.startswith("[t")]), 7)
            text, err = c.call("search", team="TS3004", query="L_C_D_ display", limit=2)
            self.assertFalse(err); self.assertIn("== TS3004", text)
            text, err = c.call("meetings", team="TS3004")
            self.assertIn("final meeting: TS3004d", text)
            # bad citation: a 4-word turn -> rejected with a reason, nothing stored
            text, err = c.call("submit_answer", question_id="X1", team="TS3004", answer="They dropped it and then put it back.",
                               citations=[{"meeting_id": "TS3004b", "turn_idx": 419}])
            self.assertTrue(err)
            self.assertIn("only 4 words", text)
            self.assertIsNone(CONN.execute("SELECT 1 FROM answers WHERE run='mcp' AND question_id='X1'").fetchone())
            text, err = c.call("submit_answer", question_id="X1", team="TS3004", answer="One. Two. Three. Four.",
                               citations=[{"meeting_id": "TS3004b", "turn_idx": 67}])
            self.assertTrue(err); self.assertIn("4 sentences", text)
            text, err = c.call("submit_answer", question_id="X1", team="TS3004",
                               answer="They dropped it in b and put it back in c.",
                               citations=[{"meeting_id": "TS3004b", "turn_idx": 67, "contains": "skip speech recognition"},
                                          {"meeting_id": "TS3004c", "turn_idx": 567}])
            self.assertFalse(err, text)
            rep = json.loads(text)
            self.assertEqual(rep["citations"][1]["quote"], "Can we just put it speech recognition in it as well, okay?")
            self.assertTrue(CONN.execute("SELECT 1 FROM answers WHERE run='mcp' AND question_id='X1'").fetchone())
            text, err = c.call("timeline", team="NOPE")
            self.assertTrue(err); self.assertIn("unknown team", text)
        finally:
            c.close()

    def test_as_of_clamp(self):
        c = McpClient({"MEETMEM_AS_OF_TEAM": "TS3004", "MEETMEM_AS_OF_ORD": "2", "MEETMEM_RUN": "clamp"})
        try:
            c.request("initialize", {"protocolVersion": "2024-11-05"})
            text, err = c.call("timeline", team="TS3004", topic=SR, as_of=4)  # asks for more: still clamped
            self.assertIn("status as of meeting 2: rejected [out]", text)
            self.assertNotIn("TS3004c", text)
            text, err = c.call("topics", team="TS3004")
            self.assertIn("rejected [out]", text)
            text, err = c.call("turns", meeting_id="TS3004c", around=567)
            self.assertTrue(err); self.assertIn("after the as-of cutoff", text)
            text, err = c.call("search", team="TS3004", query="speech recognition", limit=20)
            self.assertNotIn("TS3004c", text); self.assertNotIn("TS3004d", text)
            text, err = c.call("search", team="TS3004", query="speech", meeting_id="TS3004d")
            self.assertTrue(err)
            text, err = c.call("meetings", team="TS3004")
            self.assertNotIn("TS3004c", text)
            text, err = c.call("topics", team="IS1008")
            self.assertTrue(err); self.assertIn("limited to team TS3004", text)
            text, err = c.call("submit_answer", question_id="X5", team="TS3004", answer="x.",
                               citations=[{"meeting_id": "TS3004c", "turn_idx": 567}])
            self.assertTrue(err); self.assertIn("after the as-of cutoff", text)
            tb = long_turn("TS3004b", "A")
            text, err = c.call("submit_brief", team="TS3004", as_of=4, sections=[
                {"heading": "Decided", "points": [{"text": "A point.", "citations": [{"meeting_id": "TS3004b", "turn_idx": tb}]}]}])
            self.assertFalse(err, text)
            self.assertEqual(json.loads(text)["as_of"], 2)  # the clamp wins over the requested 4
        finally:
            c.close()


# =========================================================================== backend with a fake claude

FAKE_CLAUDE = r'''#!/usr/bin/env python3
"""Fake Claude Code CLI for tests: logs argv/stdin, then behaves by mode."""
import json, os, re, subprocess, sys
argv = sys.argv[1:]
prompt = sys.stdin.read()
state = os.environ["FAKE_STATE"]
n = int(open(state).read()) + 1 if os.path.exists(state) else 1
open(state, "w").write(str(n))
log = {"argv": argv, "stdin": prompt, "cwd": os.getcwd(), "tool_results": []}

def result(text, **kw):
    d = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1500, "duration_api_ms": 1200,
         "num_turns": 4, "result": text, "session_id": "fake", "total_cost_usd": 0.0125,
         "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 5000,
                   "cache_creation_input_tokens": 300}}
    d.update(kw)
    return d

def finish(d, code=0):
    with open(os.environ["FAKE_LOG"], "a") as fh:
        fh.write(json.dumps(log) + "\n")
    print(json.dumps(d))
    sys.exit(code)

lim = os.environ.get("FAKE_LIMIT_AFTER")
if lim and n > int(lim):
    finish(result("Claude usage limit reached. Your limit will reset at 17 Nov.", is_error=True, subtype="error",
                  api_error="usage_limit_reached", num_turns=0, total_cost_usd=0, usage={}), 1)

if "--mcp-config" in argv:
    cfg = json.load(open(argv[argv.index("--mcp-config") + 1]))["mcpServers"]["meetmem"]
    env = dict(os.environ, **cfg["env"])
    p = subprocess.Popen([cfg["command"], *cfg["args"]], env=env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    k = [0]
    def rpc(method, params=None):
        k[0] += 1
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": k[0], "method": method, "params": params or {}}) + "\n"); p.stdin.flush()
        return json.loads(p.stdout.readline())
    def call(name, **a):
        r = rpc("tools/call", {"name": name, "arguments": a})["result"]
        log["tool_results"].append({"tool": name, "isError": r["isError"], "text": r["content"][0]["text"][:400]})
        return r
    rpc("initialize", {"protocolVersion": "2025-06-18"})
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"); p.stdin.flush()
    if cfg["env"].get("MEETMEM_AS_OF_TEAM"):          # brief run: try to peek at the future, then submit
        team = cfg["env"]["MEETMEM_AS_OF_TEAM"]
        call("turns", meeting_id=team + "d", around=10)
        call("submit_brief", team=team, as_of=9, sections=json.loads(os.environ["FAKE_BRIEF"]))
    else:                                              # answer run: bad citation first, then re-point
        qid = cfg["env"]["MEETMEM_QUESTION_ID"]
        call("timeline", team=cfg["env"]["MEETMEM_TEAM"])
        r = call("submit_answer", question_id=qid, answer="They dropped it, then put it back.",
                 citations=[{"meeting_id": "TS3004b", "turn_idx": 419}])
        if r["isError"]:
            call("submit_answer", question_id=qid, answer="They dropped it in b and put it back in c.",
                 citations=json.loads(os.environ["FAKE_CITATIONS"]))
    p.stdin.close(); p.wait()
    finish(result("Submitted."))

# extraction: no tools; reply with JSON built from the transcript in the prompt
turns = [(int(m.group(1)), m.group(2)) for m in re.finditer(r"^\[t(\d+) ([A-Z]) [\d:]+\] (.*)$", prompt, re.M)
         if len(m.group(3).split()) >= 8]
t, spk = turns[0]
good = {"topic": "fake-topic", "kind": "info", "summary": "something was said", "at_turn": t, "speaker": spk,
        "evidence": {"turn_idx": t}}
events = [good]
if "=== SUBMIT REPORT ===" not in prompt:
    events.append({"topic": "fake-topic", "kind": "info", "summary": "bad speaker", "at_turn": t, "speaker": "Q"})
reply = json.dumps({"speakers": [], "topics": [{"slug": "fake-topic", "label": "Fake topic", "aliases": []}], "events": events})
finish(result("```json\n" + reply + "\n```"))
'''


class TestAgentWithFakeClaude(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = os.path.join(TMP, "fake")
        os.makedirs(cls.dir)
        cls.bin = os.path.join(cls.dir, "claude")
        with open(cls.bin, "w") as fh:
            fh.write(FAKE_CLAUDE)
        os.chmod(cls.bin, 0o755)
        cls.log = os.path.join(cls.dir, "log.jsonl")
        cls.state = os.path.join(cls.dir, "n")
        cls.qpath = dump({"questions": [{"question_id": f"X{i}", "team": "TS3004", "question": f"Synthetic question {i}?"}
                                        for i in (1, 2, 3)], "brief_task": {"team": "TS3012"}},
                         os.path.join(cls.dir, "q.json"))
        cls.saved = {k: os.environ.get(k) for k in ("CLAUDE_BIN", "FAKE_LOG", "FAKE_STATE", "FAKE_CITATIONS",
                                                    "FAKE_BRIEF", "FAKE_LIMIT_AFTER", "ANTHROPIC_API_KEY")}
        os.environ.update(CLAUDE_BIN=cls.bin, FAKE_LOG=cls.log, FAKE_STATE=cls.state, FAKE_CITATIONS=json.dumps([
            {"meeting_id": "TS3004b", "turn_idx": 67, "contains": "skip speech recognition"},
            {"meeting_id": "TS3004c", "turn_idx": 567}]))
        os.environ.pop("FAKE_LIMIT_AFTER", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        tb = long_turn("TS3012b", "A")
        os.environ["FAKE_BRIEF"] = json.dumps([{"heading": "Decided", "points": [
            {"text": "A point.", "citations": [{"meeting_id": "TS3012b", "turn_idx": tb}]}]}])

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        for p in (self.log, self.state):
            if os.path.exists(p):
                os.remove(p)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [json.loads(l) for l in fh]

    def test_1_ask_flags_logging_and_resume(self):
        code, out, err = run_cli("ask", "X1", "--run", "fake", "--model", "sonnet")
        # X1 is not in data/questions.json: use the synthetic file through the API instead
        self.assertEqual(code, 2, out)
        r = agent.ask(CONN, "X1", run="fake", model="sonnet", questions_path=self.qpath)
        self.assertEqual(r["status"], "done", r)
        self.assertEqual(r["citations"][1]["quote"], "Can we just put it speech recognition in it as well, okay?")
        (c,) = self.calls()
        a = c["argv"]
        for flag in ("-p", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
            self.assertIn(flag, a)
        pairs = {a[i]: a[i + 1] for i in range(len(a) - 1) if a[i].startswith("--")}
        self.assertEqual(pairs["--output-format"], "json")
        self.assertEqual(pairs["--model"], "sonnet")
        self.assertEqual(pairs["--tools"], "")
        self.assertEqual(pairs["--allowedTools"], "mcp__meetmem")
        self.assertEqual(pairs["--permission-mode"], "dontAsk")
        self.assertEqual(pairs["--max-turns"], "40")
        self.assertNotIn("--bare", a)
        self.assertIn("Synthetic question 1?", pairs["--system-prompt"])
        self.assertIn("Step 3. Read the real turns", pairs["--system-prompt"])
        self.assertNotIn("{{", pairs["--system-prompt"])
        self.assertIn("X1", c["stdin"])
        self.assertNotEqual(os.path.realpath(c["cwd"]), os.path.realpath(ROOT))
        self.assertTrue(c["tool_results"][1]["isError"])        # the bad citation was refused ...
        self.assertFalse(c["tool_results"][2]["isError"])       # ... and the re-pointed one accepted
        row = CONN.execute("SELECT * FROM llm_runs WHERE run='fake' AND target='X1'").fetchone()
        self.assertEqual((row["purpose"], row["model"], row["input_tokens"], row["output_tokens"], row["cache_tokens"],
                          row["num_turns"], row["ok"]), ("answer", "sonnet", 1000, 200, 5300, 4, 1))
        self.assertAlmostEqual(row["cost_usd"], 0.0125)
        self.assertEqual(CONN.execute("SELECT source FROM answers WHERE run='fake' AND question_id='X1'").fetchone()[0],
                         "claude-sonnet")
        # resumable: a second ask is skipped without calling the model
        r = agent.ask(CONN, "X1", run="fake", questions_path=self.qpath)
        self.assertEqual(r["status"], "skipped")
        self.assertEqual(len(self.calls()), 1)

    def test_2_ask_all_stops_on_usage_limit_and_resumes(self):
        os.environ["FAKE_LIMIT_AFTER"] = "1"
        try:
            code, out, err = run_cli("ask-all", "--questions", self.qpath, "--run", "lim")
        finally:
            os.environ.pop("FAKE_LIMIT_AFTER")
        self.assertEqual(code, agent.EXIT_USAGE_LIMIT, (out, err))
        self.assertIn("usage limit", err)
        self.assertIn("rerun the same command", err)
        done = {r[0] for r in CONN.execute("SELECT question_id FROM answers WHERE run='lim'")}
        self.assertEqual(done, {"X1"})
        bad = CONN.execute("SELECT ok, error FROM llm_runs WHERE run='lim' AND target='X2'").fetchone()
        self.assertEqual(bad["ok"], 0)
        self.assertIn("usage_limit_reached", bad["error"])
        code, out, err = run_cli("ask-all", "--questions", self.qpath, "--run", "lim")
        self.assertEqual(code, 0, (out, err))
        self.assertIn("done 2, skipped (already answered) 1, failed 0", out)
        self.assertEqual({r[0] for r in CONN.execute("SELECT question_id FROM answers WHERE run='lim'")}, {"X1", "X2", "X3"})

    def test_3_single_ask_usage_limit(self):
        os.environ["FAKE_LIMIT_AFTER"] = "0"
        try:
            code, out, err = run_cli("ask", "--team", "TS3004", "Was speech recognition in the final design?", "--run", "lim1")
        finally:
            os.environ.pop("FAKE_LIMIT_AFTER")
        self.assertEqual(code, agent.EXIT_USAGE_LIMIT)
        self.assertIn("STOPPED", err)

    def test_4_free_text_ask(self):
        code, out, err = run_cli("ask", "--team", "TS3004", "Was speech recognition in the final design?", "--run", "free")
        self.assertEqual(code, 0, (out, err))
        r = json.loads(out)
        self.assertTrue(r["question_id"].startswith("ASK-"))
        self.assertEqual(r["status"], "done")

    def test_5_brief_is_clamped(self):
        code, out, err = run_cli("brief", "TS3012", "--as-of", 3, "--run", "fake")
        self.assertEqual(code, 0, (out, err))
        self.assertIn("Brief for team TS3012 (based on meetings 1-3)", out)
        (c,) = self.calls()
        peek = c["tool_results"][0]
        self.assertTrue(peek["isError"]); self.assertIn("after the as-of cutoff", peek["text"])
        a = c["argv"]
        sp = a[a.index("--system-prompt") + 1]
        self.assertIn("TS3012a, TS3012b, TS3012c", sp)
        self.assertNotIn("{{", sp)
        self.assertEqual(CONN.execute("SELECT as_of_ord FROM briefs WHERE run='fake' AND team='TS3012'").fetchone()[0], 3)
        code, out, err = run_cli("brief", "TS3012", "--as-of", 3, "--run", "fake")
        self.assertIn("skipped", out)

    def test_6_extract_loop_feeds_rejections_back(self):
        code, out, err = run_cli("extract", "IS1005", "--model", "haiku")
        self.assertEqual(code, 0, (out, err))
        n_parts = sum(len(m["parts"]) for m in tools.extract_plan(CONN, "IS1005")["meetings"])
        self.assertIn(f"parts done {n_parts}", out)
        calls = self.calls()
        self.assertEqual(len(calls), 2 * n_parts)  # try 1 has a bad item, try 2 is clean
        self.assertIn("=== SUBMIT REPORT ===", calls[1]["stdin"])
        self.assertIn("speaker 'Q'", calls[1]["stdin"])
        a = calls[0]["argv"]
        self.assertNotIn("--mcp-config", a)
        self.assertEqual(a[a.index("--tools") + 1], "")
        self.assertEqual(a[a.index("--model") + 1], "haiku")
        rows = CONN.execute("SELECT source, count(*) FROM events WHERE team='IS1005' GROUP BY source").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("claude-haiku", n_parts)])
        self.assertIsNone(tools.extract_plan(CONN, "IS1005")["next"])
        code, out, err = run_cli("extract", "IS1005", "--model", "haiku")   # resumable: nothing left to do
        self.assertIn(f"skipped (already extracted) {n_parts}", out)
        self.assertEqual(len(self.calls()), 2 * n_parts)

    def test_7_usage_report(self):
        code, out, err = run_cli("usage")
        self.assertEqual(code, 0)
        self.assertIn("per 1,000 meetings", out)
        self.assertIn("answer", out)
        code, out, err = run_cli("usage", "--run", "nothing-here")
        self.assertIn("no LLM calls logged", out)

    def test_8_bare_with_api_key(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-not-a-key"
        try:
            cmd = agent.build_command("haiku", "sys", 2)
        finally:
            os.environ.pop("ANTHROPIC_API_KEY")
        self.assertEqual(cmd[1:3], ["-p", "--bare"])

    def test_9_parse_helpers(self):
        self.assertTrue(agent.is_usage_limit({"is_error": True, "api_error": "usage_limit_reached"}))
        self.assertFalse(agent.is_usage_limit({"is_error": False, "result": "fine"}))
        self.assertEqual(agent.parse_json_reply('Here:\n{"a": 1}\nthanks')[0], {"a": 1})
        self.assertIsNotNone(agent.parse_json_reply("no json")[1])
        self.assertEqual(agent.parse_result('{"type":"system"}\n{"type":"result","is_error":false}')["type"], "result")


if __name__ == "__main__":
    unittest.main()
