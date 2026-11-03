#!/usr/bin/env python3
"""Phase 3 validation: replay a draft's tests in a sandbox, check test provenance.

  replay.py <draft-name>            re-run tests for generated/<name>/ and update validation.json
  replay.py <draft-name> --allow-no-netns   run even if network isolation is unavailable

Sandbox per test:
  * fresh temp dir holding only the draft's files and the test's input files
  * `unshare -rn`: new user + network namespace, so no network at all
  * `python3 -I`, empty environment (PATH, LANG, HOME=sandbox), no secrets
  * CPU 10s, memory 512MB, max file size 10MB, wall-clock timeout 20s
Generated code never runs outside this.

Provenance (no execution): a `logged` test's input files and expected outputs
must appear in the source session's redacted transcript. A
`derived_from_feedback` test needs logged inputs and a rating note on that
session. `synthetic` tests are allowed but don't count toward coverage.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_lib import store  # noqa: E402

TIMEOUT_S = 20
MAX_OUTPUT = 4000


# ---------------- sandbox ----------------

def netns_available() -> bool:
    if not shutil.which("unshare"):
        return False
    try:
        return subprocess.run(["unshare", "-rn", "true"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _limits():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 2**20, 512 * 2**20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 2**20, 10 * 2**20))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def safe_rel(path: str) -> str:
    p = Path(path)
    if p.is_absolute() or ".." in p.parts or not path or path.startswith("~"):
        raise ValueError(f"unsafe path {path!r}")
    return p.as_posix()


def _norm_json(text: str):
    return json.loads(text)


def _compare(actual: str, expected: str, mode: str) -> tuple[bool, str]:
    if mode == "ignore":
        return True, ""
    if mode == "json":
        try:
            a = _norm_json(actual)
        except json.JSONDecodeError as e:
            return False, f"actual is not valid JSON: {e}"
        try:
            b = _norm_json(expected)
        except json.JSONDecodeError as e:
            return False, f"expected is not valid JSON: {e}"
        return (a == b), ("" if a == b else "JSON differs")
    ok = actual.rstrip("\n") == expected.rstrip("\n")
    return ok, ("" if ok else "text differs")


def run_test(files: dict[str, str], entrypoint: str, test: dict, *, netns: bool) -> dict:
    res = {"name": test["name"], "passed": False, "problems": []}
    box = Path(tempfile.mkdtemp(prefix="lh-replay-"))
    try:
        for p, c in files.items():
            dst = box / safe_rel(p)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(c, encoding="utf-8")
        for f in test.get("input_files", []):
            dst = box / safe_rel(f["path"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(f["content"], encoding="utf-8")
        before = {p.relative_to(box).as_posix() for p in box.rglob("*") if p.is_file()}
        cmd = [sys.executable, "-I", safe_rel(entrypoint)] + list(test.get("args", []))
        if netns:
            cmd = ["unshare", "-rn", "--"] + cmd
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(box), "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            p = subprocess.run(cmd, cwd=box, env=env, input=test.get("stdin", ""), capture_output=True,
                               text=True, timeout=TIMEOUT_S, preexec_fn=_limits)
            res.update(exit=p.returncode, stdout=p.stdout[:MAX_OUTPUT], stderr=p.stderr[:MAX_OUTPUT])
        except subprocess.TimeoutExpired:
            res["problems"].append(f"timed out after {TIMEOUT_S}s")
            return res

        if p.returncode != test.get("expected_exit", 0):
            res["problems"].append(f"exit {p.returncode}, expected {test.get('expected_exit', 0)}")
        ok, why = _compare(p.stdout, test.get("expected_stdout", ""), test.get("stdout_compare", "ignore"))
        if not ok:
            res["problems"].append(f"stdout: {why}")
        for ef in test.get("expected_files", []):
            fp = box / safe_rel(ef["path"])
            if not fp.is_file():
                res["problems"].append(f"missing output file {ef['path']}")
                continue
            actual = fp.read_text(encoding="utf-8", errors="replace")
            ok, why = _compare(actual, ef["content"], ef.get("compare", "exact"))
            if not ok:
                res["problems"].append(f"{ef['path']}: {why}")
                res.setdefault("actual_files", {})[ef["path"]] = actual[:MAX_OUTPUT]
        after = {p.relative_to(box).as_posix() for p in box.rglob("*") if p.is_file()}
        expected_new = {safe_rel(ef["path"]) for ef in test.get("expected_files", [])}
        res["unexpected_new_files"] = sorted(after - before - expected_new)
        res["passed"] = not res["problems"]
        return res
    finally:
        shutil.rmtree(box, ignore_errors=True)


# ---------------- provenance ----------------

_LINENO = re.compile(r"^\s*\d+\t", re.M)  # `cat -n` style prefixes in Read tool output
_WS = re.compile(r"\s+")


def _strings(obj, out):
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, list):
        for x in obj:
            _strings(x, out)
    elif isinstance(obj, dict):
        for v in obj.values():
            _strings(v, out)


def session_haystack(sid: str) -> tuple[str, list]:
    """(whitespace-free text of every string in the transcript, list of JSON values found in it)."""
    path = store.session_dir(sid) / "transcript.jsonl.gz"
    strings: list[str] = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            try:
                _strings(json.loads(line), strings)
            except json.JSONDecodeError:
                continue
    text = "\n".join(_LINENO.sub("", s) for s in strings)
    dec = json.JSONDecoder()
    values = []
    for s in strings:
        for m in re.finditer(r"[\[{]", s):
            try:
                v, _ = dec.raw_decode(s, m.start())
                values.append(v)
            except json.JSONDecodeError:
                continue
    return _WS.sub("", text), values


def check_provenance(test: dict, members: set[str], cache: dict) -> list[str]:
    prov = test.get("provenance")
    if prov == "synthetic":
        return []
    sid = test.get("source_session", "")
    match = [m for m in members if m == sid or (len(sid) >= 8 and m.startswith(sid))]
    if not match:
        return [f"source_session {sid!r} is not a member of the pattern"]
    sid = match[0]
    if sid not in cache:
        cache[sid] = session_haystack(sid)
    hay, values = cache[sid]
    problems = []
    for f in test.get("input_files", []):
        if _WS.sub("", f["content"]) not in hay:
            problems.append(f"input {f['path']} not found in session {sid[:8]} logs")
    if test.get("stdin") and _WS.sub("", test["stdin"]) not in hay:
        problems.append(f"stdin not found in session {sid[:8]} logs")
    if prov == "logged":
        outs = list(test.get("expected_files", []))
        if test.get("stdout_compare") in ("exact", "json"):
            outs.append({"path": "<stdout>", "content": test.get("expected_stdout", ""),
                         "compare": test["stdout_compare"]})
        for ef in outs:
            if ef.get("compare") == "json":
                try:
                    want = json.loads(ef["content"])
                except json.JSONDecodeError:
                    problems.append(f"expected {ef['path']} is not valid JSON")
                    continue
                if want not in values:
                    problems.append(f"expected {ef['path']} (as JSON) not found in session {sid[:8]} logs")
            elif _WS.sub("", ef["content"]) not in hay:
                problems.append(f"expected {ef['path']} not found in session {sid[:8]} logs")
    elif prov == "derived_from_feedback":
        if not store.outcome(sid).get("note"):
            problems.append(f"derived_from_feedback but session {sid[:8]} has no rating note")
    else:
        problems.append(f"unknown provenance {prov!r}")
    return problems


# ---------------- whole-draft validation ----------------

def validate_draft(proposal: dict, files: dict[str, str], pattern: dict, *, allow_no_netns=False) -> dict:
    netns = netns_available()
    report = {"validated_at": store.now(), "network_isolated": netns, "tests": [], "provenance": [],
              "coverage": {}, "passed": False, "problems": []}
    members = set(pattern["session_ids"]) - set(pattern.get("excluded_injection", []))
    tests = proposal.get("tests", [])
    cache: dict = {}
    for t in tests:
        probs = check_provenance(t, members, cache)
        if proposal["type"] in ("script", "tool") and t.get("provenance") in ("logged", "derived_from_feedback") \
                and not t.get("expected_files") and t.get("stdout_compare", "ignore") == "ignore":
            probs.append("checks no output (needs expected_files or stdout_compare), so it can't count as evidence")
        report["provenance"].append({"name": t["name"], "provenance": t.get("provenance"), "problems": probs})
        if probs:
            report["problems"] += [f"[{t['name']}] {p}" for p in probs]

    successful = {m for m in members if store.outcome(m)["outcome"] in pattern.get("success_outcomes", ["accepted"])}
    covered = set()
    for t in tests:
        if t.get("provenance") in ("logged", "derived_from_feedback"):
            covered |= {m for m in successful if m.startswith(t.get("source_session") or "\0")}
    need = min(3, len(successful))
    report["coverage"] = {"successful_sessions": len(successful), "covered": len(covered), "required": need}
    if len(covered) < need:
        report["problems"].append(f"tests cover {len(covered)} successful session(s); need {need}")

    if proposal["type"] in ("script", "tool"):
        if not tests:
            report["problems"].append("no tests")
        if not netns and not allow_no_netns:
            report["problems"].append("network isolation (unshare -rn) unavailable; refusing to run generated "
                                      "code. Pass --allow-no-netns to run without it.")
        else:
            entry = proposal["entrypoint"]
            if entry not in files:
                report["problems"].append(f"entrypoint {entry!r} not among files")
            else:
                for t in tests:
                    r = run_test(files, entry, t, netns=netns)
                    report["tests"].append(r)
                    if not r["passed"]:
                        report["problems"].append(f"[{t['name']}] failed: {'; '.join(r['problems'])}")
    else:  # skill: static checks only (behavioural evaluation needs a model run; see README)
        skill = files.get("SKILL.md", "")
        m = re.match(r"^---\n(.*?)\n---\n", skill, re.S)
        if not m:
            report["problems"].append("SKILL.md missing YAML frontmatter")
        else:
            fm = m.group(1)
            for key in ("name", "description"):
                if not re.search(rf"^{key}:\s*\S", fm, re.M):
                    report["problems"].append(f"SKILL.md frontmatter missing {key}")
        for ref in re.findall(r"`(scripts/[^`\s]+)`", skill):
            if ref not in files:
                report["problems"].append(f"SKILL.md references missing file {ref}")
    report["passed"] = not report["problems"]
    report["fingerprint"] = fingerprint(files, tests)
    return report


def fingerprint(files: dict[str, str], tests: list) -> str:
    """Hash of exactly what was validated: file bytes + tests. Used to detect edits after validation."""
    import hashlib
    blob = json.dumps({"files": dict(sorted(files.items())), "tests": tests}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_draft(name: str) -> tuple[dict, dict[str, str], Path]:
    d = drafts_dir() / name
    proposal = store.read_json(d / "proposal.json")
    if not proposal:
        raise LookupError(f"no draft {name!r} in {drafts_dir()}")
    files = {f: (d / "files" / f).read_text(encoding="utf-8") for f in proposal["file_list"]}
    return proposal, files, d


def drafts_dir() -> Path:
    p = Path(os.environ.get("LEARNING_HARNESS_GENERATED", store.REPO_ROOT / "generated"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name")
    ap.add_argument("--allow-no-netns", action="store_true")
    a = ap.parse_args(argv)
    try:
        proposal, files, d = load_draft(a.name)
    except LookupError as e:
        sys.exit(f"error: {e}")
    pattern = store.load_pattern(proposal["pattern_key"])
    if not pattern:
        sys.exit(f"error: pattern {proposal['pattern_key']!r} not found")
    rep = validate_draft(proposal, files, pattern, allow_no_netns=a.allow_no_netns)
    store.write_json_atomic(d / "validation.json", rep)
    print_report(rep)
    sys.exit(0 if rep["passed"] else 1)


def print_report(rep: dict) -> None:
    print(f"network isolated: {rep['network_isolated']}")
    for t in rep["tests"]:
        print(f"  {'PASS' if t['passed'] else 'FAIL'}  {t['name']}" + ("" if t["passed"] else f"  -- {'; '.join(t['problems'])}"))
    c = rep["coverage"]
    print(f"coverage: {c.get('covered')}/{c.get('successful_sessions')} successful sessions (need {c.get('required')})")
    for p in rep["provenance"]:
        print(f"  provenance {p['provenance']:22} {p['name']}: {'ok' if not p['problems'] else '; '.join(p['problems'])}")
    print("RESULT:", "PASSED" if rep["passed"] else "FAILED")
    for p in rep["problems"]:
        print("  -", p)


if __name__ == "__main__":
    main()
