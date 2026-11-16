"""Event-ledger tests on the real AMI data (temp database; memory.db untouched).

    python3 -m unittest discover -s tests -v

Events are hand-written here (no LLM) and go through the CLI path
(``cli.main([...])``), exactly as an LLM worker would submit them.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from meetmem import cite, cli, events as ev, store, tools  # noqa: E402
from meetmem.check import check_submission  # noqa: E402

TRANSCRIPTS = os.path.join(ROOT, "data", "transcripts")
TEAMS = ("TS3004", "IS1008", "TS3012")
TMP = DB = CONN = None


def setUpModule():
    global TMP, DB, CONN
    TMP = tempfile.mkdtemp(prefix="meetmem_events_")
    DB = os.path.join(TMP, "t.db")
    CONN = store.connect(DB)
    for f in sorted(os.listdir(TRANSCRIPTS)):
        if f.startswith(TEAMS):
            store.ingest_file(CONN, os.path.join(TRANSCRIPTS, f))


def tearDownModule():
    CONN.close()
    shutil.rmtree(TMP, ignore_errors=True)


def dump(obj, path):
    with open(path, "w") as fh:
        json.dump(obj, fh)


def run(*argv):
    """Run the CLI on the temp DB; return (exit code, stdout)."""
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        code = cli.main(["--db", DB, *map(str, argv)])
    return code, buf.getvalue()


def submit(meeting, data, part=1, source="test"):
    path = os.path.join(TMP, f"{meeting}_{part}.json")
    with open(path, "w") as fh:
        json.dump(data, fh)
    code, out = run("submit-events", meeting, path, "--part", part, "--source", source)
    return code, json.loads(out)


def turn_of(mid, needle):
    r = CONN.execute("SELECT turn_idx FROM turns WHERE meeting_id=? AND text LIKE ? ORDER BY turn_idx",
                     (mid, f"%{needle}%")).fetchone()
    assert r, (mid, needle)
    return r[0]


def ev_(topic, kind, at, summary, evidence=None, contains=None, **kw):
    d = {"topic": topic, "kind": kind, "summary": summary, "at_turn": at,
         "evidence": {"turn_idx": evidence if evidence is not None else at, "contains": contains}}
    d.update(kw)
    return d


SR = "speech-recognition"


class TestSpeechRecognitionTS3004(unittest.TestCase):
    """TS3004: skipped b 08:41, reopened b 31:19, 'Okay, no speech recognition.' b 33:24;
    skipped c 38:31, reinstated c 39:13; kept in d ('speech function')."""

    @classmethod
    def setUpClass(cls):
        cls.t = {
            "b_skip": turn_of("TS3004b", "we can skip speech recognition"),
            "b_reopen": turn_of("TS3004b", "something to read about um uh speech"),
            "b_hard": turn_of("TS3004b", "very hard for uh speech recognition"),
            "b_no": turn_of("TS3004b", "Okay, no speech recognition."),
            "c_skip": turn_of("TS3004c", "we just skip the the the voca"),
            "c_back": turn_of("TS3004c", "Can we just put it speech recognition in it as well"),
            "d_keep": turn_of("TS3004d", "Only one button to say it's on or off"),
            "d_func": turn_of("TS3004d", "The speech function is new."),
        }
        t = cls.t
        assert (t["b_skip"], t["b_no"], t["c_skip"], t["c_back"]) == (67, 419, 553, 567), t
        cls.topic = {"slug": SR, "label": "Speech recognition",
                     "aliases": ["voice recognition", "speech function", "sample speaker"]}
        # meeting b: t67 in part 1 (t0-t196); the rest in part 2 (t191-t473)
        cls.rb1 = submit("TS3004b", {"topics": [cls.topic], "events": [
            ev_(SR, "rejected", t["b_skip"], "D proposes skipping speech recognition: not reachable for 25 EUR",
                contains="skip speech recognition", speaker="D", value="25 EUR")]}, part=1)
        cls.bad_evidence = ev_(SR, "rejected", t["b_no"], "D concludes: no speech recognition", evidence=t["b_no"])
        cls.rb2 = submit("TS3004b", {"events": [
            ev_(SR, "proposed", t["b_reopen"], "D reopens speech recognition, asks whether it is reachable",
                contains="speech uh recognition"),
            cls.bad_evidence,                                                      # rejected: 4-word evidence
            ev_(SR, "rejected", t["b_no"], "D concludes: no speech recognition", evidence=t["b_hard"],
                contains="very hard for uh speech recognition"),                  # accepted: same at_turn, longer evidence
        ]}, part=2)
        cls.rc = submit("TS3004c", {"events": [
            ev_(SR, "rejected", t["c_skip"], "A summarises: skip speech recognition", contains="speech recognition"),
            ev_(SR, "reinstated", t["c_back"], "A asks to put speech recognition in as well; agreed"),
        ]}, part=2)
        cls.rd = submit("TS3004d", {"events": [
            ev_(SR, "decided", t["d_keep"], "Speech recognition kept, one on/off button"),
        ]}, part=1)
        cls.rd2 = submit("TS3004d", {"events": [
            ev_(SR, "info", t["d_func"], "D lists the speech function as a new feature", evidence=t["d_func"]),
        ]}, part=2)

    def test_quotes_are_code_copied_and_verify(self):
        rows = CONN.execute("SELECT * FROM events WHERE team='TS3004'").fetchall()
        self.assertEqual(len(rows), 7)
        for r in rows:
            q = r["evidence_quote"]
            self.assertGreaterEqual(len(q.split()), 6)
            self.assertLessEqual(len(q.split()), ev.QUOTE_MAX)
            ok, idx, reason = cite.verify_quote(r["meeting_id"], q, conn=CONN)
            self.assertTrue(ok, reason)
            self.assertEqual(idx, r["evidence_turn"])
            text = CONN.execute("SELECT text FROM turns WHERE meeting_id=? AND turn_idx=?",
                                (r["meeting_id"], r["evidence_turn"])).fetchone()[0]
            self.assertIn(q, " ".join(text.split()))
        q67 = CONN.execute("SELECT evidence_quote FROM events WHERE meeting_id='TS3004b' AND at_turn=67").fetchone()[0]
        self.assertIn("skip speech recognition", q67)

    def test_short_evidence_rejected_but_at_turn_ok(self):
        code, rep = self.rb2
        self.assertEqual(code, 0)
        self.assertEqual(rep["accepted"], 2)
        self.assertEqual(len(rep["rejected"]), 1)
        rj = rep["rejected"][0]
        self.assertEqual(rj["index"], 1)
        self.assertIn("only 4 words", rj["reasons"][0])
        self.assertTrue(any(f"evidence.turn_idx {self.t['b_hard']}" in s for s in rj["suggestions"]), rj)
        row = CONN.execute("SELECT * FROM events WHERE meeting_id='TS3004b' AND at_turn=?", (self.t["b_no"],)).fetchone()
        self.assertEqual((row["kind"], row["evidence_turn"]), ("rejected", self.t["b_hard"]))

    def test_status_as_of(self):
        st = {k: ev.topic_statuses(CONN, "TS3004", as_of_ord=k)[SR] for k in (2, 3, 4)}
        self.assertEqual((st[2]["status"], st[2]["state"]), ("rejected", "out"))
        self.assertEqual((st[3]["status"], st[3]["state"]), ("reinstated", "in"))
        self.assertEqual((st[4]["status"], st[4]["state"]), ("decided", "in"))
        self.assertNotIn(SR, ev.topic_statuses(CONN, "TS3004", as_of_ord=1))
        # sequence b:rej, b:prop, b:rej, c:rej, c:rei, d:dec -> one reversal (out->in) in c, one reopening in b
        self.assertEqual([s["kind"] for s in st[4]["sequence"]],
                         ["rejected", "proposed", "rejected", "rejected", "reinstated", "decided"])
        self.assertEqual(st[4]["reversals"], 1)
        self.assertEqual(st[4]["last_reversal_meeting"], "TS3004c")
        self.assertEqual(st[4]["reopenings"], 1)
        self.assertEqual(st[2]["reversals"], 0)

    def test_cli_readers(self):
        code, out = run("timeline", "--team", "TS3004", "--topic", SR, "--as-of", 2)
        self.assertEqual(code, 0)
        self.assertIn("status as of meeting 2: rejected [out]", out)
        self.assertNotIn("TS3004c", out)
        code, out = run("timeline", "--team", "TS3004", "--topic", SR)
        self.assertIn("reversals: 1 (last in TS3004c)", out)
        self.assertIn("-> evidence TS3004c t567", out)
        self.assertIn("D (Jens", out)  # speaker table label from regex self-intro
        lines = [l for l in out.splitlines() if l.startswith("  #")]
        keys = [(l.split()[1], l.split()[2]) for l in lines]
        self.assertEqual(keys, sorted(keys))  # chronological
        code, out = run("timeline", "--team", "TS3004", "--cap", 2)
        self.assertIn("5 earlier event(s) omitted", out)
        code, out = run("topics", "--team", "TS3004", "--as-of", 3)
        self.assertIn("speech-recognition: Speech recognition", out)
        self.assertIn("reinstated [in]", out)
        code, out = run("facts", "voice recognition", "--team", "TS3004", "--as-of", 2)
        self.assertIn("TS3004b", out)
        self.assertNotIn("TS3004c", out)
        code, out = run("facts", "25", "--team", "TS3004")
        self.assertIn("value 25 EUR", out)
        code, out = run("turns", "TS3004c", 560, 570, "--team", "TS3004", "--as-of", 2)
        self.assertEqual(code, 2)
        self.assertIn("after the as-of cutoff", out)
        code, out = run("turns", "TS3004c", 566, 568, "--as-of", 3)
        self.assertIn("[t567 A 00:39:13]", out)
        code, out = run("search", "speech recognition", "--team", "TS3004", "--as-of", 2)
        self.assertNotIn("TS3004c", out)
        self.assertNotIn("TS3004d", out)

    def test_extract_plan_and_prompt(self):
        code, out = run("extract-plan", "TS3004")
        plan = json.loads(out)
        b = [m for m in plan["meetings"] if m["meeting_id"] == "TS3004b"][0]
        self.assertEqual(b["n_parts"], 2)
        self.assertIsNotNone(b["parts"][0]["extracted"])
        self.assertEqual(plan["next"], {"meeting_id": "TS3004a", "part": 1})
        code, out = run("extract-prompt", "TS3004c", "--part", 2)
        self.assertIn("speech-recognition: Speech recognition (aka sample speaker, speech function, voice recognition)", out)
        self.assertIn("rejected [out]", out)           # status as of before TS3004c
        self.assertIn("[t567 A 00:39:13] Can we just put it speech recognition in it as well, okay?", out)
        self.assertNotIn("[t0 ", out)                   # part 2 only
        self.assertIn('"events":[', out)


class TestValidation(unittest.TestCase):
    def test_mixed_file(self):
        mid = "TS3004c"  # part 2 = t265-t605
        ts = [r[0] for r in CONN.execute("SELECT turn_idx FROM turns WHERE meeting_id=? AND turn_idx BETWEEN 300 AND 400 "
                                         "AND n_words>=6 ORDER BY turn_idx LIMIT 5", (mid,))]
        good = ev_("lcd-screen", "proposed", ts[0], "B wants a more modern look", speaker="B")
        data = {"topics": [{"slug": "lcd-screen", "label": "LCD screen", "aliases": ["display"]}], "events": [
            good,
            ev_("lcd-screen", "decided", 50, "outside the part"),
            ev_("lcd-screen", "decision", ts[1], "bad kind"),
            ev_("lcd-screen", "action_reported", ts[2], "resolves nothing", resolves=999999),
            ev_("LCD Screen", "proposed", ts[3], "bad slug"),
            ev_("lcd-screen", "objection", ts[4], "bad speaker", speaker="Q"),
        ]}
        code, rep = submit(mid, data, part=2, source="test-mixed")
        self.assertEqual(code, 0)
        self.assertEqual(rep["accepted"], 1)
        reasons = {r["index"]: " ".join(r["reasons"]) for r in rep["rejected"]}
        self.assertIn("outside this part", reasons[1])
        self.assertIn("kind 'decision' is not one of", reasons[2])
        self.assertIn("use kind 'decided'", " ".join(rep["rejected"][1]["suggestions"]))
        self.assertIn("no such event id", reasons[3])
        self.assertIn("not a lowercase-hyphen slug", reasons[4])
        self.assertIn("speaker 'Q'", reasons[5])
        self.assertEqual(CONN.execute("SELECT count(*) FROM events WHERE meeting_id=? AND topic_slug='lcd-screen'",
                                      (mid,)).fetchone()[0], 1)
        run_row = CONN.execute("SELECT * FROM extraction_runs WHERE meeting_id=? AND part=2", (mid,)).fetchone()
        self.assertEqual((run_row["n_submitted"], run_row["n_accepted"], run_row["n_rejected"]), (6, 1, 5))
        # resubmitting a part REPLACES it: TS3004c part 2 now holds only this file's event,
        # so restore the speech-recognition events afterwards for the other test class
        TestSpeechRecognitionTS3004.setUpClass()

    def test_resubmit_replaces_and_keeps_ids(self):
        mid = "TS3012a"
        rows = CONN.execute("SELECT turn_idx FROM turns WHERE meeting_id=? AND n_words>=8 ORDER BY turn_idx LIMIT 2",
                            (mid,)).fetchall()
        e1 = ev_("kickoff", "info", rows[0][0], "first")
        e2 = ev_("kickoff", "info", rows[1][0], "second")
        _, r1 = submit(mid, {"events": [e1, e2]})
        _, r2 = submit(mid, {"events": [e1]})
        self.assertEqual(r2["accepted"], 1)
        self.assertEqual(r2["stored_event_ids"][0], r1["stored_event_ids"][0])  # stable id
        self.assertEqual(CONN.execute("SELECT count(*) FROM events WHERE meeting_id=?", (mid,)).fetchone()[0], 1)
        self.assertEqual(CONN.execute("SELECT count(*) FROM events_fts WHERE rowid=?",
                                      (r1["stored_event_ids"][1],)).fetchone()[0], 0)
        self.assertTrue(any("not listed in 'topics'" in w for w in r1["warnings"]))

    def test_overlap_duplicate_guarded(self):
        mid = "TS3004d"
        rows = CONN.execute("SELECT turn_idx FROM turns WHERE meeting_id=? AND turn_idx BETWEEN 296 AND 301 AND n_words>=6",
                            (mid,)).fetchall()
        t = rows[0][0]
        e = ev_("overlap-test", "info", t, "something in the overlap")
        _, p1 = submit(mid, {"events": [e]}, part=1)
        _, p2 = submit(mid, {"events": [e, e]}, part=2)
        self.assertEqual(p2["accepted"], 0)
        self.assertTrue(all("duplicate" in " ".join(r["reasons"]) for r in p2["rejected"]))
        # restore the speech-recognition events of TS3004d
        TestSpeechRecognitionTS3004.setUpClass()

    def test_bad_json_file(self):
        path = os.path.join(TMP, "broken.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        code, out = run("submit-events", "TS3004a", path, "--source", "x")
        self.assertEqual(code, 2)
        self.assertIn("cannot read JSON", out)
        code, out = run("submit-events", "TS3004a", path, "--part", 5, "--source", "x")
        self.assertEqual(code, 2)

    def test_evidence_window(self):
        q, w = ev.resolve_evidence(CONN, "TS3004b", 63, "remote control")
        self.assertTrue(6 <= len(q.split()) <= 30)
        self.assertIn("remote control", q.lower())
        self.assertEqual(w, [])
        q, w = ev.resolve_evidence(CONN, "TS3004b", 63, "not in this turn at all")
        self.assertEqual(len(q.split()), 30)
        self.assertTrue(w)
        with self.assertRaises(cite.QuoteError) as cm:
            ev.resolve_evidence(CONN, "TS3004b", 419)
        self.assertTrue(cm.exception.suggestions)


class TestActionsIS1008(unittest.TestCase):
    def test_assigned_and_reported(self):
        t_ask = turn_of("IS1008c", "Do you wanna take an action item")
        t_until = turn_of("IS1008c", "Ed find outs")
        _, rc = submit("IS1008c", {"topics": [{"slug": "display", "label": "Display / LCD", "aliases": []}], "events": [
            ev_("display", "action_assigned", t_ask, "Ed (D) to find out what a display costs", evidence=t_until,
                contains="until uh th Ed find outs", speaker="C", owner="Ed"),
            ev_("display", "deferred", t_until, "Keep the simple design until Ed finds the display cost",
                contains="stick with the the simple design", speaker="A"),
        ]})
        self.assertEqual(rc["accepted"], 2, rc)
        self.assertTrue(any("mapped to speaker D" in w for w in rc["warnings"]))
        aid = rc["stored_event_ids"][0]
        code, out = run("actions", "--team", "IS1008", "--open")
        self.assertIn(f"#{aid}", out)
        self.assertIn("owner D (Ed", out)
        self.assertIn("OPEN", out)
        # the prompt for meeting d lists it as an open item
        code, prompt = run("extract-prompt", "IS1008d")
        self.assertIn(f"#{aid} IS1008c", prompt)
        self.assertIn("D: name Ed", prompt)
        t_rep = turn_of("IS1008d", "twelve and a half")
        _, rd = submit("IS1008d", {"speakers": [{"speaker": "D", "role": "marketing expert", "turn_idx": t_rep}],
                                   "events": [
            ev_("display", "action_reported", t_rep, "D reports the cost is now about 12.5 EUR",
                contains="twelve and a half", speaker="D", value="12.5 EUR", resolves=aid)]})
        self.assertEqual(rd["accepted"], 1, rd)
        acts = tools.actions(CONN, "IS1008")
        a = [x for x in acts if x["event_id"] == aid][0]
        self.assertEqual(a["status"], "closed")
        self.assertEqual(a["owner"], "D")
        self.assertTrue(a["owner_label"].startswith("D (Ed"))
        self.assertEqual(a["reported"][0]["meeting_id"], "IS1008d")
        code, out = run("actions", "--team", "IS1008", "--open")
        self.assertNotIn(f"#{aid}", out)
        code, out = run("actions", "--team", "IS1008", "--as-of", 3)  # report-back not visible yet
        self.assertIn("OPEN", out)
        code, out = run("facts", "12.5", "--team", "IS1008")
        self.assertIn("value 12.5 EUR", out)
        st = ev.speaker_table(CONN, "IS1008")
        self.assertEqual((st["D"]["name"], st["D"]["role"]), ("Ed", "marketing expert"))
        # resolves must point to an EARLIER event
        _, bad = submit("IS1008a", {"events": [ev_("display", "info", 3, "x", resolves=aid)]})
        self.assertIn("not earlier", " ".join(bad["rejected"][0]["reasons"]))

    def test_speaker_conflicts_flagged(self):
        _, r = submit("IS1008b", {"speakers": [{"speaker": "A", "name": "Sridhar", "turn_idx": 2}]})
        st = ev.speaker_table(CONN, "IS1008")
        self.assertTrue(any("name differs" in c for c in st["A"]["conflicts"]), st["A"])
        submit("IS1008b", {})  # clean up


class TestAnswersBriefExport(unittest.TestCase):
    def test_brief_cutoff_and_answer_export(self):
        tb = CONN.execute("SELECT turn_idx FROM turns WHERE meeting_id='TS3012b' AND n_words>=10 ORDER BY turn_idx LIMIT 1").fetchone()[0]
        td = CONN.execute("SELECT turn_idx FROM turns WHERE meeting_id='TS3012d' AND n_words>=10 ORDER BY turn_idx LIMIT 1").fetchone()[0]
        brief = {"sections": [{"heading": "Decided", "points": [
            {"text": "A point", "citations": [{"meeting_id": "TS3012b", "turn_idx": tb}]},
            {"text": "Leaks the future", "citations": [{"meeting_id": "TS3012d", "turn_idx": td}]}]}]}
        path = os.path.join(TMP, "brief.json")
        dump(brief, path)
        code, out = run("brief-submit", "--run", "r1", "TS3012", "--as-of", 3, path)
        self.assertEqual(code, 1)
        self.assertIn("TS3012d is meeting 4", out)
        brief["sections"][0]["points"].pop()
        dump(brief, path)
        code, out = run("brief-submit", "--run", "r1", "TS3012", "--as-of", 3, path)
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r'- A point \[TS3012b: ".{20,}"\]')

        ans = {"answer": "They dropped it, then reinstated it.", "citations": [
            {"meeting_id": "TS3004b", "turn_idx": 67, "contains": "skip speech recognition"},
            {"meeting_id": "TS3004c", "turn_idx": 567}]}
        apath = os.path.join(TMP, "ans.json")
        dump(ans, apath)
        code, out = run("answer-submit", "--run", "r1", "X1", apath, "--source", "test", "--team", "TS3004")
        rep = json.loads(out)
        self.assertEqual(code, 0, out)
        self.assertEqual(rep["citations"][1]["quote"], "Can we just put it speech recognition in it as well, okay?")
        # wrong team / too many citations / short turn are refused
        bad = {"answer": "x", "citations": [{"meeting_id": "IS1008c", "turn_idx": 144}]}
        dump(bad, apath)
        code, out = run("answer-submit", "--run", "r1", "X2", apath, "--source", "t", "--team", "TS3004")
        self.assertEqual(code, 1)
        self.assertIn("belongs to IS1008", out)
        bad = {"answer": "x", "citations": [{"meeting_id": "TS3004b", "turn_idx": 419}] * 4}
        dump(bad, apath)
        code, out = run("answer-submit", "--run", "r1", "X2", apath, "--source", "t", "--team", "TS3004")
        self.assertIn("need 1-3 citations", out)
        code, out = run("answer-submit", "--run", "r1", "Q99", apath, "--source", "t")
        self.assertEqual(code, 2)

        # export with a temp copy of the checker's expectations restricted to the synthetic run
        qpath = os.path.join(TMP, "q.json")
        qs = {"questions": [{"question_id": "X1", "team": "TS3004", "question": "synthetic"}],
              "brief_task": {"team": "TS3012"}}
        dump(qs, qpath)
        code, out = run("export-answers", "--run", "r1", "--questions", qpath)
        sub = json.loads(out)
        self.assertEqual(set(sub), {"answers", "brief"})
        self.assertEqual(set(sub["answers"][0]), {"question_id", "answer", "citations"})
        self.assertEqual(set(sub["answers"][0]["citations"][0]), {"meeting_id", "quote"})
        rep = check_submission(sub, qs, CONN)
        self.assertEqual(rep["failures"], 0, rep)
        self.assertTrue(rep["brief_ok"])
        # also through the real checker CLI
        spath = os.path.join(TMP, "sub.json")
        dump(sub, spath)
        from meetmem import check
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check.main([spath, "--questions", qpath, "--db", DB]), 0)


if __name__ == "__main__":
    unittest.main()
