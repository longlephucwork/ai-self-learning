"""Parse a Claude Code JSONL transcript into a structured session record.

The transcript format is internal to Claude Code and changes between versions,
so this parser is deliberately lenient: unknown line types are ignored and
missing fields default to None.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field

from .redact import redact, redact_obj

MAX_INPUT_CHARS = 2000
MAX_RESULT_PREVIEW = 500
MAX_REQUEST_CHARS = 8000
MAX_FINAL_CHARS = 8000

_SYS_REMINDER = re.compile(r"<system-reminder>[\s\S]*?</system-reminder>")
_CMD_NAME = re.compile(r"<command-name>([\s\S]*?)</command-name>")
_CMD_ARGS = re.compile(r"<command-args>([\s\S]*?)</command-args>")
_NOISE_PREFIXES = ("<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>",
                   "[Request interrupted", "<bash-stdout>", "<bash-stderr>", "<task-notification>")

FILE_TOOLS = {
    "Read": ("read", "file_path"),
    "Write": ("write", "file_path"),
    "Edit": ("edit", "file_path"),
    "MultiEdit": ("edit", "file_path"),
    "NotebookEdit": ("edit", "notebook_path"),
}


@dataclass
class Step:
    idx: int
    tool_use_id: str | None
    tool_name: str
    input_json: str
    timestamp: str | None
    is_error: bool | None = None
    result_preview: str | None = None


@dataclass
class Session:
    session_id: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    cc_version: str | None = None
    entrypoint: str | None = None
    models: list[str] = field(default_factory=list)
    started_at: str | None = None
    last_activity_at: str | None = None
    requests: list[dict] = field(default_factory=list)   # {idx, timestamp, text}
    steps: list[Step] = field(default_factory=list)
    files: dict = field(default_factory=dict)            # (path, action) -> count
    final_result: str | None = None
    n_sidechain_lines: int = 0
    n_lines: int = 0


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 15] + "…[truncated]"


def _clean_user_text(text: str) -> str | None:
    """Return the human-authored part of a user message, or None if it's noise."""
    text = _SYS_REMINDER.sub("", text).strip()
    if not text:
        return None
    m = _CMD_NAME.search(text)
    if m:
        args = _CMD_ARGS.search(text)
        return (m.group(1).strip() + " " + (args.group(1).strip() if args else "")).strip()
    if text.startswith(_NOISE_PREFIXES):
        return None
    return text


def _tool_result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, dict):
                parts.append(f"[{b.get('type')}]")
        return "\n".join(parts)
    return ""


class TranscriptParser:
    """Builds a Session line by line, and can save its progress and resume, so a growing transcript is
    parsed once in total instead of once per turn (see parse_incremental)."""

    def __init__(self) -> None:
        self.s = Session()
        self.steps_by_id: dict[str, Step] = {}
        self.last_text: str | None = None   # newest assistant text since the last snapshot (raw)
        self.final: str | None = None       # redacted, truncated final result as of the last snapshot

    def feed(self, d) -> None:
        s = self.s
        if not isinstance(d, dict):
            return
        s.n_lines += 1
        s.session_id = s.session_id or d.get("sessionId")
        ts = d.get("timestamp")
        if ts:
            s.started_at = s.started_at or ts
            s.last_activity_at = ts

        typ = d.get("type")
        if typ not in ("user", "assistant"):
            return
        if d.get("isSidechain"):
            s.n_sidechain_lines += 1
            return

        s.cwd = s.cwd or d.get("cwd")
        s.git_branch = s.git_branch or d.get("gitBranch")
        s.cc_version = d.get("version") or s.cc_version
        s.entrypoint = s.entrypoint or d.get("entrypoint")
        msg = d.get("message") or {}
        content = msg.get("content")

        if typ == "user":
            if d.get("isMeta") or d.get("isCompactSummary"):
                return
            texts = []
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        texts.append(b.get("text", ""))
                    elif b.get("type") == "tool_result":
                        st = self.steps_by_id.get(b.get("tool_use_id"))
                        if st is not None:
                            st.is_error = bool(b.get("is_error"))
                            st.result_preview = _truncate(redact(_tool_result_text(b)), MAX_RESULT_PREVIEW)
            cleaned = _clean_user_text("\n".join(texts)) if texts else None
            if cleaned:
                s.requests.append({
                    "idx": len(s.requests),
                    "timestamp": ts,
                    "text": _truncate(redact(cleaned), MAX_REQUEST_CHARS),
                })

        else:  # assistant
            model = msg.get("model")
            if model and model not in s.models and not model.startswith("<"):
                s.models.append(model)
            if not isinstance(content, list):
                return
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and b.get("text", "").strip():
                    self.last_text = b["text"]
                elif bt == "tool_use":
                    tid = b.get("id")
                    if tid and tid in self.steps_by_id:
                        continue
                    name = b.get("name") or "?"
                    inp = b.get("input") or {}
                    st = Step(
                        idx=len(s.steps),
                        tool_use_id=tid,
                        tool_name=name,
                        input_json=_truncate(json.dumps(redact_obj(inp), ensure_ascii=False), MAX_INPUT_CHARS),
                        timestamp=ts,
                    )
                    s.steps.append(st)
                    if tid:
                        self.steps_by_id[tid] = st
                    if name in FILE_TOOLS and isinstance(inp, dict):
                        action, key = FILE_TOOLS[name]
                        p = inp.get(key)
                        if isinstance(p, str):
                            s.files[(p, action)] = s.files.get((p, action), 0) + 1

    def feed_raw(self, raw: bytes | str) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        raw = raw.strip()
        if not raw:
            return
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            return
        self.feed(d)

    def _settle(self) -> None:
        if self.last_text:
            self.final = _truncate(redact(self.last_text), MAX_FINAL_CHARS)
            self.last_text = None

    def session(self) -> Session:
        self._settle()
        self.s.final_result = self.final
        return self.s

    # Saved state holds only what's already stored in session.json: redacted, truncated text.
    def to_state(self) -> dict:
        self._settle()
        st = asdict(self.s)
        st["files"] = [[p, a, c] for (p, a), c in self.s.files.items()]
        st["final_result"] = self.final
        return st

    @classmethod
    def from_state(cls, st: dict) -> TranscriptParser:
        st = dict(st)
        steps = [Step(**x) for x in st.pop("steps")]
        files = {(p, a): c for p, a, c in st.pop("files")}
        final = st.pop("final_result")
        self = cls()
        self.s = Session(**st, steps=steps, files=files)
        self.steps_by_id = {x.tool_use_id: x for x in steps if x.tool_use_id}
        self.final = final
        return self


def parse_transcript(path: str) -> Session:
    parser = TranscriptParser()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            parser.feed_raw(raw)
    return parser.session()


# ---------- resuming from where the last run stopped ----------

TAIL_BYTES = 4096   # bytes before a saved offset that must be unchanged to trust it


def tail_hash(fin, offset: int) -> str:
    """Hash of the TAIL_BYTES before `offset` in a binary file."""
    lo = max(0, offset - TAIL_BYTES)
    fin.seek(lo)
    return hashlib.sha256(fin.read(offset - lo)).hexdigest()


def resumable(state: dict, fin, source: str, version: int) -> bool:
    """Whether `state` (offset, tail_hash, source, version) still describes the start of this file."""
    offset = state.get("offset", 0)
    size = os.fstat(fin.fileno()).st_size
    return (state.get("version") == version and state.get("source") == source and 0 < offset <= size
            and tail_hash(fin, offset) == state.get("tail_hash"))


def read_complete_lines(fin, start: int, on_line) -> int:
    """on_line(raw bytes) for each complete line from `start`; returns the offset just past the last one.

    A last line without a newline is taken only if it's whole JSON (a transcript line that's still being
    written isn't), so a half-written line is left for the next run instead of being lost."""
    fin.seek(start)
    pos = start
    for raw in fin:
        if not raw.endswith(b"\n"):
            try:
                json.loads(raw)
            except ValueError:
                break
        pos += len(raw)
        on_line(raw)
    return pos


# Bump when what the parser stores changes (fields, limits, cleaning, redaction), so saved states from an
# older parser are thrown away and the transcript is parsed again from the start.
PARSE_STATE_VERSION = 1


def parse_incremental(path: str, state: dict | None) -> tuple[Session, dict]:
    """Parse only what was added since `state` (from a previous call), or the whole file when `state` is
    missing or can't be trusted. Returns the session and the state to pass next time."""
    source = os.path.abspath(path)
    with open(path, "rb") as fin:
        if state and resumable(state, fin, source, PARSE_STATE_VERSION):
            parser, start = TranscriptParser.from_state(state["parser"]), state["offset"]
        else:
            parser, start = TranscriptParser(), 0
        end = read_complete_lines(fin, start, parser.feed_raw)
        new_state = {"version": PARSE_STATE_VERSION, "source": source, "offset": end,
                     "tail_hash": tail_hash(fin, end), "parser": parser.to_state()}
    return parser.session(), new_state
