# meetmem: memory over a team's meetings

meetmem turns the AMI meeting transcripts in `data/transcripts/` (138 meetings, 35 teams designing a TV remote) into a database you can ask questions of: what was decided, how it changed between meetings, who said what, who promised to check something and whether they reported back, and the numbers. Every answer is backed by quotes that code copies from the transcript and checks. A model never types a quote.

It is plain Python 3.12 with only the standard library. SQLite (with FTS5) holds everything in `memory.db`.

## Architecture

1. **Ingest** (`store.py`, `parse.py`): each transcript becomes turns (`[t567 A 00:39:13] text`), ~300-word search passages and an FTS index. Speaker self-introductions are picked up by regex (`speakers.py`).
2. **Extract** (`events.py`, `prompts/extract.md`): a cheap model reads one meeting part at a time and submits *events* (proposed, decided, rejected, reinstated, deferred, objection, number, action_assigned, action_reported, requirement, info). Each event points at a turn id. Code validates the pointer, copies the evidence quote from the turn, and rejects bad items with reasons so the model can fix them.
3. **Ledger reads** (`tools.py`): `topics`, `timeline`, `facts`, `actions`, `search`, `turns`, `speakers`, all scoped to a team and with an `--as-of` cutoff applied in SQL. Status, reversals and the last-reversal meeting are computed by code from the events.
4. **Answer** (`agent.py`, `prompts/answer.md`, `prompts/brief.md`): an agent model uses the read tools through an MCP server (`mcp_server.py`), reads the real turns, and submits an answer whose citations are `{meeting_id, turn_idx, contains}`. Code turns them into exact quotes and refuses bad ones.
5. **Export and check** (`tools.export_answers`, `check.py`): the submission file in the format `data/questions.json` asks for, validated against the transcripts.

The ledger is an index into the evidence, not the truth. The answering prompt makes the model re-read the turns around every key event and check the team's last meeting before it answers.

## Who does what

| Step | Model | Code |
|---|---|---|
| Parse, passages, search index | none | everything |
| Speaker names and roles | fills gaps the regex misses | regex self-intros; per-meeting resolution; conflicts shown, never merged |
| Events | chooses what happened, kind, topic, turn id | validates turn ids, copies quotes, dedups overlap, keeps event ids stable |
| Status and reversals | none | computed from the event sequence |
| Finding the evidence | picks topics, searches, reads turns | ranking, `as_of` cutoff, turn windows |
| Answer text | writes 1-3 sentences | the MCP `submit_answer` tool rejects more than 3 sentences; `check` warns |
| Citations | points at a turn | copies and verifies the quote (6+ words, one turn, right team, before the cutoff) |

## Running everything from scratch

All commands run from the project folder. `--db PATH` (or `$MEETMEM_DB`) picks another database.

```sh
# 1. ingest (seconds; idempotent per meeting)
python3 -m meetmem ingest data/transcripts
python3 -m meetmem stats

# 2. extract the event ledger, one team at a time (resumable: finished parts are skipped)
python3 -m meetmem extract TS3004 --model haiku
python3 -m meetmem extract-plan TS3004          # 'next' is null when the team is done
python3 -m meetmem topics --team TS3004
python3 -m meetmem timeline --team TS3004 --topic speech-recognition
python3 -m meetmem speakers --team TS3006        # who each letter is, per meeting

# 3. answer the official questions (resumable; --force redoes answered ones)
python3 -m meetmem ask-all --questions data/questions.json --run official --model sonnet
python3 -m meetmem ask Q13 --run official --force          # redo one
python3 -m meetmem ask --team TS3004 "Was speech recognition in the final design?"

# 4. the brief for TS3012, written as if only meetings 1-3 had happened
python3 -m meetmem brief TS3012 --as-of 3 --run official

# 5. export and check
python3 -m meetmem export-answers --run official > answers.json
python3 -m meetmem.check answers.json

# cost and time of every model call
python3 -m meetmem usage --run official
python3 -m meetmem usage
```

Tests (temp databases only, no model calls; a fake `claude` script stands in for the CLI):

```sh
python3 -m unittest discover -s tests
```

## The model backend

`agent.py` shells out to the Claude Code CLI in print mode. The binary is `$CLAUDE_BIN`, or `claude` on PATH. The CLI uses `ANTHROPIC_API_KEY` when it is set; otherwise it uses the subscription you are logged in with (`claude auth`). With an API key set, calls also get `--bare` (no CLAUDE.md, hooks or plugins in the context); set `MEETMEM_CLAUDE_BARE=1` to force that.

The flags used for answers and briefs:

```
claude -p --output-format json --model sonnet --system-prompt "<answer.md, filled in>" --max-turns 40 \
  --no-session-persistence --disable-slash-commands --permission-mode dontAsk \
  --strict-mcp-config --tools "" --mcp-config <tmp>/mcp.json --allowedTools mcp__meetmem
```

The prompt goes on stdin. `--tools ""` removes every built-in tool (no shell, files or web), `--strict-mcp-config` ignores your other MCP servers, and `--permission-mode dontAsk` denies anything not in `--allowedTools`. Each call runs in an empty temp directory. Extraction uses the same flags without the MCP server (the model only replies with JSON); rejections are fed back for up to 3 tries per part.

Every call is logged in the `llm_runs` table (tokens, cache tokens, cost, duration, turns, ok, error). If the account hits its usage limit, the command stops with exit code 3 and a message; rerun it later and it carries on where it stopped.

## Asking live questions from Claude Code or Claude Desktop

The MCP server speaks JSON-RPC over stdio and exposes `meetings`, `topics`, `timeline`, `facts`, `actions`, `search`, `turns`, `speakers`, `submit_answer` and `submit_brief`.

Claude Code, from any folder:

```sh
claude mcp add meetmem --scope user \
  -e PYTHONPATH="/path/to/meeting-memory" \
  -e MEETMEM_DB="/path/to/meeting-memory/memory.db" \
  -- python3 -m meetmem.mcp_server
```

Claude Desktop (`~/.config/Claude/claude_desktop_config.json`), or a project `.mcp.json` for Claude Code:

```json
{
  "mcpServers": {
    "meetmem": {
      "command": "python3",
      "args": ["-m", "meetmem.mcp_server"],
      "env": {
        "PYTHONPATH": "/path/to/meeting-memory",
        "MEETMEM_DB": "/path/to/meeting-memory/memory.db",
        "MEETMEM_RUN": "live"
      }
    }
  }
}
```

Then ask, for example: "Using the meetmem tools, which of LCD and speech recognition did team TS3006 drop, and why?" Answers submitted from a live session are stored under run `live`.

Optional environment for the server: `MEETMEM_AS_OF_TEAM` and `MEETMEM_AS_OF_ORD` together limit every call to one team and its meetings up to that number, whatever the model asks for (the `brief` command sets them). `MEETMEM_QUESTION_ID` limits `submit_answer` to one question; `MEETMEM_SOURCE` tags submissions.

## Web interface (for someone else to look around)

```sh
python3 -m meetmem.web          # then open http://127.0.0.1:8765
```

Standard library only, reads the same `memory.db`:

- **Answers:** every answer, with citations you can click. A click opens the raw transcript with the quoted words highlighted.
- **Brief:** the TS3012 pre-meeting brief, built from meetings 1 to 3.
- **Explore a team:** every topic, with its current status and reversal count worked out by code, and an "as of meeting N" switch to rewind. Click a topic to see its timeline. Commitments are shown with whether and where they were reported back.
- **Search:** keyword search over events and transcripts. "12.5" finds "twelve and a half"; "LCD" finds `L_C_D_`.
- **Setup:** connect a model once.
  - It finds the Claude command-line tool by itself: `CLAUDE_BIN`, then `claude` on PATH, then the copy bundled with the Claude desktop app.
  - Paste an Anthropic API key, or leave it empty to use your Claude subscription login (`claude login`).
  - Pick the models, then press Test for one tiny call.
  - The key is kept in memory, or, if you tick "remember", in `~/.config/meetmem/settings.json` (mode 600), never in the project. An `ANTHROPIC_API_KEY` environment variable always wins.
- **Ask:** a new question about one team, run in the background with a live log, quotes and cost.
- **Run:** answer all 25 questions into a separate run, write a brief as of any meeting, extract a team's meetings, export and check a run, or re-ingest. Jobs run one at a time in the background, with logs and cost. Paid jobs ask for confirmation first.

The server only listens on 127.0.0.1, and it refuses POST requests from other sites, so a web page cannot change your settings or start jobs.

## Status (16 Nov 2026)

| Step | Backend and model | Result |
|---|---|---|
| Ingest | code | 138 meetings, 48,148 turns, 676,745 words, 3,294 passages, about 4 s |
| Extraction | Claude subscription session workers, Haiku 5.5, through `extract-prompt` / `submit-events` | all 35 teams, 138 meetings; 8,083 events |
| Official answers | session agent (Claude Opus 5.5) using the same read tools; citations built by `answer-submit` | `check`: 25/25 pass, brief ok |
| Brief TS3012 (as of 3) | same | 4 sections, every point cited |
| Our test set (`eval/gold.json`, 14 questions) | same | 14/14 citations valid; 51/56 key points; 9/14 fully complete, graded by a separate reviewer session (see `runs/eval/scores.json`) |
| Cost | API list prices applied to measured tokens | about $4.40 per 1,000 meetings to extract on Haiku 5.5 (see WRITEUP.md) |

Files: `answers.json` (submission), `WRITEUP.md` (one-page write-up; full version in `docs/WRITEUP_FULL.md`), `runs/official/` (answer and brief sources), `runs/eval/` (test-set answers and scores), `eval/` (gold questions and notes).

Known limits:

- The standalone `claude -p` path (`ask`, `ask-all`, `brief`, `extract`) has been tested only with a fake CLI, because the CLI login was at its weekly limit. The submitted answers came from session workers using the same tools, prompts and checker.
- The status a topic shows is the kind of its last status event. A late `proposed` or `deferred` about a detail of a feature (how many buttons, where it goes) makes a feature that is in the design show as `open`; `last settled` then says what was last decided.
- A speaker letter is only a seat. Names and roles are resolved per meeting; values taken from other meetings are marked `?`, `from other meetings`.
