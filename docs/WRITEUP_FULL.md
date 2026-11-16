# Meeting memory: full write-up (appendix to WRITEUP.md)

## What it is

An agent that turns 138 AMI meeting transcripts (35 teams designing a TV remote, about 677,000 words) into a memory you can question, and that briefs a team before its next meeting. It answers each question in one to three sentences, with citations copied from the transcript by code.

## Architecture

```
transcripts ─► parse + index ─► extract events ─► ledger (SQLite) ─► agent with read tools ─► answer ─► code builds quotes ─► answers.json
               (code)            (cheap model,      events, topics,     (stronger model:        (refused and re-pointed
                                  per meeting part)  speakers, actions)  topics, timeline,        if a quote fails)
                                                                         facts, actions, turns)
```

- **Store.** One SQLite file: turns, roughly 300-word passages with an FTS5 keyword index, and the event ledger.
- **Search column.** Search runs over a cleaned column. The corpus has no digits ("twelve and a half") and writes acronyms as `L_C_D_`, so "12.5" and "LCD" would otherwise miss. Quotes always come from the raw text.
- **Events.** A cheap model reads one meeting part (about 3,500 words) and records *events* that point at turn ids. Kinds are `proposed`, `decided`, `rejected`, `reinstated`, `deferred`, `objection`, `number`, `action_assigned`, `action_reported`, `requirement` and `info`. The previous meeting's open actions and topics are in its prompt, so report-backs link to promises.
- **Knowing what is current.** For each topic, code orders the events by meeting and turn. The latest status event is the current state, and code counts reversals and records the meeting of the last one. An `as_of` cutoff is enforced in SQL, which is how the TS3012 brief is written from meetings a to c without seeing d.
- **Answering.** The agent finds the topic, reads its timeline, then re-reads the real turns around each key event and checks the team's last meeting. It submits citations as `{meeting_id, turn_idx, contains}`, never as typed quotes.
- **Scale.** Everything is per meeting and incremental. A new meeting adds its own rows; nothing is rebuilt. Search filters by team first.

## Model vs code

| Work | Who | Why |
|---|---|---|
| Parsing, ordering meetings, indexing, number and acronym normalising | Code | Fixed format; must be exact and free |
| Quotes: copying, checking 6+ words inside one turn, right team, before the cutoff | Code | A model "approves" near-misses; the grader is literal. 50.5% of turns are under six words and sentences split across turns, so a model-typed quote fails often |
| Status now, reversal counts, last-reversal meeting, open vs closed actions | Code | Pure ordering logic over events; a model would re-derive it inconsistently |
| What happened in messy speech, which topic an event belongs to, who "Ed" is | Cheap model | Needs language understanding; high volume, simple per-part task |
| Answering across meetings and writing the brief | Stronger model | Low volume and the hardest reasoning; must judge the ledger against the turns |

The ledger is an index, not the truth. Extraction makes mistakes, so the agent is told to read the turns before trusting a summary.

## Proof it works

- **Official questions:** 25 of 25 answers and the brief pass the strict checker (`python3 -m meetmem.check answers.json`). Every quote is exact and inside one turn.
- **Our test set:** 14 questions on six teams not in the official 25, written from the raw transcripts before any answering. They are in `eval/gold.json`, with notes in `eval/gold_notes.md`.
  - All 14 answers' citations are valid.
  - No answer contradicts a gold key point.
  - 51 of 56 key points are covered (91%), and 9 of 14 answers cover every key point. Two of the five missed points (G03, G04) are details the question did not ask for.
  - Grading was redone by a separate reviewer session that did not answer the questions and read the cited turns first. The answering session's own grade was 50 of 56; the two differ on one point (G08). Three questions (G06, G13, G14) were seen with their gold answers during a spot-check before answering, so they are contaminated. Excluding them, 7 of 11 answers cover every key point.
  - Only 9 of 14 answers cite every meeting the gold answer needs; five leave one out.
  - Scores per question are in `runs/eval/scores.json`.
- **The brief check:** TS3012d exists, so we compared the brief against it.
  - The brief, written from meetings a to c only, warned that nobody had costed the design, that cuts were likely, and that speech recognition was still unsettled.
  - In meeting d the first costing came to 17 Euros against 12.50. They cut speech recognition and the special yellow colour, and kept the LCD.

## Three questions it got wrong (or only partly right)

1. **G09 (IS1005, radio vs infrared).**
   - *Got right:* the people and the price argument.
   - *Missed:* the final infrared design used the company's own chip.
   - *Why:* that fact sits in a meeting-c turn about something else. The agent stopped searching once each part of the question had an answer. Retrieval is "good enough per part", not exhaustive.
2. **G04 (ES2012, keeping voice recognition).**
   - *Got right:* who pushed and who backed them.
   - *Missed:* that the final costing came in at exactly 12.50.
   - *Why:* the figure is buried inside a single 482-word turn where the project manager reads the cost sheet. Long monologue turns hide decisions and numbers, and the extractor gave that turn one event.
3. **G14 (IS1005, false premise).**
   - *Got right:* it said the premise was false.
   - *Missed:* that features were being *added*, not cut, at the end of their last meeting.
   - *Why:* the agent answered the negative and did not look for the positive counter-evidence.

Ledger-level failures the agent had to work around:

- **TS3004 speech recognition shows `open`, with one reversal.** In fact it was in the final design, after several reversals. A late remark about how many speech buttons there would be was recorded as `deferred` on the feature itself. A ledger-only answer to Q13 would be wrong; the agent got it right by reading the turns.
- **Valid but off-topic quotes.** When a citation had no `contains` hint, or the hint was not found in the turn, code quoted the turn's first words. That passes the checker but may not support the claim. The reviewer session found this in five official answers (Q07, Q10, Q12, Q15, Q25) and re-pointed them, tightened the wording of Q16, Q19 and Q25, and corrected three points in the brief. `answer-submit` now warns when it happens.
- **TS3006's Speaker B shows two names.** The regex took "May" from "Yeah I'm I'm May maybe...". The interface flags it as a conflict instead of hiding it.

## Cost and time for 1,000 meetings

Measured on this run:

- **Ingest:** 30 ms per meeting, all in code.
- **Extraction volume:** 1.75 parts per meeting, a prompt of about 8,300 input tokens per part, and about 33 events (around 2,500 output tokens) per part. About 20% of parts needed a resubmit.

| | Haiku 5.5 ($0.10 / $0.50 per million tokens) | Sonnet 5.5 ($2 / $10) |
|---|---|---|
| Extraction cost per meeting (API path, retries included) | about $0.0044 | about $0.09 |
| **1,000 meetings** | **about $4.40** | about $90 |
| Answering one question (agent, about 50k input / 2k output tokens) | | about $0.12 |

On Opus 5.5 ($4 / $20), the model that actually answered here, the same question is about $0.24.

- **Wall time, API path:** each part is one call of 15 to 30 seconds. 1,000 meetings is about 1,750 calls, so roughly 40 minutes at 20 calls in parallel, or about 10 hours one at a time. Ingest adds 30 seconds.
- **What actually ran:** this run used Claude subscription session workers instead of the API, at about 250,000 tokens per four-meeting team, with 10 to 14 workers in parallel. 30 teams finished in roughly 25 minutes of wall time.
- **Prices:** dollar figures are API list prices applied to measured token counts, not a bill.

## Platform: what I chose and where it broke

- **Python standard library and SQLite with FTS5.** Nothing to install, it runs anywhere, and it is about 28 MB for 138 meetings (about 2 GB projected at 10,000).
- **Keyword search only.** I left out vectors deliberately: every question names its team, which leaves about 20,000 words to search. Without team scoping, at 10,000 meetings, vector search would be needed.
- **Models:**
  - Haiku extracts. Sonnet-class models answer; this run used the session's own model, Opus 5.5, to answer.
  - The tools are an MCP server (`meetmem/mcp_server.py`), so the same tools serve the CLI agent, Claude Code or Desktop, and the web interface.
- **Where it broke:**
  - **Usage limits:** the subscription's 5-hour limit stopped the workers twice mid-run.
    - Ingestion is resumable per part, so nothing was lost, but it shows the real constraint at scale: throughput limits, not cost.
    - The standalone `claude -p` path (`meetmem/agent.py`) was blocked by a weekly limit, so it has been tested only against a fake CLI. The submitted answers were produced by session workers using the same tools and checker.
  - **Typed quotes:** before quotes were moved into code, typed quotes failed on punctuation, `L_C_D_` and split turns.
  - **Topic slugs:** small models invent near-duplicate slugs such as `battery-type` next to `energy-source`.

## What production would need

- **Extraction:** the batch API for extraction (half price, no rate-limit races), plus a nightly job that extracts each new meeting.
- **Merging and checking:**
  - a topic-merge pass per team;
  - a second-pass checker on status events, so that "detail" events stop flipping a feature's state;
  - a learned speaker identity across meetings.
- **Scale:** vector search once questions stop naming a team, and Postgres or D1 instead of a local file.
- **Monitoring:** an eval run on every prompt change, and per-team access control.
