#!/usr/bin/env python3
"""Classify `git log --oneline` lines into release-note categories.

Input: lines of '<hash> <subject>' from a file, or '-' for stdin.
Output: JSON list of {hash, subject, category, message}.
Categories: security, feature, improvement, fix, docs, dependency, internal, unknown.
"""
import argparse
import json
import re
import sys

HASH_RE = re.compile(r"^([0-9a-f]{7,40})\s+(.*)$")
CONV_RE = re.compile(r"^(\w+)(?:\(([^)]*)\))?(!)?:\s*(.*)$")
INTERNAL_TYPES = {"refactor", "chore", "ci", "test", "tests", "build", "style", "internal"}
INTERNAL_WORDS = re.compile(r"\b(ci|rename|renamed|refactor|bump|lint|typo)\b", re.I)
FEATURE_WORDS = {"add", "adds", "added", "new", "support", "introduce"}
FIX_WORDS = {"fix", "fixed", "fixes"}
IMPROVE_WORDS = {"speed", "improve", "optimize", "optimise", "faster", "reduce"}
SECURITY_RE = re.compile(r"\b(security|cve-\d+)\b", re.I)


def classify(subject):
    m = CONV_RE.match(subject)
    if m:
        typ, scope, _bang, msg = m.group(1).lower(), (m.group(2) or "").lower(), m.group(3), m.group(4)
        if typ in ("fix", "feat", "perf", "feature") and SECURITY_RE.search(scope + " " + msg) and typ == "fix":
            return "security", msg
        if typ == "fix":
            return "fix", msg
        if typ in ("feat", "feature"):
            return "feature", msg
        if typ == "perf":
            return "improvement", msg
        if typ == "docs":
            return "docs", msg
        if typ == "deps" or (typ in ("chore", "build") and scope in ("deps", "dependencies")):
            return "dependency", msg
        if typ in INTERNAL_TYPES:
            return "internal", msg
        return "unknown", msg
    words = subject.split()
    first = words[0].lower().rstrip(":") if words else ""
    if SECURITY_RE.search(subject) and first in FIX_WORDS:
        return "security", subject
    if INTERNAL_WORDS.search(subject):
        return "internal", subject
    if first in FEATURE_WORDS:
        return "feature", subject
    if first in FIX_WORDS:
        return "fix", subject
    if first in IMPROVE_WORDS:
        return "improvement", subject
    return "unknown", subject


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="file with `git log --oneline` output, or '-' for stdin")
    args = p.parse_args()
    try:
        if args.input == "-":
            text = sys.stdin.read()
        else:
            with open(args.input, encoding="utf-8") as f:
                text = f.read()
    except OSError as e:
        print("error: cannot read input: %s" % e, file=sys.stderr)
        return 2
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = HASH_RE.match(line)
        h, subject = (m.group(1), m.group(2).strip()) if m else ("", line)
        cat, msg = classify(subject)
        out.append({"hash": h, "subject": subject, "category": cat, "message": msg})
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
