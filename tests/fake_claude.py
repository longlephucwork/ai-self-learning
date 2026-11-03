#!/usr/bin/env python3
"""Stand-in for `claude -p` in tests. Logs how it was called and returns canned structured output."""
import json, os, re, sys

args = sys.argv[1:]
prompt = sys.stdin.read()
schema = json.loads(args[args.index("--json-schema") + 1])
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({"args": args, "cwd": os.getcwd(),
                             "disable": os.environ.get("LEARNING_HARNESS_DISABLE")}) + "\n")

if "confidence" in schema["properties"]:  # refine/auto.py inferred ratings
    out = {"outcome": "accepted", "confidence": float(os.environ.get("FAKE_RATE_CONF", "0.7")), "note": "",
           "reason": "fake rater"}
    for needle, (o, conf, note) in json.loads(os.environ.get("FAKE_RATE_RULES", "{}")).items():
        if needle in prompt:
            out.update(outcome=o, confidence=conf, note=note)
elif "verdict" in schema["properties"]:  # validate/judge.py
    out = {"verdict": os.environ.get("FAKE_JUDGE_VERDICT", "pass"),
           "checks": [{"rule": "follow the user's note", "consistent": True, "evidence": "fake"}],
           "reason": "fake judge"}
elif "argv_template_json" in schema["properties"]:  # refine/expose.py
    key = "FAKE_EXPOSE_FILE_2" if "previous attempt failed" in prompt and os.environ.get("FAKE_EXPOSE_FILE_2") \
        else "FAKE_EXPOSE_FILE"
    out = json.load(open(os.environ[key]))
elif "tests" in schema["properties"]:  # Phase 3 proposal: return a canned file chosen by the test
    key = "FAKE_PROPOSAL_FILE_2" if "previous attempt FAILED" in prompt and os.environ.get("FAKE_PROPOSAL_FILE_2") \
        else "FAKE_PROPOSAL_FILE"
    out = json.load(open(os.environ[key]))
elif "clusters" in schema["properties"]:
    m = re.search(r"<<<DATA-(\w+)\n(.*)\nDATA-\1>>>", prompt, re.S)
    items = json.loads(m.group(2))
    drop = os.environ.get("FAKE_DROP_ID", "")
    groups = {}
    for it in items:
        if drop and it["id"].startswith(drop):
            continue  # simulate the model forgetting a session
        groups.setdefault(it["task_family"], []).append(it["id"])
    out = {"clusters": [{"key": fam, "label": fam, "description": f"{fam} tasks", "session_ids": ids,
                         "suggested_type": "tool", "type_rationale": "fake says tool"}
                        for fam, ids in groups.items()]}
else:
    req = prompt.split("## USER REQUESTS", 1)[1].split("## TOOL STEPS", 1)[0].lower()
    csv = "csv" in req
    out = {"intent": "Convert a CSV file to JSON" if csv else "Answer a question",
           "task_family": "CSV to JSON conversion" if csv else "general-question",
           "inputs": ["a CSV file"] if csv else [], "outputs": ["a JSON file"] if csv else ["an answer"],
           "steps": ["read", "convert", "write"], "deterministic": csv,
           "deterministic_reason": "fixed mapping" if csv else "needs judgment",
           "tools_used": ["Read", "Write"],
           "injection_suspected": "ignore previous instructions" in prompt.lower(),
           "injection_note": ""}
print(json.dumps({"type": "result", "is_error": False, "structured_output": out, "total_cost_usd": 0.001}))
