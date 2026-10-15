"""GOAL.md acceptance record: parsing, checks, judging, pre_verify hook.

Documented format (a goal file is a plain markdown file next to the work):

    # Goal: <title>

    ## Requirement
    <free text the semantic gate judges evidence against>

    ## Checks
    - run: `python -m pytest -q` exits 0
    - file: CHANGELOG.md exists
    - file: CHANGELOG.md contains "v1.2"
    - file: build.log not contains "ERROR"

    ## Limits
    - max_continuations: 3
    - timeout: 600

`run:` expects the command in backticks, optionally followed by "exits N"
(default exit code 0). File paths are relative to the working directory and
must not escape it.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_RUN_BACKTICK_EXIT = re.compile(r"^`(.*)`\s+exits\s+(\d+)$")
_RUN_BACKTICK = re.compile(r"^`(.*)`$")
_FILE_EXISTS = re.compile(r"^(\S+)\s+exists$")
_FILE_CONTAINS = re.compile(r'^(\S+)\s+contains\s+"(.*)"$')
_FILE_NOT_CONTAINS = re.compile(r'^(\S+)\s+not\s+contains\s+"(.*)"$')
_MAX_CONTINUATIONS = re.compile(r"max_continuations:\s*(\d+)$")
_TIMEOUT = re.compile(r"timeout:\s*(\d+)$")
_DEFAULT_TIMEOUT = 600


@dataclass
class Check:
    kind: str  # run | file_exists | file_contains | file_not_contains
    arg: str
    expect: str | int | None
    raw: str


@dataclass
class Goal:
    title: str
    requirement: str
    checks: list[Check] = field(default_factory=list)
    max_continuations: int | None = None
    timeout: int = _DEFAULT_TIMEOUT
    path: str | None = None


@dataclass
class CheckResult:
    check: Check
    ok: bool
    detail: str


@dataclass
class Verdict:
    met: bool
    code_ok: bool
    semantic_ok: bool | None
    semantic_prob: float | None
    results: list[CheckResult] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def _parse_check(line: str) -> Check:
    m = re.match(r"^(run|file):\s*(.*)$", line)
    if not m:
        raise ValueError(f"unparseable check line: {line}")
    head, rest = m.group(1), m.group(2).strip()
    if head == "run":
        m = _RUN_BACKTICK_EXIT.match(rest)
        if m:
            return Check("run", m.group(1), int(m.group(2)), line)
        m = _RUN_BACKTICK.match(rest)
        if m:
            return Check("run", m.group(1), 0, line)
        return Check("run", rest, 0, line)
    m = _FILE_EXISTS.match(rest)
    if m:
        return Check("file_exists", m.group(1), None, line)
    m = _FILE_CONTAINS.match(rest)
    if m:
        return Check("file_contains", m.group(1), m.group(2), line)
    m = _FILE_NOT_CONTAINS.match(rest)
    if m:
        return Check("file_not_contains", m.group(1), m.group(2), line)
    raise ValueError(f"unparseable check line: {line}")


def parse_goal(text: str, path: str | None = None) -> Goal:
    title = ""
    requirement: list[str] = []
    checks: list[Check] = []
    max_continuations: int | None = None
    timeout = _DEFAULT_TIMEOUT
    section = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("# Goal:"):
            title = stripped[len("# Goal:"):].strip()
            section = None
            continue
        if stripped.startswith("## "):
            section = stripped[3:].strip().lower()
            continue
        if not stripped:
            continue
        if section == "requirement":
            requirement.append(stripped)
        elif section == "checks":
            if stripped.startswith("- "):
                checks.append(_parse_check(stripped[2:].strip()))
        elif section == "limits":
            if stripped.startswith("- "):
                body = stripped[2:].strip()
                m = _MAX_CONTINUATIONS.match(body)
                if m:
                    max_continuations = int(m.group(1))
                    continue
                m = _TIMEOUT.match(body)
                if m:
                    timeout = int(m.group(1))
    return Goal(title=title, requirement="\n".join(requirement), checks=checks,
                max_continuations=max_continuations, timeout=timeout, path=path)


def load_goal(path) -> Goal:
    path = str(path)
    with open(path, encoding="utf-8") as fh:
        return parse_goal(fh.read(), path=path)


def _safe_path(path: str, cwd: str) -> str | None:
    if os.path.isabs(path):
        resolved = os.path.realpath(path)
    else:
        resolved = os.path.realpath(os.path.join(cwd, path))
    cwd_real = os.path.realpath(cwd)
    if resolved == cwd_real or resolved.startswith(cwd_real + os.sep):
        return resolved
    return None


def _run_check(check: Check, *, cwd: str, timeout: int) -> CheckResult:
    if check.kind == "run":
        try:
            argv = shlex.split(check.arg)
        except ValueError as exc:
            return CheckResult(check, False, f"unparseable command: {exc}")
        if not argv:
            return CheckResult(check, False, "empty command")
        try:
            proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return CheckResult(check, False, f"timed out after {timeout}s")
        except OSError as exc:
            return CheckResult(check, False, f"failed to run: {exc}")
        output = (proc.stdout or "") + (proc.stderr or "")
        expected = check.expect if isinstance(check.expect, int) else 0
        detail = f"exit {proc.returncode}: {output[-1500:]}".strip()
        return CheckResult(check, proc.returncode == expected, detail)
    safe = _safe_path(check.arg, cwd)
    if safe is None:
        return CheckResult(check, False, f"path escapes cwd: {check.arg}")
    if check.kind == "file_exists":
        ok = os.path.exists(safe)
        return CheckResult(check, ok, f"exists={ok}")
    if not os.path.isfile(safe):
        if check.kind == "file_not_contains":
            return CheckResult(check, True, "file missing, so it cannot contain the text")
        return CheckResult(check, False, f"file missing: {check.arg}")
    try:
        with open(safe, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        return CheckResult(check, False, f"unreadable: {exc}")
    needle = check.expect or ""
    if check.kind == "file_contains":
        ok = needle in content
        return CheckResult(check, ok, f"{'found' if ok else 'missing'} {needle!r}")
    if check.kind == "file_not_contains":
        ok = needle not in content
        return CheckResult(check, ok, f"{'absent' if ok else 'present'} {needle!r}")
    return CheckResult(check, False, f"unknown check kind: {check.kind}")


def run_checks(goal: Goal, *, cwd: str, timeout: int | None = None) -> list[CheckResult]:
    limit = timeout or goal.timeout or _DEFAULT_TIMEOUT
    return [_run_check(check, cwd=cwd, timeout=limit) for check in goal.checks]


def judge(goal: Goal, results: list[CheckResult], *, chain=None, extra_evidence: str = "",
          threshold: float = 0.7) -> Verdict:
    """code checks first; the semantic question runs only when they all pass."""
    code_ok = all(r.ok for r in results)
    reasons = [f"{r.check.raw}: {r.detail}" for r in results if not r.ok]
    semantic_ok: bool | None = None
    semantic_prob: float | None = None
    if code_ok and chain is not None:
        state = {"requirement": goal.requirement,
                 "checks": [r.detail for r in results],
                 "evidence": (extra_evidence or "")[:4000]}
        try:
            from hermes_loopguard.backends import ask_with_fallback
            from hermes_loopguard.questions import Noul
            questions = {"met": Noul(
                instructions="Is the requirement met given this evidence?",
                criteria={"true": "the evidence shows the requirement is satisfied",
                          "false": "the evidence does not show it is satisfied"})}
            result = ask_with_fallback(chain, state, questions)
            answer = result.answers.get("met")
            if answer is None or getattr(result, "error", None):
                semantic_ok = False
                reasons.append("semantic check failed")
            else:
                semantic_prob = answer.noul
                semantic_ok = semantic_prob >= threshold
                if not semantic_ok:
                    reasons.append(f"semantic check says requirement not met "
                                   f"(p={semantic_prob:.2f} < {threshold})")
        except Exception as exc:
            semantic_ok = False
            reasons.append(f"semantic check failed: {exc}")
    met = code_ok and semantic_ok is not False
    return Verdict(met=met, code_ok=code_ok, semantic_ok=semantic_ok,
                   semantic_prob=semantic_prob, results=results, reasons=reasons)


def check_goal(path, *, cwd: str | None = None, chain=None, extra_evidence: str = "",
               threshold: float = 0.7) -> Verdict:
    cwd = cwd or os.getcwd()
    goal = load_goal(path)
    results = run_checks(goal, cwd=cwd)
    return judge(goal, results, chain=chain, extra_evidence=extra_evidence, threshold=threshold)


def make_pre_verify_hook(get_cfg, *, store_factory, chain_factory=None, cwd_fn=os.getcwd):
    """pre_verify hook with a continuation cap; never raises."""

    def hook(session_id="", final_response=None, attempt=0, **kwargs):
        try:
            cfg = (get_cfg() or {}).get("completion") or {}
            goal_path = os.path.join(cwd_fn(), cfg.get("goal_file", "GOAL.md"))
            if not os.path.isfile(goal_path):
                return None
            goal = load_goal(goal_path)
            cap = goal.max_continuations or cfg.get("max_continuations", 3)
            key = f"completion:{session_id}"
            store = store_factory()
            if store.get_counter(key) >= cap:
                store.log_decision(watcher="completion", kind="completion", outcome="cap_reached",
                                   note=f"{key} at {store.get_counter(key)}/{cap}")
                return None
            chain = chain_factory() if chain_factory is not None else None
            verdict = check_goal(goal_path, cwd=cwd_fn(), chain=chain,
                                 extra_evidence=str(final_response or ""))
            if verdict.met:
                store.reset_counter(key)
                store.log_decision(watcher="completion", kind="completion", outcome="met",
                                   note=goal_path)
                return None
            store.bump_counter(key)
            count = store.get_counter(key)
            reasons = "; ".join(verdict.reasons) or "checks failed"
            store.log_decision(watcher="completion", kind="completion", outcome="continue",
                               note=f"{count}/{cap}: {reasons[:300]}")
            return {"action": "continue",
                    "message": (f"LoopGuard: GOAL.md is not met yet ({count}/{cap}): {reasons}. "
                                "Fix this, re-run the checks, then finish.")}
        except Exception:
            log.debug("pre_verify hook failed", exc_info=True)
            return None

    return hook
