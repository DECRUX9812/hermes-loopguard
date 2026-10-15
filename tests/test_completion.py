"""Tests for GOAL.md parsing, checks, judging and the pre_verify hook."""
import pytest

from hermes_loopguard import completion

EXAMPLES = None


def examples_dir():
    global EXAMPLES
    if EXAMPLES is None:
        from pathlib import Path
        EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
    return EXAMPLES


class FakeChain:  # FAKE BACKEND (unit test only)
    def __init__(self, noul=0.95, error=None):
        self.noul = noul
        self.calls = 0
        self.error = error

    def available(self):
        return True

    def ask(self, state, questions):
        self.calls += 1
        from hermes_loopguard.backends.base import GateResult, Usage
        from hermes_loopguard.questions import NoulAnswer
        answers = {name: NoulAnswer(noul=self.noul) for name in questions}
        return GateResult(answers=answers, backend="fake", model="fake-model",
                          usage=Usage(), error=self.error)


class FakeStore:  # minimal Store stand-in
    def __init__(self):
        self.counters = {}
        self.decisions = []

    def get_counter(self, key):
        return self.counters.get(key, 0)

    def bump_counter(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def reset_counter(self, key):
        self.counters[key] = 0

    def log_decision(self, **kw):
        self.decisions.append(kw)


# parsing
def test_parse_example_goal():
    goal = completion.load_goal(examples_dir() / "GOAL.md")
    assert goal.title
    assert goal.requirement
    assert goal.max_continuations == 3
    assert goal.timeout == 600
    kinds = [c.kind for c in goal.checks]
    assert kinds == ["run", "file_exists", "file_contains", "file_not_contains"]
    run_check = goal.checks[0]
    assert run_check.arg == "python3 -m pytest -q tests/test_auth.py"
    assert run_check.expect == 0


def test_parse_bad_check_line_names_the_line():
    text = "# Goal: t\n\n## Checks\n- frobnicate: nightly\n"
    with pytest.raises(ValueError, match="frobnicate: nightly"):
        completion.parse_goal(text)


def test_parse_limits_optional():
    goal = completion.parse_goal("# Goal: t\n\n## Checks\n- file: a.txt exists\n")
    assert goal.checks[0].kind == "file_exists"
    assert goal.max_continuations is None


# running checks
def test_run_checks_in_tmp_path(tmp_path):
    goal = completion.parse_goal(
        "# Goal: t\n\n## Checks\n"
        "- run: `python3 -c \"import sys; sys.exit(0)\"` exits 0\n"
        "- run: `python3 -c \"import sys; sys.exit(3)\"` exits 0\n"
        "- run: `python3 -c \"import sys; sys.exit(2)\"` exits 2\n")
    results = completion.run_checks(goal, cwd=str(tmp_path))
    assert results[0].ok
    assert not results[1].ok and "exit 3" in results[1].detail
    assert results[2].ok and "exit 2" in results[2].detail


def test_run_check_timeout_is_not_ok(tmp_path):
    goal = completion.parse_goal(
        '# Goal: t\n\n## Checks\n- run: `python3 -c "import time; time.sleep(5)"` exits 0\n')
    results = completion.run_checks(goal, cwd=str(tmp_path), timeout=1)
    assert not results[0].ok and "timed out" in results[0].detail


def test_file_checks(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text("v1.2 notes\n")
    (tmp_path / "build.log").write_text("all good\n")
    goal = completion.parse_goal(
        '# Goal: t\n\n## Checks\n'
        '- file: CHANGELOG.md exists\n'
        '- file: CHANGELOG.md contains "v1.2"\n'
        '- file: build.log not contains "ERROR"\n')
    results = completion.run_checks(goal, cwd=str(tmp_path))
    assert all(r.ok for r in results)
    (tmp_path / "build.log").write_text("ERROR boom\n")
    results = completion.run_checks(goal, cwd=str(tmp_path))
    assert results[2].ok is False and "present" in results[2].detail


def test_path_escape_rejected(tmp_path):
    goal = completion.parse_goal("# Goal: t\n\n## Checks\n- file: ../secret.txt exists\n")
    results = completion.run_checks(goal, cwd=str(tmp_path))
    assert not results[0].ok and "escape" in results[0].detail


def test_absolute_path_outside_cwd_rejected(tmp_path):
    goal = completion.parse_goal('# Goal: t\n\n## Checks\n- file: /etc/passwd exists\n')
    results = completion.run_checks(goal, cwd=str(tmp_path))
    assert not results[0].ok and "escape" in results[0].detail


def test_missing_file_fails_contains_but_passes_not_contains(tmp_path):
    goal = completion.parse_goal(
        '# Goal: t\n\n## Checks\n'
        '- file: missing.txt contains "x"\n'
        '- file: missing.txt not contains "x"\n')
    results = completion.run_checks(goal, cwd=str(tmp_path))
    assert not results[0].ok
    assert results[1].ok


# judging
def test_judge_failing_code_does_not_call_chain(tmp_path):
    chain = FakeChain()
    goal = completion.parse_goal(
        '# Goal: t\n\n## Checks\n- run: `python3 -c "import sys; sys.exit(1)"`\n')
    results = completion.run_checks(goal, cwd=str(tmp_path))
    verdict = completion.judge(goal, results, chain=[chain])
    assert chain.calls == 0
    assert not verdict.met and not verdict.code_ok
    assert verdict.semantic_ok is None
    assert verdict.reasons


def test_judge_passing_code_calls_chain_once(tmp_path):
    pytest.importorskip("hermes_loopguard.backends")
    chain = FakeChain(noul=0.95)
    goal = completion.parse_goal(
        '# Goal: t\n\n## Requirement\nWrite the file.\n\n## Checks\n- file: ok.txt exists\n')
    (tmp_path / "ok.txt").write_text("x")
    results = completion.run_checks(goal, cwd=str(tmp_path))
    verdict = completion.judge(goal, results, chain=[chain], extra_evidence="I did it")
    assert chain.calls == 1
    assert verdict.met and verdict.semantic_ok is True
    assert verdict.semantic_prob == pytest.approx(0.95)


def test_judge_below_threshold_not_met(tmp_path):
    pytest.importorskip("hermes_loopguard.backends")
    chain = FakeChain(noul=0.4)
    goal = completion.parse_goal('# Goal: t\n\n## Requirement\nDo it.\n\n## Checks\n- file: ok.txt exists\n')
    (tmp_path / "ok.txt").write_text("x")
    results = completion.run_checks(goal, cwd=str(tmp_path))
    verdict = completion.judge(goal, results, chain=[chain])
    assert not verdict.met and verdict.semantic_ok is False and verdict.semantic_prob == pytest.approx(0.4)
    assert verdict.reasons


def test_judge_no_chain_means_code_only(tmp_path):
    goal = completion.parse_goal('# Goal: t\n\n## Checks\n- file: ok.txt exists\n')
    (tmp_path / "ok.txt").write_text("x")
    verdict = completion.judge(goal, completion.run_checks(goal, cwd=str(tmp_path)))
    assert verdict.met and verdict.semantic_ok is None and verdict.semantic_prob is None


# pre_verify hook
def test_pre_verify_hook(tmp_path):
    pytest.importorskip("hermes_loopguard.backends")
    (tmp_path / "GOAL.md").write_text(
        "# Goal: demo\n\n## Requirement\nWrite the file.\n\n## Checks\n- file: out.txt exists\n")
    store = FakeStore()
    cfg = {"completion": {"goal_file": "GOAL.md", "max_continuations": 2}}
    chain = FakeChain(noul=0.95)
    hook = completion.make_pre_verify_hook(lambda: cfg, store_factory=lambda: store,
                                           chain_factory=lambda: [chain],
                                           cwd_fn=lambda: str(tmp_path))
    (tmp_path / "out.txt").write_text("x")
    assert hook(session_id="s1", final_response="done") is None
    assert store.get_counter("completion:s1") == 0
    assert any(d["outcome"] == "met" for d in store.decisions)

    chain.noul = 0.1
    directive = hook(session_id="s1", final_response="half done")
    assert directive is not None and directive["action"] == "continue"
    assert "(1/2)" in directive["message"]
    assert "GOAL.md is not met yet" in directive["message"]
    assert store.get_counter("completion:s1") == 1
    assert any(d["outcome"] == "continue" for d in store.decisions)

    store.counters["completion:s1"] = 2
    calls_before = chain.calls
    assert hook(session_id="s1", final_response="still trying") is None
    assert chain.calls == calls_before  # cap reached: no goal run, no chain call
    assert any(d["outcome"] == "cap_reached" for d in store.decisions)


def test_pre_verify_hook_missing_goal_file(tmp_path):
    store = FakeStore()
    hook = completion.make_pre_verify_hook(lambda: {"completion": {}},
                                           store_factory=lambda: store,
                                           cwd_fn=lambda: str(tmp_path))
    assert hook(session_id="s") is None
    assert store.decisions == []


def test_pre_verify_hook_never_raises():
    def boom():
        raise RuntimeError("boom")

    hook = completion.make_pre_verify_hook(boom, store_factory=lambda: None, cwd_fn=lambda: "/")
    assert hook(session_id="s") is None


def test_decision_rows_carry_semantic_provenance(tmp_path):
    # The semantic judge's backend/model/usage land on the met/continue rows
    # (model-usage evidence), and never on code-only failure rows.
    from hermes_loopguard.backends.base import GateResult, Usage
    from hermes_loopguard.questions import NoulAnswer

    class ProvenanceChain:  # FAKE BACKEND (unit test only)
        def __init__(self):
            self.calls = 0

        def available(self):
            return True

        def ask(self, state, questions):
            self.calls += 1
            return GateResult(answers={k: NoulAnswer(noul=0.95) for k in questions},
                              backend="openai", model="gemini-3.1-flash-lite",
                              usage=Usage(input_tokens=120, output_tokens=8, cost_usd=0.001))

    (tmp_path / "GOAL.md").write_text(
        "# Goal: demo\n\n## Requirement\nWrite the file.\n\n## Checks\n- file: out.txt exists\n")
    store = FakeStore()
    chain = ProvenanceChain()
    cfg = {"completion": {"goal_file": "GOAL.md", "max_continuations": 2}}
    hook = completion.make_pre_verify_hook(lambda: cfg, store_factory=lambda: store,
                                           chain_factory=lambda: [chain],
                                           cwd_fn=lambda: str(tmp_path))

    # code-only failure: no semantic call, no provenance
    directive = hook(session_id="s1", final_response="nothing yet")
    assert directive is not None and directive["action"] == "continue"
    row = [d for d in store.decisions if d["outcome"] == "continue"][0]
    assert row.get("backend") is None and row.get("model") is None and row.get("tokens_in", 0) == 0
    assert chain.calls == 0

    # semantic ran: provenance on the row
    (tmp_path / "out.txt").write_text("x")
    hook(session_id="s1", final_response="done")
    met_row = [d for d in store.decisions if d["outcome"] == "met"][0]
    assert met_row["backend"] == "openai" and met_row["model"] == "gemini-3.1-flash-lite"
    assert met_row["tokens_in"] == 120 and met_row["tokens_out"] == 8
    assert met_row["cost_usd"] == pytest.approx(0.001)


def test_pre_verify_finds_goal_next_to_changed_paths(tmp_path):
    # Regression: gateway-relayed turns run in the gateway process, whose cwd is NOT
    # the session's working directory. The hook must resolve GOAL.md next to the turn's
    # landed changed_paths (upstream pre_verify kwargs), else it silently no-ops.
    (tmp_path / "GOAL.md").write_text(
        "# Goal: demo\n\n## Requirement\nWrite the file.\n\n## Checks\n- file: out.txt exists\n")
    store = FakeStore()
    cfg = {"completion": {"goal_file": "GOAL.md", "max_continuations": 2}}
    chain = FakeChain(noul=0.1)  # code check passes (file written), semantic says not met -> continue
    hook = completion.make_pre_verify_hook(lambda: cfg, store_factory=lambda: store,
                                           chain_factory=lambda: [chain],
                                           cwd_fn=lambda: "/")  # gateway cwd: no GOAL.md here
    (tmp_path / "out.txt").write_text("x")
    directive = hook(session_id="gw1", final_response="done",
                     changed_paths=[str(tmp_path / "out.txt")])
    assert directive is not None and directive["action"] == "continue"
    assert "(1/2)" in directive["message"]
    assert store.get_counter("completion:gw1") == 1


def test_resolve_goal_file_prefers_cwd_then_walks_up(tmp_path):
    from hermes_loopguard.completion import _resolve_goal_file
    work = tmp_path / "work"
    (work / "deep" / "nest").mkdir(parents=True)
    (work / "GOAL.md").write_text("# Goal: t\n")
    assert _resolve_goal_file("GOAL.md", str(tmp_path), []) is None
    assert _resolve_goal_file("GOAL.md", str(work), []) == str(work / "GOAL.md")
    found = _resolve_goal_file("GOAL.md", str(tmp_path), [str(work / "deep" / "nest" / "f.txt")])
    assert found == str(work / "GOAL.md")
    assert _resolve_goal_file("GOAL.md", str(tmp_path), None) is None
