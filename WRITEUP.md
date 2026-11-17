# Meeting memory: one-page write-up

Full version with every number and example: [docs/WRITEUP_FULL.md](docs/WRITEUP_FULL.md).

## Architecture

```
transcripts → parse + index (code) → extract events (cheap model, per meeting part) → ledger (SQLite)
           → agent with read tools (stronger model) → pointer citations → code builds and checks quotes → answers.json
```

- **Store.** One SQLite file holds every turn, about 300-word passages with an FTS5 keyword index, and the event ledger. Search runs on a cleaned column ("12.5" finds "twelve and a half", "LCD" finds `L_C_D_`). Quotes always come from the raw text.
- **Events.** A cheap model reads one meeting part and records what happened (decided, rejected, reinstated, number, promise, report-back), each pointing at a turn id. Code validates every pointer and returns rejected items with reasons for the model to fix.
- **What is current.** Code orders each topic's events by meeting and turn. The latest status event is the current state, and code counts reversals. An "as of" cutoff in SQL is how the TS3012 brief was written without seeing meeting d.
- **Answering.** The agent reads a topic's timeline, re-reads the real turns, checks the team's last meeting, and cites as `{meeting_id, turn_idx, contains}`. Code turns that into the exact quote, or refuses it.
- **Scale.** Everything is per meeting and incremental, and search is scoped by team. Nothing is pasted into one prompt.

## Model vs code

Code does parsing, indexing, normalising, quote building and checking, ordering, status and reversal counts, the time cutoff, and linking promises to report-backs. These must be exact and auditable, and half of all turns are under six words, so a model-typed quote often fails a literal grader. A cheap model decides what happened in messy speech and which topic it belongs to: high volume, needs language understanding. A stronger model answers and writes the brief: low volume, the hardest reasoning. The ledger is an index, not the truth.

## Proof it works

- **Official set:** 25 of 25 answers and the brief pass the strict checker.
- **Our test set:** 14 questions on six other teams, written from raw transcripts before answering, graded by a separate reviewer.
  - 51 of 56 key facts covered (91%); 9 of 14 answers cover every fact.
  - Every citation valid, and no answer contradicts a fact.
  - Three questions were seen with their answers before answering, and are disclosed as contaminated.
- **The brief:** checked against the real meeting d. It warned the design was uncosted and cuts were coming. In meeting d the first costing came to 17 Euros against 12.50, and speech recognition and the yellow colour were cut.

## Three it got wrong

- **G09:** missed that the final infrared design used the company's own chip. It stopped searching once each part of the question had an answer.
- **G04:** missed that the final costing was exactly 12.50. The figure was buried in one 482-word cost-sheet turn.
- **G14:** said the false premise was false, but missed that features were being added, not cut. It did not look for the counter-evidence.
- **Ledger bugs:**
  - TS3004's speech recognition shows "open" because a detail was filed against the feature itself; the agent was right only because it reads the turns.
  - Five quotes were valid but off-topic. A reviewer re-pointed them, and code now warns.

## Cost and time for 1,000 meetings

Measured: 1.75 parts per meeting, about 8,300 input and 2,500 output tokens per part, about 20% resubmits.

- **Haiku 5.5:** about $4.40 per 1,000 meetings at list price.
- **Sonnet 5.5:** about $90 per 1,000 meetings.
- **Time:** about 40 minutes at 20 parallel calls. Code ingest adds 30 seconds.

## Platform, and where it broke

- **Stack:** Python standard library and SQLite FTS5. Vectors were left out deliberately, because questions name a team.
- **Models:** Claude Haiku 5.5 extracts and Claude Opus 5.5 answers. The tools are an MCP server, also used by a local web interface.
- **Subscription, not an API key:** I ran the build on my Claude subscription. With hundreds of agent calls, a flat monthly plan costs far less than paying per token on an API key. The code takes an API key the same way, and a production service would use the API's batch endpoint.
- **What broke:**
  - Subscription usage limits stopped the workers twice; every step is resumable.
  - The standalone `claude -p` runner was blocked by a weekly limit, so it is tested only against a fake CLI. `answers.json` came from Claude agent sessions using the same tools and checker.
  - Typed quotes failed until quotes moved into code.
  - The small model drifts on topic names.

## What production needs

- The batch API with a nightly job for new meetings.
- A topic-merge pass, a status-event checker and a citation relevance check.
- Speaker identity across meetings.
- Vectors once questions stop naming a team.
- Postgres, access control, and an eval run on every prompt change.
