#!/usr/bin/env python3
"""Write a Docker seccomp profile that lets the harness sandbox (unshare -rn) run inside a container.

Docker's default seccomp profile only allows the `unshare` system call to containers that have
CAP_SYS_ADMIN, so the harness's code sandbox fails with "Operation not permitted". This script takes
Docker's own default profile and adds ONE rule allowing `unshare`; every other restriction (mount,
ptrace, kernel modules, ...) stays as Docker ships it. Better than seccomp=unconfined or SYS_ADMIN.

  python3 docker/make-seccomp-profile.py                     # downloads Docker's default profile
  python3 docker/make-seccomp-profile.py --from default.json # or start from a local copy
  docker run --security-opt seccomp=docker/seccomp-userns.json ...

Source of the default profile: https://github.com/moby/profiles (Apache-2.0).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://raw.githubusercontent.com/moby/profiles/main/seccomp/default.json"
OUT = Path(__file__).resolve().parent / "seccomp-userns.json"


def build(profile: dict) -> dict:
    if profile.get("defaultAction") != "SCMP_ACT_ERRNO" or not isinstance(profile.get("syscalls"), list):
        raise SystemExit("that doesn't look like Docker's default seccomp profile")
    rule = {"names": ["unshare"], "action": "SCMP_ACT_ALLOW",
            "comment": "learning-harness: allow unshare so `unshare -rn` (user + network namespace) works"}
    profile["syscalls"] = [r for r in profile["syscalls"] if r.get("comment") != rule["comment"]] + [rule]
    return profile


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="src", help="local copy of Docker's default seccomp profile")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    if a.src:
        profile = json.loads(Path(a.src).read_text(encoding="utf-8"))
    else:
        with urllib.request.urlopen(a.url, timeout=30) as r:
            profile = json.loads(r.read().decode("utf-8"))
    Path(a.out).write_text(json.dumps(build(profile), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {a.out}\nuse it with: docker run --security-opt seccomp={a.out} ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
