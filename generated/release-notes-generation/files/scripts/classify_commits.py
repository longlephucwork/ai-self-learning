#!/usr/bin/env python3
"""Bucket git log --oneline lines into release-note categories.

Input: a file (or '-' for stdin) with one commit per line, optionally
starting with an abbreviated hash. Output: JSON on stdout.
"""
import argparse
import json
import re
import sys

BUCKETS = ["security", "features", "improvements", "fixes", "docs", "internal", "other"]

PREFIX = re.compile(r"^([A-Za-z]+)(?:\(([^)]*)\))?(!)?:\s*(.*)$")
HASH = re.compile(r"^[0-9a-f]{7,40}$")
INTERNAL_WORDS = re.compile(
    r"\b(ci|internal|refactor\w*|rename[sd]?|bump\w*|dependenc\w+|lint\w*|tests?)\b", re.I)
SEC_WORDS = re.compile(r"\b(security|cve-\d+|vulnerabilit\w+)\b", re.I)

TYPE_MAP = {
    "feat": "features", "feature": "features",
    "fix": "fixes", "bugfix": "fixes", "hotfix": "fixes",
    "perf": "improvements",
    "docs": "docs", "doc": "docs",
    "security": "security", "sec": "security",
    "chore": "internal", "ci": "internal", "test": "internal", "tests": "internal",
    "refactor": "internal", "build": "internal", "style": "internal",
    "internal": "internal", "deps": "internal",
}


def classify(subject):
    m = PREFIX.match(subject)
    if m and m.group(1).lower() in TYPE_MAP:
        typ = m.group(1).lower()
        scope = m.group(2) or ""
        message = m.group(4).strip()
        bucket = TYPE_MAP[typ]
        if scope.lower() in ("security", "sec"):
            bucket = "security"
        return bucket, scope, message
    if SEC_WORDS.search(subject):
        return "security", "", subject
    if INTERNAL_WORDS.search(subject):
        return "internal", "", subject
    first = subject.split(None, 1)[0].lower() if subject.split() else ""
    if first in ("add", "adds", "implement", "introduce", "support"):
        return "features", "", subject
    if first in ("fix", "fixes", "fixed", "resolve"):
        return "fixes", "", subject
    if first in ("speed", "improve", "optimize", "optimise", "faster", "reduce"):
        return "improvements", "", subject
    return "other", "", subject


def main():
    ap = argparse.ArgumentParser(
        description="Classify git log --oneline lines into release-note buckets "
                    "(security, features, improvements, fixes, docs, internal, other) and print JSON.")
    ap.add_argument("input", help="file with one commit per line, or '-' for stdin")
    args = ap.parse_args()
    try:
        if args.input == "-":
            text = sys.stdin.read()
        else:
            with open(args.input, encoding="utf-8") as f:
                text = f.read()
    except OSError as e:
        print("error: cannot read input: %s" % e, file=sys.stderr)
        return 2
    out = {b: [] for b in BUCKETS}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        h = ""
        subject = line
        if len(parts) == 2 and HASH.match(parts[0]):
            h, subject = parts[0], parts[1].strip()
        bucket, scope, message = classify(subject)
        out[bucket].append({"hash": h, "scope": scope, "message": message})
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
