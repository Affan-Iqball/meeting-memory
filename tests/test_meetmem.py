"""Tests on the real AMI data. Run from the project root:

    python3 -m unittest discover -s tests -v

The whole corpus is ingested once into a temporary database (memory.db is
not touched).
"""
import glob
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from meetmem import store, search, cite, speakers, check  # noqa: E402
from meetmem.parse import parse_file  # noqa: E402
from meetmem.normalize import normalize, tokens  # noqa: E402

TRANSCRIPTS = os.path.join(ROOT, "data", "transcripts")
QUESTIONS = os.path.join(ROOT, "data", "questions.json")
FILES = sorted(glob.glob(os.path.join(TRANSCRIPTS, "*.txt")))

TMP = None
CONN = None


def setUpModule():
    global TMP, CONN
    TMP = tempfile.mkdtemp(prefix="meetmem_test_")
    CONN = store.connect(os.path.join(TMP, "test.db"))
    for f in FILES:
        store.ingest_file(CONN, f)


def tearDownModule():
    CONN.close()
    shutil.rmtree(TMP, ignore_errors=True)


def turn_index_of(meeting_id, needle):
    r = CONN.execute("SELECT turn_idx FROM turns WHERE meeting_id=? AND text LIKE ? ORDER BY turn_idx",
                     (meeting_id, f"%{needle}%")).fetchone()
    return r[0] if r else None


class TestParse(unittest.TestCase):
    def test_all_files_parse(self):
        self.assertEqual(len(FILES), 138)
        total = 0
        for f in FILES:
            pm = parse_file(f)
            self.assertEqual(pm.problems, [], f)
            self.assertTrue(pm.turns, f)
            self.assertEqual(pm.meeting_id, os.path.basename(f)[:-4])
            self.assertEqual([t.turn_idx for t in pm.turns], list(range(len(pm.turns))))
            total += pm.n_words
        self.assertTrue(670_000 <= total <= 683_000, total)

    def test_raw_text_preserved(self):
        pm = parse_file(os.path.join(TRANSCRIPTS, "TS3004b.txt"))
        t0 = pm.turns[0]
        self.assertEqual(t0.speaker, "A")
        self.assertEqual(t0.time_str, "00:00:34")
        self.assertEqual(t0.start_sec, 34)
        self.assertEqual(t0.text, "I just forgot their name, so uh you're i sorry, I just forgot them all. So I have to write it down.")

    def test_wrapped_lines_and_bad_header(self):
        from meetmem.parse import parse_text
        pm = parse_text("Meeting: XX1234a\nTeam: XX1234\nbogus header line without colon\n\n"
                        "00:00:01 Speaker A\nhello   there\nsecond line\n\n00:00:02 Speaker B\nok\n")
        self.assertEqual([t.text for t in pm.turns], ["hello   there second line", "ok"])
        self.assertTrue(any("unrecognised header" in p for p in pm.problems))


class TestStore(unittest.TestCase):
    def test_counts(self):
        q = lambda s: CONN.execute(s).fetchone()[0]
        self.assertEqual(q("SELECT count(*) FROM meetings"), 138)
        self.assertEqual(q("SELECT count(DISTINCT team) FROM meetings"), 35)
        self.assertTrue(670_000 <= q("SELECT sum(n_words) FROM meetings") <= 683_000)

    def test_ords_and_last_meeting(self):
        ms = store.team_meetings(CONN, "IS1002")
        self.assertEqual([m["letter"] for m in ms], ["b", "c", "d"])
        self.assertEqual([m["ord"] for m in ms], [1, 2, 3])
        self.assertEqual(store.last_meeting(CONN, "IS1005")["meeting_id"], "IS1005c")
        self.assertEqual(store.last_meeting(CONN, "TS3012")["meeting_id"], "TS3012d")
        self.assertEqual(search.team_meetings("TS3012", conn=CONN)[0]["ord"], 1)
        # passages carry the same ord as their meeting
        bad = CONN.execute("SELECT count(*) FROM passages p JOIN meetings m USING(meeting_id) WHERE p.ord != m.ord").fetchone()[0]
        self.assertEqual(bad, 0)

    def test_passages_shape(self):
        rows = CONN.execute("SELECT meeting_id, first_turn, last_turn, n_words, text_display FROM passages "
                            "ORDER BY meeting_id, first_turn").fetchall()
        by_m = {}
        for r in rows:
            by_m.setdefault(r[0], []).append(r)
        for mid, ps in by_m.items():
            n_turns = CONN.execute("SELECT n_turns FROM meetings WHERE meeting_id=?", (mid,)).fetchone()[0]
            self.assertEqual(ps[0][1], 0)
            self.assertEqual(ps[-1][2], n_turns - 1)
            for a, b in zip(ps, ps[1:]):
                # 2-turn overlap (1 when the previous passage is only 2 turns, to guarantee progress)
                self.assertEqual(b[1], max(a[2] - 1, a[1] + 1), (mid, a[1:3], b[1:3]))
        sizes = sorted(r[3] for r in rows)
        median = sizes[len(sizes) // 2]
        self.assertTrue(250 <= median <= 350, median)
        self.assertRegex(rows[0][4].split("\n")[0], r"^\[t0 [A-D] \d\d:\d\d:\d\d\] ")

    def test_reingest_idempotent(self):
        def counts():
            return tuple(CONN.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                         for t in ("meetings", "turns", "passages", "speakers"))
        before = counts()
        hits_before = [h["passage_id"] for h in search.search_passages("remote", meeting_id="TS3004b", limit=50, conn=CONN)]
        f = os.path.join(TRANSCRIPTS, "TS3004b.txt")
        store.ingest_file(CONN, f)
        store.ingest_file(CONN, f)
        self.assertEqual(counts(), before)
        CONN.execute("INSERT INTO passages_fts(passages_fts) VALUES('integrity-check')")
        hits_after = [h["passage_id"] for h in search.search_passages("remote", meeting_id="TS3004b", limit=50, conn=CONN)]
        self.assertEqual(sorted(hits_before), sorted(hits_after))
        self.assertEqual(len(hits_after), len(set(hits_after)))


class TestNormalize(unittest.TestCase):
    def test_acronyms(self):
        self.assertEqual(normalize("The L_C_D_ on the T_V_."), "the lcd on the tv")
        self.assertEqual(normalize("LCD"), "lcd")
        self.assertIn("lcd", normalize("L.C.D."))

    def test_numbers_added_not_replaced(self):
        self.assertEqual(normalize("twelve and a half euros"), "twelve and a half 12.5 euros")
        self.assertEqual(normalize("twenty five"), "twenty five 25")
        self.assertIn("1500", tokens("fifteen hundred"))
        self.assertIn("100", tokens("a hundred"))
        self.assertIn("0.8", tokens("zero point eight"))
        self.assertIn("12.5", tokens("twelve point five"))
        self.assertIn("12.5", tokens("twelve fifty"))
        self.assertEqual(tokens("12.50"), ["12.5"])
        self.assertEqual(tokens("€1,500"), ["1500"])

    def test_conservative(self):
        self.assertEqual(normalize("I want the blue one"), "i want the blue one")
        self.assertEqual(normalize("a remote with a button"), "a remote with a button")
        self.assertEqual(normalize("from a marketing point of view"), "from a marketing point of view")


class TestSearch(unittest.TestCase):
    def test_lcd_team(self):
        hits = search.search_passages("LCD", team="TS3006", conn=CONN)
        self.assertTrue(hits)
        for h in hits:
            self.assertEqual(h["meeting_id"][:6], "TS3006")
            self.assertIn("L_C_D_", h["text_display"])

    def test_digit_query_finds_words(self):
        # TS3009d: "It has to go to twelve, right? Twelve and a half."
        idx = turn_index_of("TS3009d", "Twelve and a half.")
        self.assertIsNotNone(idx)
        hits = search.search_passages("12.5", team="TS3009", limit=20, conn=CONN)
        self.assertTrue(any(h["meeting_id"] == "TS3009d" and h["first_turn"] <= idx <= h["last_turn"] for h in hits))
        for q in ("12.50", "twelve fifty", "€12.5"):
            self.assertTrue(search.search_passages(q, team="TS3009", conn=CONN), q)

    def test_as_of_ord_cutoff(self):
        for q in ("final decision price budget", "remote control design", "buttons", "twelve and a half"):
            hits = search.search_passages(q, team="TS3012", as_of_ord=3, limit=50, conn=CONN)
            self.assertTrue(hits)
            self.assertNotIn("TS3012d", {h["meeting_id"] for h in hits})
            self.assertTrue(all(h["ord"] <= 3 for h in hits))

    def test_query_safety(self):
        for q in ['"', "AND OR NOT", "NEAR(", "*", "col:umn", "a)b(", "", "   ", "-", "^start"]:
            search.search_passages(q, team="TS3004", conn=CONN)  # must not raise
        self.assertIsNone(search.build_fts_query("..."))

    def test_get_turns(self):
        rows = search.get_turns("TS3004b", 0, 4, conn=CONN)
        self.assertEqual([r["turn_idx"] for r in rows], [0, 1, 2, 3, 4])
        with self.assertRaises(ValueError):
            search.get_turns("TS3004b", 0, 500, conn=CONN)


class TestCite(unittest.TestCase):
    def test_normalize_for_match(self):
        self.assertEqual(cite.normalize_for_match("  Hello\t  WORLD,\nok "), "hello world, ok")

    def test_short_turn_rejected(self):
        self.assertIsNotNone(turn_index_of("TS3004b", "Okay, no speech recognition."))
        ok, idx, reason = cite.verify_quote("TS3004b", "Okay, no speech recognition.", conn=CONN)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("too_short"), reason)

    def test_meeting_not_found(self):
        ok, _, reason = cite.verify_quote("ZZ9999a", "one two three four five six", conn=CONN)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("meeting_not_found"))

    def test_split_turn_is1008d(self):
        first = turn_index_of("IS1008d", "two to three Euro per")
        cont = turn_index_of("IS1008d", "remote, now we're up around about twelve")
        self.assertIsNotNone(first)
        self.assertGreater(cont, first)
        ok, idx, reason = cite.verify_quote("IS1008d", "approximately two to three Euro per remote, now", conn=CONN)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("spans_turns"), reason)
        self.assertEqual(idx, first)
        ok, idx, reason = cite.verify_quote("IS1008d", "so if we add approximately two to three Euro per", conn=CONN)
        self.assertTrue(ok, reason)
        self.assertEqual(idx, first)
        ok, idx, reason = cite.verify_quote("IS1008d", "SO if   we ADD approximately\n two to three euro PER ", conn=CONN)
        self.assertTrue(ok, reason)

    def test_spans_adjacent_turns(self):
        rows = CONN.execute("SELECT turn_idx, text, n_words FROM turns WHERE meeting_id='TS3004b' ORDER BY turn_idx").fetchall()
        i = next(k for k in range(len(rows) - 1) if rows[k][2] >= 4 and rows[k + 1][2] >= 3)
        q = " ".join(rows[i][1].split()[-4:] + rows[i + 1][1].split()[:3])
        ok, idx, reason = cite.verify_quote("TS3004b", q, conn=CONN)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("spans_turns"), reason)

    def test_altered_quotes_rejected(self):
        r = CONN.execute("SELECT meeting_id, turn_idx, text FROM turns WHERE text LIKE '%the L_C_D_ %' AND n_words >= 12 LIMIT 1").fetchone()
        words = r["text"].split()
        k = words.index("L_C_D_")
        span = words[max(0, k - 4):k + 4]
        exact = " ".join(span)
        self.assertTrue(cite.verify_quote(r["meeting_id"], exact, conn=CONN)[0])
        ok, idx, reason = cite.verify_quote(r["meeting_id"], exact.replace("L_C_D_", "LCD"), conn=CONN)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("near_miss"), reason)
        self.assertEqual(idx, r["turn_idx"])
        # comma removed
        r = CONN.execute("SELECT meeting_id, turn_idx, text FROM turns WHERE meeting_id='IS1008d' AND text LIKE '%up there, okay, so if%'").fetchone()
        q = "We don't have the price up there, okay, so if we add"
        self.assertTrue(cite.verify_quote("IS1008d", q, conn=CONN)[0])
        ok, idx, reason = cite.verify_quote("IS1008d", q.replace(", okay,", " okay"), conn=CONN)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("near_miss"), reason)
        self.assertEqual(idx, r["turn_idx"])
        ok, idx, reason = cite.verify_quote("IS1008d", "purple elephants dance quietly under the moon tonight", conn=CONN)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("not_found"), reason)

    def test_quote_from_span_property(self):
        rng = random.Random(1234)
        rows = CONN.execute("SELECT meeting_id, turn_idx, n_words FROM turns WHERE n_words >= 6").fetchall()
        sample = rng.sample(rows, 400)
        for r in sample:
            n = r["n_words"]
            if rng.random() < 0.3:
                s, e = None, None
            else:
                s = rng.randrange(0, n - 5)
                e = rng.randrange(s + 6, n + 1)
            q = cite.quote_from_span(r["meeting_id"], r["turn_idx"], s, e, conn=CONN)
            ok, idx, reason = cite.verify_quote(r["meeting_id"], q, conn=CONN)
            self.assertTrue(ok, (r["meeting_id"], r["turn_idx"], s, e, q, reason))
            self.assertLessEqual(len(q.split()), cite.MAX_QUOTE_WORDS)

    def test_quote_from_span_errors(self):
        idx = turn_index_of("TS3004b", "Okay, no speech recognition.")
        with self.assertRaises(cite.QuoteError) as cm:
            cite.quote_from_span("TS3004b", idx, conn=CONN)
        self.assertIn("Nearby turns by the same speaker", str(cm.exception))
        self.assertTrue(cm.exception.suggestions)
        self.assertTrue(all(s["n_words"] >= 6 for s in cm.exception.suggestions))
        with self.assertRaises(cite.QuoteError):
            cite.quote_from_span("TS3004b", 99999, conn=CONN)
        with self.assertRaises(cite.QuoteError):
            cite.quote_from_span("TS3004b", 0, 5, 3, conn=CONN)

    def test_quote_cap(self):
        r = CONN.execute("SELECT meeting_id, turn_idx FROM turns WHERE n_words > 100 LIMIT 1").fetchone()
        q = cite.quote_from_span(r[0], r[1], conn=CONN)
        self.assertEqual(len(q.split()), cite.MAX_QUOTE_WORDS)
        self.assertTrue(cite.verify_quote(r[0], q, conn=CONN)[0])


class TestCheck(unittest.TestCase):
    def test_checker(self):
        with open(QUESTIONS) as fh:
            questions = json.load(fh)
        answers = []
        for q in questions["questions"]:
            mid = store.team_meetings(CONN, q["team"])[0]["meeting_id"]
            r = CONN.execute("SELECT turn_idx FROM turns WHERE meeting_id=? AND n_words>=8 LIMIT 1", (mid,)).fetchone()
            answers.append({"question_id": q["question_id"], "answer": "Something.",
                            "citations": [{"meeting_id": mid, "quote": cite.quote_from_span(mid, r[0], 0, 7, conn=CONN)}]})
        sub = {"answers": answers, "brief": "Brief."}
        rep = check.check_submission(sub, questions, CONN)
        self.assertEqual(rep["failures"], 0, rep)
        # break things: wrong team, paraphrase, too many citations, missing question
        sub["answers"][0]["citations"][0]["meeting_id"] = "IS1008d"
        sub["answers"][1]["citations"][0]["quote"] = "this is a paraphrase that is not there"
        sub["answers"][2]["citations"] = sub["answers"][2]["citations"] * 4
        del sub["answers"][3]
        rep = check.check_submission(sub, questions, CONN)
        self.assertEqual(rep["failures"], 4)
        fails = {r["question_id"]: r["problems"] for r in rep["results"] if not r["ok"]}
        self.assertIn("wrong_team", " ".join(fails["Q01"]))
        self.assertIn("missing answer", fails["Q04"])
        path = os.path.join(TMP, "answers.json")
        with open(path, "w") as fh:
            json.dump(sub, fh)
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = check.main([path, "--questions", QUESTIONS, "--db", os.path.join(TMP, "test.db")])
        self.assertNotEqual(code, 0)
        self.assertIn("summary:", buf.getvalue())


class TestSpeakers(unittest.TestCase):
    def test_detect_self_intro(self):
        d = speakers.detect([
            {"turn_idx": 0, "speaker": "A", "text": "My name is Ed and I'm the Project Manager."},
            {"turn_idx": 1, "speaker": "B", "text": "Okay. I'm Sarah, the User Interface Designer."},
            {"turn_idx": 2, "speaker": "C", "text": "I don't know what I'm marketing."},
            {"turn_idx": 3, "speaker": "D", "text": "I'm not sure the industrial designer agrees."},
        ])
        self.assertEqual(d["A"]["name"], "Ed")
        self.assertEqual(d["A"]["role"], "project manager")
        self.assertEqual(d["B"]["name"], "Sarah")
        self.assertEqual(d["B"]["role"], "user interface designer")
        self.assertNotIn("C", d)
        self.assertNotIn("D", d)

    def test_rows_and_report(self):
        rows = speakers.speaker_rows(CONN, meeting_id="ES2011a")
        self.assertEqual({r["speaker"]: r["role"] for r in rows},
                         {"A": "project manager", "B": "industrial designer",
                          "C": "user interface designer", "D": "marketing expert"})
        rep = speakers.role_consistency(CONN)
        self.assertEqual(rep["teams_total"], 35)
        self.assertGreater(rep["teams_with_any_role"], 20)


if __name__ == "__main__":
    unittest.main()
