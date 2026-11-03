"""Parse a Claude Code JSONL transcript into a structured session record.

The transcript format is internal to Claude Code and changes between versions,
so this parser is deliberately lenient: unknown line types are ignored and
missing fields default to None.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

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


def parse_transcript(path: str) -> Session:
    s = Session()
    steps_by_id: dict[str, Step] = {}
    last_text: str | None = None

    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            s.n_lines += 1
            s.session_id = s.session_id or d.get("sessionId")
            ts = d.get("timestamp")
            if ts:
                s.started_at = s.started_at or ts
                s.last_activity_at = ts

            typ = d.get("type")
            if typ not in ("user", "assistant"):
                continue
            if d.get("isSidechain"):
                s.n_sidechain_lines += 1
                continue

            s.cwd = s.cwd or d.get("cwd")
            s.git_branch = s.git_branch or d.get("gitBranch")
            s.cc_version = d.get("version") or s.cc_version
            s.entrypoint = s.entrypoint or d.get("entrypoint")
            msg = d.get("message") or {}
            content = msg.get("content")

            if typ == "user":
                if d.get("isMeta") or d.get("isCompactSummary"):
                    continue
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
                            st = steps_by_id.get(b.get("tool_use_id"))
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
                    continue
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text" and b.get("text", "").strip():
                        last_text = b["text"]
                    elif bt == "tool_use":
                        tid = b.get("id")
                        if tid and tid in steps_by_id:
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
                            steps_by_id[tid] = st
                        if name in FILE_TOOLS and isinstance(inp, dict):
                            action, key = FILE_TOOLS[name]
                            p = inp.get(key)
                            if isinstance(p, str):
                                s.files[(p, action)] = s.files.get((p, action), 0) + 1

    if last_text:
        s.final_result = _truncate(redact(last_text), MAX_FINAL_CHARS)
    return s
