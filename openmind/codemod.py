"""Test-gated code modification — the ONE place Open Mind writes code.

WHY THIS IS GATED (design intent)
---------------------------------
Open Mind is a read-only knowledge layer everywhere else. Code modification is
the single exception, and it is deliberately the most constrained capability in
the system, for one reason: a LOCAL model's edit is a *probabilistic* artifact,
and we only let probabilistic artifacts through when a DETERMINISTIC check
confirms them. That check is the project's own test suite.

So every edit is:
  * SMALL and EXPLICIT — a literal find/replace in one file, never a free-form
    "rewrite this" the model improvises.
  * HUMAN-IN-THE-LOOP — :func:`propose` previews a unified diff and writes
    nothing; a human reviews it before :func:`apply_fix` is ever called.
  * RED-GREEN GATED — apply runs the tests FIRST (the baseline must be green, or
    we refuse: a failing baseline makes the green/red signal meaningless),
    applies the edit, runs the tests AGAIN, and KEEPS the change only if they are
    still green. On red, the file is reverted byte-for-byte. The model never gets
    to "decide" the edit is correct — the tests do.

This is the project's thesis in one module: turn an emergent, unreliable model
capability into deterministic, reliable behaviour by collapsing the judgement
("is this edit correct?") onto something that cannot hand-wave — a passing test.
"""
from __future__ import annotations

import difflib
import os
import subprocess
from typing import Any, Dict, List, Optional, Union

# A hard ceiling so a hung or pathological test command can never wedge the tool.
DEFAULT_TEST_TIMEOUT = 900.0


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _unified_diff(old: str, new: str, path: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}"))


def run_tests(test_cmd: Union[str, List[str]], cwd: Optional[str] = None,
              timeout: float = DEFAULT_TEST_TIMEOUT) -> Dict[str, Any]:
    """Run the project's test command and report green/red deterministically.

    `test_cmd` is a list (run directly) or a string (run via the shell). Green is
    defined solely by exit code 0 — we trust the project's own definition of
    passing, never the model's opinion of the output.
    """
    shell = isinstance(test_cmd, str)
    try:
        proc = subprocess.run(test_cmd, cwd=cwd, shell=shell, timeout=timeout,
                              capture_output=True, text=True)
    except subprocess.TimeoutExpired as exc:
        return {"green": False, "code": None, "timed_out": True,
                "output": (exc.output or "") + f"\n[timed out after {timeout}s]"}
    out = (proc.stdout or "") + (proc.stderr or "")
    return {"green": proc.returncode == 0, "code": proc.returncode,
            "timed_out": False, "output": out[-8000:]}


def propose(file_path: str, find: str, replace: str) -> Dict[str, Any]:
    """Preview a literal find/replace as a unified diff. Writes NOTHING.

    This is the human-in-the-loop step: a person reads the diff and decides
    whether to call :func:`apply_fix`.
    """
    if not os.path.isfile(file_path):
        return {"ok": False, "status": "no-file", "message": f"not a file: {file_path}"}
    if not find:
        return {"ok": False, "status": "empty-find", "message": "find text is empty"}
    old = _read(file_path)
    occurrences = old.count(find)
    if occurrences == 0:
        return {"ok": False, "status": "no-match", "occurrences": 0,
                "message": "find text not present; nothing to change"}
    new = old.replace(find, replace)
    return {"ok": True, "status": "preview", "file": file_path,
            "occurrences": occurrences, "diff": _unified_diff(old, new, file_path)}


def apply_fix(file_path: str, find: str, replace: str,
              test_cmd: Union[str, List[str]], cwd: Optional[str] = None,
              require_green_baseline: bool = True,
              timeout: float = DEFAULT_TEST_TIMEOUT) -> Dict[str, Any]:
    """Apply a literal find/replace ONLY if the test suite stays green.

    Sequence: (optionally) require a green baseline -> apply -> re-run tests ->
    keep on green, REVERT on red. Returns a structured verdict including both test
    runs so the outcome is auditable. The file is never left in a red state by
    this function.
    """
    preview = propose(file_path, find, replace)
    if not preview.get("ok"):
        return preview
    cwd = cwd or os.path.dirname(os.path.abspath(file_path))
    original = _read(file_path)

    # 1) BASELINE — a red baseline makes the post-edit signal meaningless.
    baseline = None
    if require_green_baseline:
        baseline = run_tests(test_cmd, cwd=cwd, timeout=timeout)
        if not baseline["green"]:
            return {"ok": False, "status": "baseline-red", "applied": False,
                    "diff": preview["diff"], "baseline": baseline,
                    "message": "tests already failing before the edit — refusing "
                               "to gate on a red baseline; fix the baseline first"}

    # 2) APPLY, then 3) VERIFY.
    _write(file_path, original.replace(find, replace))
    after = run_tests(test_cmd, cwd=cwd, timeout=timeout)
    if after["green"]:
        return {"ok": True, "status": "applied-green", "applied": True,
                "file": file_path, "occurrences": preview["occurrences"],
                "diff": preview["diff"], "baseline": baseline, "after": after}

    # 4) RED -> revert byte-for-byte; the model's edit does not get the benefit
    # of the doubt.
    _write(file_path, original)
    return {"ok": False, "status": "reverted-red", "applied": False,
            "file": file_path, "diff": preview["diff"], "baseline": baseline,
            "after": after,
            "message": "edit made the tests fail; reverted. The change was not kept."}
