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

if "clusters" in schema["properties"]:
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
