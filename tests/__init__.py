"""Test package.

Safety net: before any test runs, every harness location (store, learned/, patterns, drafts) points at a
throwaway folder, so no test can ever read or write the real data of the repo it runs in. This matters
because the installer runs the self-test on the user's own machine. Tests that need a specific location
still set their own; tests that check the defaults remove these variables themselves.
"""
import atexit
import os
import shutil
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="lh-tests-")
atexit.register(shutil.rmtree, _SANDBOX, True)
for _var, _sub in (("LEARNING_HARNESS_STORE", "store"), ("LEARNING_HARNESS_INSTALL_ROOT", "learned"),
                   ("LEARNING_HARNESS_PATTERNS", "learned/patterns"), ("LEARNING_HARNESS_GENERATED", "learned/generated")):
    os.environ.setdefault(_var, os.path.join(_SANDBOX, _sub))
