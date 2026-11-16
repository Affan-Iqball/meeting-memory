"""Parse one AMI-style transcript file into a meeting record and ordered turns.

File shape (as delivered)::

    Meeting: TS3004b
    Team: TS3004
    Meeting number: 2 (b) of the team's four meetings
    Speakers: Speaker A, Speaker B, Speaker C, Speaker D
    Times: ...
    Source: ...

    00:00:34 Speaker A
    turn text (possibly wrapped over several lines)

    00:00:38 Speaker D
    ...

Public API::

    parse_file(path) -> ParsedMeeting
    parse_text(text, fallback_id=None) -> ParsedMeeting

``ParsedMeeting.problems`` lists anything that did not parse cleanly; the
parser never raises on content problems (only on unreadable files), so a
caller ingesting 10,000 files a day can log and continue.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

MEETING_ID_RE = re.compile(r"^([A-Z]{2}\d{4})([a-z])$")
TURN_HEAD_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})\s+Speaker\s+([A-Z])\s*$")
HEADER_RE = re.compile(r"^([A-Za-z][A-Za-z ]{0,30}):\s*(.*)$")


@dataclass
class Turn:
    turn_idx: int          # 0-based within the meeting
    speaker: str           # "A", "B", ...
    time_str: str          # "HH:MM:SS" exactly as in the file
    start_sec: int
    text: str              # raw text; wrapped lines joined with one space
    n_words: int           # len(text.split())


@dataclass
class ParsedMeeting:
    meeting_id: str
    team: str
    letter: str
    header: dict = field(default_factory=dict)
    turns: list = field(default_factory=list)
    problems: list = field(default_factory=list)

    @property
    def n_words(self) -> int:
        return sum(t.n_words for t in self.turns)

    @property
    def ok(self) -> bool:
        return not self.problems


def _split_id(mid: str):
    m = MEETING_ID_RE.match(mid or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def parse_text(text: str, fallback_id: str | None = None) -> ParsedMeeting:
    problems: list[str] = []
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # --- header: key: value lines until the first turn head ---
    header: dict[str, str] = {}
    i = 0
    while i < len(lines):
        ln = lines[i]
        if TURN_HEAD_RE.match(ln.strip()):
            break
        if ln.strip():
            m = HEADER_RE.match(ln.strip())
            if m:
                header[m.group(1).strip().lower()] = m.group(2).strip()
            else:
                problems.append(f"line {i+1}: unrecognised header line {ln[:60]!r}")
        i += 1

    mid = header.get("meeting") or fallback_id or ""
    team, letter = _split_id(mid)
    if team is None and fallback_id and fallback_id != mid:
        problems.append(f"header meeting id {mid!r} malformed; using file name")
        mid = fallback_id
        team, letter = _split_id(mid)
    if team is None:
        problems.append(f"cannot derive team/letter from meeting id {mid!r}")
        team, letter = header.get("team", ""), ""
    if fallback_id and mid != fallback_id:
        problems.append(f"header meeting id {mid!r} != file name {fallback_id!r}")
    if header.get("team") and header["team"] != team:
        problems.append(f"header team {header['team']!r} != id-derived team {team!r}")

    # --- turns ---
    turns: list[Turn] = []
    cur = None  # (speaker, time_str, secs, [lines], lineno)

    def flush():
        if cur is None:
            return
        spk, ts, secs, body, lineno = cur
        txt = " ".join(s.strip() for s in body if s.strip())
        if not txt:
            problems.append(f"line {lineno}: turn with empty text (kept)")
        turns.append(Turn(len(turns), spk, ts, secs, txt, len(txt.split())))

    for j in range(i, len(lines)):
        raw = lines[j]
        m = TURN_HEAD_RE.match(raw.strip())
        if m:
            flush()
            h, mi, s, spk = m.groups()
            ts = f"{int(h):02d}:{mi}:{s}"
            cur = (spk, ts, int(h) * 3600 + int(mi) * 60 + int(s), [], j + 1)
        elif raw.strip():
            if cur is None:
                problems.append(f"line {j+1}: text before first turn head")
            else:
                cur[3].append(raw)
    flush()

    if not turns:
        problems.append("no turns found")
    for a, b in zip(turns, turns[1:]):
        if b.start_sec < a.start_sec:
            problems.append(f"turn {b.turn_idx}: start time goes backwards ({a.time_str} -> {b.time_str})")
            break
    return ParsedMeeting(mid, team, letter or "", header, turns, problems)


def parse_file(path: str) -> ParsedMeeting:
    fallback = os.path.splitext(os.path.basename(path))[0]
    with open(path, encoding="utf-8", errors="strict") as fh:
        text = fh.read()
    return parse_text(text, fallback_id=fallback)
