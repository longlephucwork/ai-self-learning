#!/usr/bin/env python3
"""Phase 3 validation: replay a draft's tests in a sandbox, check test provenance.

  replay.py <draft-name>            re-run tests for generated/<name>/ and update validation.json
  replay.py <draft-name> --allow-no-netns   run even if network isolation is unavailable

Sandbox per test:
  * fresh temp dir holding only the draft's files and the test's input files
  * no network: `unshare -rn` on Linux, `sandbox-exec` on macOS (harness_lib/sandbox.py)
  * `python3 -I`, scrubbed environment (PATH, LANG, HOME=sandbox), no secrets
  * CPU 10s, memory 512MB, max file size 10MB (POSIX), wall-clock timeout 20s
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
from harness_lib import sandbox  # noqa: E402
from harness_lib import tools as rt  # noqa: E402

TIMEOUT_S = 20
MAX_OUTPUT = 4000


# ---------------- sandbox ----------------

def netns_available() -> bool:
    """True when generated code can run without network access on this OS."""
    return sandbox.network_isolated()


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
        try:
            p = sandbox.run(cmd, cwd=str(box), env=sandbox.clean_env(str(box)), stdin=test.get("stdin", ""),
                            timeout=TIMEOUT_S, cpu_s=10, mem_mb=512, fsize_mb=10)
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


def check_provenance(test: dict, members: set[str], cache: dict, *, stdin_is_arguments: bool = False) -> list[str]:
    """stdin_is_arguments: for tools, stdin is the JSON-encoded call arguments, not logged data, so it isn't
    looked up in the logs (the input files and expected outputs still are)."""
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
    if test.get("stdin") and not stdin_is_arguments and _WS.sub("", test["stdin"]) not in hay:
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

def tool_schema(proposal: dict, files: dict[str, str]):
    """The input schema of a tool draft, from tool.json on disk or the raw proposal."""
    try:
        if "tool.json" in files:
            return json.loads(files["tool.json"])["input_schema"]
        return json.loads(proposal.get("tool_input_schema_json") or "null")
    except (json.JSONDecodeError, KeyError):
        return None


def validate_exposure(proposal: dict, files: dict[str, str], *, allow_no_netns=False) -> dict:
    """A tool that wraps an installed script: the argv template must reproduce every validated test of the
    script exactly (so the script's own tests prove the tool's behaviour), and those tests are re-run."""
    from registry import install as reg  # local import: registry imports store only
    netns = netns_available()
    rep = {"validated_at": store.now(), "network_isolated": netns, "tests": [], "provenance": [],
           "coverage": {}, "passed": False, "problems": []}
    P = rep["problems"]
    try:
        spec = json.loads(files["tool.json"])
        schema, template, script = spec["input_schema"], spec["wraps"]["argv"], spec["wraps"]["script"]
    except (KeyError, json.JSONDecodeError) as e:
        P.append(f"tool.json is missing or malformed: {e}")
        return rep
    if not rt.TOOL_NAME_RE.match(proposal["name"]):
        P.append(f"tool name {proposal['name']!r} must match {rt.TOOL_NAME_RE.pattern}")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        P.append("input_schema must be a JSON Schema object with type=object")
        return rep
    P += rt.check_template(template, schema)
    entry = reg.load_registry()["installed"].get(script)
    if not entry or entry["type"] != "script":
        P.append(f"wrapped script {script!r} is not installed")
        return rep
    base = reg.install_root() / entry["path"]
    script_files = {q.relative_to(base).as_posix(): q.read_text(encoding="utf-8")
                    for q in sorted(base.rglob("*")) if q.is_file()}
    script_tests = (store.read_json(reg.resolve(entry["draft"]) / "proposal.json") or {}).get("tests", [])
    if not script_tests:
        P.append(f"no validated tests found for {script!r}; can't prove the mapping")
    examples = {t.get("wraps_test"): t for t in proposal.get("tests", [])}
    mapped = 0
    for st in script_tests:
        ex = examples.get(st["name"])
        probs = []
        if ex is None:
            probs.append("no example arguments given for this test")
        else:
            args = ex.get("arguments")
            probs += rt.check_arguments(args, schema, mode="argv")
            if not probs and not P:
                try:
                    argv = rt.render_argv(template, args)
                except rt.ToolError as e:
                    argv, _ = None, probs.append(str(e))
                if argv is not None and argv != st.get("args", []):
                    probs.append(f"template renders {argv}, but the script test runs {st.get('args')}")
        rep["provenance"].append({"name": st["name"], "provenance": "wraps", "problems": probs})
        P += [f"[{st['name']}] {p}" for p in probs]
        if not probs and not P:
            mapped += 1
    rep["coverage"] = {"successful_sessions": len(script_tests), "covered": mapped, "required": len(script_tests)}
    if not P:
        ok, why = sandbox.may_run(allow_no_netns)
        if not ok:
            P.append(why)
        else:
            for st in script_tests:
                r = run_test(script_files, entry["entrypoint"], st, netns=netns)
                rep["tests"].append(r)
                if not r["passed"]:
                    P.append(f"[{st['name']}] script test failed: {'; '.join(r['problems'])}")
    rep["passed"] = not P
    rep["fingerprint"] = fingerprint(files, proposal.get("tests", []))
    return rep


def validate_draft(proposal: dict, files: dict[str, str], pattern: dict, *, allow_no_netns=False,
                   previous: dict | None = None) -> dict:
    """`previous` is an earlier validation report of this draft: its judged checks are reused when unchanged."""
    if proposal.get("wraps"):
        return validate_exposure(proposal, files, allow_no_netns=allow_no_netns)
    if proposal["type"] == "skill":
        return validate_skill(proposal, files, pattern, allow_no_netns=allow_no_netns, previous=previous)
    netns = netns_available()
    report = {"validated_at": store.now(), "network_isolated": netns, "tests": [], "provenance": [],
              "coverage": {}, "passed": False, "problems": []}
    members = set(pattern["session_ids"]) - set(pattern.get("excluded_injection", []))
    tests = proposal.get("tests", [])
    cache: dict = {}
    for t in tests:
        probs = check_provenance(t, members, cache, stdin_is_arguments=proposal["type"] == "tool")
        if proposal["type"] in ("script", "tool") and t.get("provenance") in ("logged", "derived_from_feedback") \
                and not t.get("expected_files") and t.get("stdout_compare", "ignore") == "ignore" \
                and t.get("expected_exit", 0) == 0:
            probs.append("checks nothing (needs expected_files, stdout_compare or a non-zero expected_exit), "
                         "so it can't count as evidence")
        report["provenance"].append({"name": t["name"], "provenance": t.get("provenance"), "problems": probs})
        if probs:
            report["problems"] += [f"[{t['name']}] {p}" for p in probs]

    successful = {m for m in members if store.outcome(m)["outcome"] in pattern.get("success_outcomes", ["accepted"])}
    covered = set()
    for t in tests:
        if t.get("provenance") in ("logged", "derived_from_feedback"):
            covered |= {m for m in successful if m.startswith(t.get("source_session") or "\0")}
    need = min(1 if pattern.get("quick") else 3, len(successful))  # quick learn: the session asked about
    report["coverage"] = {"successful_sessions": len(successful), "covered": len(covered), "required": need}
    if len(covered) < need:
        report["problems"].append(f"tests cover {len(covered)} successful session(s); need {need}")

    if proposal["type"] == "tool":
        schema = tool_schema(proposal, files)
        if not isinstance(schema, dict) or schema.get("type") != "object":
            report["problems"].append("tool input schema must be a JSON Schema object with type=object")
        else:
            for t in tests:
                if t.get("args"):
                    report["problems"].append(f"[{t['name']}] tools take arguments on stdin; args must be []")
                if t.get("provenance") == "synthetic" and t.get("expected_exit", 0) != 0:
                    continue  # deliberate bad-input test
                try:
                    probs = rt.check_arguments(json.loads(t.get("stdin") or ""), schema, mode="stdin-json")
                except json.JSONDecodeError:
                    probs = ["stdin is not a JSON object of arguments"]
                report["problems"] += [f"[{t['name']}] {p}" for p in probs]
    if proposal["type"] in ("script", "tool"):
        if not tests:
            report["problems"].append("no tests")
        ok, why = sandbox.may_run(allow_no_netns)
        if not ok:
            report["problems"].append(why)
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
    report["passed"] = not report["problems"]
    report["fingerprint"] = fingerprint(files, tests)
    return report


# ---------------- the Claude skill standard (skill-creator's quick_validate.py) ----------------

SKILL_KEYS = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}


def _frontmatter(text: str):
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return None, "no YAML frontmatter at the top of SKILL.md (it must start with ---)"
    try:
        import yaml  # same parser as skill-creator, when available
        fm = yaml.safe_load(m.group(1))
    except ImportError:
        fm = {}
        for line in m.group(1).splitlines():
            if line.strip() and not line.startswith((" ", "\t", "#")) and ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip().strip("\"'")
    except Exception as e:
        return None, f"invalid YAML in the frontmatter: {e}"
    if not isinstance(fm, dict):
        return None, "the frontmatter must be a YAML dictionary"
    return fm, None


def skill_standard_problems(files: dict[str, str], *, expected_name: str | None = None) -> list[str]:
    """The rules of skill-creator's quick_validate.py, plus: the name must match the skill's folder."""
    probs = []
    md = [p for p in files if Path(p).name == "SKILL.md" and not set(Path(p).parts[:-1]) & {"__pycache__", "node_modules"}
          and Path(p).parts[0] != "evals"]
    if "SKILL.md" not in files:
        return ["SKILL.md not found at the skill's top level"]
    if len(md) > 1:
        probs.append(f"a skill must contain exactly one SKILL.md; extra: {', '.join(sorted(p for p in md if p != 'SKILL.md'))}")
    fm, err = _frontmatter(files["SKILL.md"])
    if err:
        return probs + [err]
    extra = set(fm) - SKILL_KEYS
    if extra:
        probs.append(f"unexpected frontmatter key(s): {', '.join(sorted(extra))} (allowed: {', '.join(sorted(SKILL_KEYS))})")
    for key in ("name", "description"):
        if key not in fm:
            probs.append(f"frontmatter is missing '{key}'")
    name, desc = fm.get("name", ""), fm.get("description", "")
    if not isinstance(name, str):
        probs.append("name must be a string")
    elif name.strip():
        n = name.strip()
        if not re.match(r"^[a-z0-9-]+$", n):
            probs.append(f"name {n!r} must be kebab-case (lowercase letters, digits and hyphens)")
        if n.startswith("-") or n.endswith("-") or "--" in n:
            probs.append(f"name {n!r} can't start or end with a hyphen or contain '--'")
        if len(n) > 64:
            probs.append(f"name is {len(n)} characters; the maximum is 64")
        if expected_name and n != expected_name:
            probs.append(f"name {n!r} must match the skill's folder name {expected_name!r}")
    if not isinstance(desc, str):
        probs.append("description must be a string")
    elif desc.strip():
        if "<" in desc or ">" in desc:
            probs.append("description can't contain angle brackets (< or >)")
        if len(desc.strip()) > 1024:
            probs.append(f"description is {len(desc.strip())} characters; the maximum is 1024")
    elif "description" in fm:
        probs.append("description is empty")
    comp = fm.get("compatibility")
    if comp and (not isinstance(comp, str) or len(comp) > 500):
        probs.append("compatibility must be a string of at most 500 characters")
    body_lines = len(files["SKILL.md"].splitlines())
    if body_lines > 500:
        probs.append(f"SKILL.md has {body_lines} lines; keep it under 500 and move detail into references/")
    return probs


def find_skill_creator() -> Path | None:
    """skill-creator's folder, if installed (Claude Code user skills, or synced Anthropic skills)."""
    import glob
    claude = Path(os.environ.get("LEARNING_HARNESS_CLAUDE_HOME") or Path.home() / ".claude")
    pats = [claude / "skills" / p for p in ("skill-creator", "*/skill-creator", "*/*/skill-creator")]
    # Claude Code plugins (e.g. the anthropics/skills marketplace): ~/.claude/plugins/<...>/skills/skill-creator
    pats += [claude / "plugins" / ("*/" * n + "skill-creator") for n in range(1, 6)]
    for pat in pats:
        for d in sorted(glob.glob(str(pat))):
            if (Path(d) / "scripts" / "quick_validate.py").is_file():
                return Path(d)
    return None


def official_skill_check(files: dict[str, str]) -> dict:
    """Run skill-creator's own quick_validate.py on the draft, when skill-creator and PyYAML are available."""
    sc = find_skill_creator()
    if sc is None:
        return {"ran": False, "message": "skill-creator not installed; the same rules were checked by the harness"}
    try:
        import yaml  # noqa: F401  (quick_validate.py needs it)
    except ImportError:
        return {"ran": False, "message": "PyYAML missing; the same rules were checked by the harness"}
    box = Path(tempfile.mkdtemp(prefix="lh-skillcheck-"))
    try:
        name = "skill"
        fm, _ = _frontmatter(files.get("SKILL.md", ""))
        if fm and isinstance(fm.get("name"), str) and re.match(r"^[a-z0-9-]{1,64}$", fm["name"].strip()):
            name = fm["name"].strip()
        root = box / name
        for p, c in files.items():
            dst = root / safe_rel(p)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(c, encoding="utf-8")
        r = subprocess.run([sys.executable, str(sc / "scripts" / "quick_validate.py"), str(root)],
                           capture_output=True, text=True, timeout=60)
        return {"ran": True, "valid": r.returncode == 0, "message": (r.stdout or r.stderr).strip()[:300],
                "script": str(sc / "scripts" / "quick_validate.py")}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ran": False, "message": f"couldn't run skill-creator's checker: {e}"}
    finally:
        shutil.rmtree(box, ignore_errors=True)


def skill_script_for(test: dict, files: dict[str, str]) -> str | None:
    """Which bundled script a skill test runs: its `script` field, or the only .py file."""
    if test.get("script"):
        return test["script"]
    pys = [p for p in files if p.endswith(".py")]
    return pys[0] if len(pys) == 1 else None


def validate_skill(proposal: dict, files: dict[str, str], pattern: dict, *, allow_no_netns=False,
                   previous: dict | None = None) -> dict:
    """Skills: structure, bundled scripts' tests in the sandbox, then a judged check per successful session."""
    from validate import judge
    netns = netns_available()
    rep = {"validated_at": store.now(), "network_isolated": netns, "tests": [], "provenance": [],
           "coverage": {}, "judged": [], "passed": False, "problems": []}
    P = rep["problems"]
    skill = files.get("SKILL.md", "")
    P += skill_standard_problems(files, expected_name=proposal.get("name"))
    rep["official_check"] = official_skill_check(files)
    if rep["official_check"]["ran"] and not rep["official_check"]["valid"]:
        P.append(f"skill-creator's quick_validate.py: {rep['official_check']['message']}")
    from harness_lib.scan import blocking, scan_files   # before any paid judge call
    for f in blocking(scan_files({k: v for k, v in files.items() if not k.startswith("evals/")})):
        P.append(f"safety scan: {f['kind']} in {f['file']}:{f['line']} ({f['detail']})")
    for ref in set(re.findall(r"(?<![\w.-])(scripts/[\w./-]+\.\w+)", skill)):
        if ref not in files:
            P.append(f"SKILL.md references missing file {ref}")
    if re.search(r"\b(?:python3?|bash|sh|node)\s+\.?/?scripts/", skill):
        P.append("SKILL.md runs a bundled script by a bare relative path; Claude runs commands from the "
                 "project folder, so use the skill's base directory (shown when the skill loads)")

    # 1. bundled scripts: provenance-checked like any test, then run in the sandbox; they must pass.
    members = set(pattern["session_ids"]) - set(pattern.get("excluded_injection", []))
    tests, cache = proposal.get("tests", []), {}
    for t in tests:
        probs = check_provenance(t, members, cache)
        rep["provenance"].append({"name": t["name"], "provenance": t.get("provenance"), "problems": probs})
        P += [f"[{t['name']}] {p}" for p in probs]
    has_scripts = any(p.endswith(".py") for p in files if not p.startswith("evals/"))
    if has_scripts and not tests and proposal.get("created_by") != "skill-creator":
        P.append("the skill bundles scripts but has no tests for them")
    if proposal.get("created_by") == "skill-creator":
        rep["note"] = "built with skill-creator: its own evals/ test the skill; the harness adds the checks below"
    if tests:
        ok, why = sandbox.may_run(allow_no_netns)
        if not ok:
            P.append(why)
        else:
            for t in tests:
                script = skill_script_for(t, files)
                if script not in files:
                    P.append(f"[{t['name']}] names no bundled script to run (set `script`)")
                    continue
                r = run_test(files, script, t, netns=netns)
                rep["tests"].append(r)
                if not r["passed"]:
                    P.append(f"[{t['name']}] failed: {'; '.join(r['problems'])}")

    # 2. evidence: a judged check against each successful session (only once everything cheaper passed).
    successful = sorted(m for m in members
                        if store.outcome(m)["outcome"] in pattern.get("success_outcomes", ["accepted"]))
    need = min(1 if pattern.get("quick") else 3, len(successful))  # quick learn: the session asked about
    if not P:
        judged = judge.judge_skill(files, successful, previous=(previous or {}).get("judged"))
        rep["judged"] = judged
        passed = [j for j in judged if j["verdict"] == "pass"]
        for j in judged:
            if j["verdict"] != "pass":
                P.append(f"[judged {j['session_id'][:8]}] {j['verdict']}: {j['reason'][:300]}")
        rep["coverage"] = {"successful_sessions": len(successful), "covered": len(passed), "required": need,
                           "method": "judged"}
        if len(passed) < need:
            P.append(f"judged checks passed for {len(passed)} successful session(s); need {need}")
    else:
        rep["coverage"] = {"successful_sessions": len(successful), "covered": 0, "required": need,
                           "method": "judged (skipped: fix the problems above first)"}
    rep["passed"] = not P
    rep["fingerprint"] = fingerprint(files, tests)
    return rep


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


def drafts_dir(create: bool = True) -> Path:
    p = Path(os.environ.get("LEARNING_HARNESS_GENERATED", store.learned_dir() / "generated"))
    if create:
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
    rep = validate_draft(proposal, files, pattern, allow_no_netns=a.allow_no_netns,
                         previous=store.read_json(d / "validation.json"))
    store.write_json_atomic(d / "validation.json", rep)
    print_report(rep)
    sys.exit(0 if rep["passed"] else 1)


def print_report(rep: dict) -> None:
    print(f"network isolated: {rep['network_isolated']}")
    for t in rep["tests"]:
        print(f"  {'PASS' if t['passed'] else 'FAIL'}  {t['name']}" + ("" if t["passed"] else f"  -- {'; '.join(t['problems'])}"))
    for j in rep.get("judged", []):
        tag = " (cached)" if j.get("cached") else ""
        print(f"  {'PASS' if j['verdict'] == 'pass' else j['verdict'].upper():5} judged vs session {j['session_id'][:8]}{tag}: "
              f"{j['reason'][:110]}")
    c = rep["coverage"]
    how = f" [{c['method']}]" if c.get("method") else ""
    print(f"coverage: {c.get('covered')}/{c.get('successful_sessions')} successful sessions (need {c.get('required')}){how}")
    for p in rep["provenance"]:
        print(f"  provenance {p['provenance']:22} {p['name']}: {'ok' if not p['problems'] else '; '.join(p['problems'])}")
    print("RESULT:", "PASSED" if rep["passed"] else "FAILED")
    for p in rep["problems"]:
        print("  -", p)


if __name__ == "__main__":
    main()
