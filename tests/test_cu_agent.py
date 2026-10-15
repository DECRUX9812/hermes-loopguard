"""Unit tests for the jev_computer agent loop (X3).

EVERYTHING here is a unit-test fake: a FakeDriver replaying recorded frames
and a FakeGate answering canned typed results.  Where a REAL recorded Jev
response is used (the safety-policy tests) it is loaded from
tests/fixtures/cu/vision/cases/ and marked.  No live daemon, display or
model is touched.

FakeDriver hands out one frame per screenshot() call in list order (then
repeats the last), so the frame lists below are sequenced shot-by-shot:
each action step consumes a pre-shot and an after-shot, which must differ
unless the test exercises the no-op path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_loopguard.cu import agent
from hermes_loopguard.cu.drivers.fake import FakeDriver
from hermes_loopguard.cu.vision import (
    ChangeResult,
    LocateResult,
    SafetyActionResult,
    SafetyResult,
    VerifyResult,
    VisionUsage,
)
from hermes_loopguard.questions import NoulAnswer

FIX = Path(__file__).parent / "fixtures" / "cu" / "vision"

# REAL recorded Jev responses (see tests/fixtures/cu/vision/README.md).
SAFETY_FORM = json.loads((FIX / "cases" / "safety_form_jev114.json").read_text())
SAFETY_PAYMENT = json.loads((FIX / "cases" / "safety_payment_jev114.json").read_text())

# The policy thresholds (defaults incl. the new safety_hard).
THRESHOLDS = {"safety": 0.2, "safety_hard": 0.6}


def _usage(model: str = "jev-1.14.0", usd: float = 0.0001) -> VisionUsage:
    return VisionUsage(backend="typesafe", model=model, input_tokens=100, output_tokens=10, usd=usd)


def _safety_from_case(case: dict) -> SafetyResult:
    answers = case["answers"]
    return SafetyResult(
        category=answers["category"]["choice"],
        probs=dict(answers["category"]["probabilities"]),
        confirm_dialog=float(answers["confirm"]["noul"]),
        pause=bool(case.get("pause")),
        usage=_usage(),
    )


class FakeGate:
    """FAKE gate: canned typed answers, every call recorded."""

    def __init__(self):
        self.thresholds = {"presence": 0.6, "box_confidence": 0.5, "verify": 0.7, "safety": 0.2,
                           "safety_hard": 0.6}
        self.calls = {"locate": [], "verify": [], "changed": [], "safety": [],
                      "safety_action": [], "ask": []}
        self.locate_results: list[LocateResult] = []
        self.verify_results: list[VerifyResult] = []
        self.safety_result = SafetyResult(
            category="none", probs={"none": 0.99}, confirm_dialog=0.0, pause=False, usage=_usage()
        )
        self.safety_action_result = SafetyActionResult(probs={}, usage=_usage())
        self.safety_action_results: list[SafetyActionResult] = []
        self.changed_result = ChangeResult(changed=True, distance=20, path="hash", usage=None)
        self.ask_noul = 0.9

    @staticmethod
    def _next(queue: list, default):
        return queue.pop(0) if queue else default

    def locate(self, frame, target, *, region=None):
        self.calls["locate"].append({"target": target, "region": region})
        return self._next(
            self.locate_results,
            LocateResult(found=False, presence=0.0, confidence=0.0, box=None, center=None,
                         grounding="none", usage=_usage()),
        )

    def verify(self, after, expected, *, before=None):
        self.calls["verify"].append({"expected": expected, "before": before is not None})
        return self._next(self.verify_results, VerifyResult(passed=True, yes=0.9, usage=_usage()))

    def changed(self, before, after):
        self.calls["changed"].append({})
        return self.changed_result

    def safety(self, frame):
        self.calls["safety"].append({})
        return self.safety_result

    def safety_action(self, frame, action, target):
        self.calls["safety_action"].append({"action": action, "target": target})
        return self._next(self.safety_action_results, self.safety_action_result)

    def ask(self, images, state, questions):
        self.calls["ask"].append({"questions": list(questions), "images": list(images)})
        return {name: NoulAnswer(noul=self.ask_noul) for name in questions}, _usage()


def make_driver(frames_spec: list[str], elements: list[dict] | None = None, actions: dict | None = None):
    fixture = {
        "frames": [{"file": f, "width": 1280, "height": 800} for f in frames_spec],
        "elements": {"Google-chrome": elements or []},
        "actions": actions or {},
    }
    return FakeDriver(fixture, base_dir=FIX)


def patch_plan(monkeypatch, plans: list[list[agent.Step]]):
    """Replace the planner with a queue of canned plans; records the
    failure-evidence argument of every call."""
    calls: list[str | None] = []

    def fake(instruction, driver, cfg, *, model=None, failure_evidence=None):
        calls.append(failure_evidence)
        idx = min(len(calls) - 1, len(plans) - 1)
        return list(plans[idx]), _usage(model="gemini-3.1-flash-lite", usd=0.0005)

    monkeypatch.setattr(agent, "plan", fake)
    return calls


# --------------------------------------------------------------------- grounding


def test_a11y_grounding_costs_zero_locate_calls(monkeypatch):
    driver = make_driver(
        ["form.png", "form.png", "thanks.png", "thanks.png"],
        elements=[{"token": "e1", "role": "entry", "label": "Name", "x": 10, "y": 20, "w": 300, "h": 30}],
    )
    gate = FakeGate()
    steps = [agent.Step(action="type", target="Name", text="Ada", expect="the Name field shows Ada")]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("type Ada", driver, gate, {"max_steps": 5})
    assert result.state == "completed"
    rec = result.steps[0]
    assert rec.grounding == "a11y"
    assert gate.calls["locate"] == []  # free path: zero-token grounding
    typed = [c for c in driver.calls if c["method"] == "type_text"]
    assert typed and typed[0]["args"]["text"] == "Ada"


def test_box_grounding_when_a11y_missing(monkeypatch):
    driver = make_driver(["form.png", "form.png", "thanks.png", "thanks.png"], elements=[])
    gate = FakeGate()
    gate.locate_results.append(
        LocateResult(found=True, presence=0.9, confidence=0.9, box=(0.1, 0.2, 0.3, 0.4),
                     center=(200, 300), grounding="box", usage=_usage())
    )
    steps = [agent.Step(action="click", target="Submit", expect="the thanks page is shown")]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("submit", driver, gate, {"max_steps": 5})
    rec = result.steps[0]
    assert rec.grounding == "box"
    assert rec.point == (200, 300)
    clicks = [c for c in driver.calls if c["method"] == "click"]
    assert clicks and clicks[0]["args"]["x"] == 200 and clicks[0]["args"]["y"] == 300


# ------------------------------------------------------------------ X4B fixes


def test_ground_type_step_prefers_editable_over_same_named_label(monkeypatch):
    """REAL failure: a static 'Feedback' label shadowed the 'Feedback' text
    area, so typing went into the still-focused Name field.  A type step must
    ground to the editable element first."""
    driver = make_driver(
        ["form.png", "form.png", "thanks.png", "thanks.png"],
        elements=[
            {"token": "lab", "role": "label", "label": "Feedback", "x": 10, "y": 20, "w": 80, "h": 24},
            {"token": "ta", "role": "text area", "label": "Feedback", "x": 10, "y": 50, "w": 300, "h": 120},
        ],
    )
    gate = FakeGate()
    steps = [agent.Step(action="type", target="Feedback", text="hi", expect="the Feedback field shows hi")]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("fill feedback", driver, gate, {"max_steps": 5})
    rec = result.steps[0]
    assert rec.grounding == "a11y"
    clicked = [c for c in driver.calls if c["method"] == "click_element"]
    assert clicked and clicked[0]["args"]["token"] == "ta"


def test_click_step_still_grounds_without_role_preference(monkeypatch):
    """Clicks keep the plain first-match lookup (buttons have no editable role)."""
    driver = make_driver(
        ["form.png", "form.png", "thanks.png", "thanks.png"],
        elements=[
            {"token": "btn", "role": "push button", "label": "Send feedback", "x": 10, "y": 400, "w": 120, "h": 34},
        ],
    )
    gate = FakeGate()
    steps = [agent.Step(action="click", target="Send feedback", expect="the thanks page is shown")]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("submit", driver, gate, {"max_steps": 5})
    rec = result.steps[0]
    assert rec.grounding == "a11y"
    clicked = [c for c in driver.calls if c["method"] == "click_element"]
    assert clicked and clicked[0]["args"]["token"] == "btn"


def test_type_step_retry_selects_all_before_typing(monkeypatch):
    """REAL failure: retried typing APPENDED ('Grace HopperThe fold is not a
    bug').  A RETRY must ctrl+a (replace) so attempts are idempotent.  The
    FIRST attempt must NOT: fields start empty (forms) or preselected (F2
    inline rename), and the live probe showed the select-all DESTROYS the
    rename preselection (GTK icon views swallow ctrl+a), so typing then
    appends ('report.txtreport-final.txt' on disk)."""
    driver = make_driver(
        ["form.png"] * 6,
        elements=[{"token": "e1", "role": "entry", "label": "Name", "x": 10, "y": 20, "w": 300, "h": 30}],
    )
    gate = FakeGate()
    gate.ask_noul = 0.9
    steps = [
        agent.Step(action="type", target="Name", text="Ada", expect="the Name field shows Ada"),
        agent.Step(action="type", target="Name", text="Ada", expect="the Name field shows Ada"),
    ]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("type Ada twice", driver, gate, {"max_steps": 5})
    assert result.state == "completed"
    methods = [c["method"] for c in driver.calls]
    keys = [c["args"].get("keys") for c in driver.calls if c["method"] == "key"]
    assert keys.count("ctrl+a") == 1  # only the repeat of the same field
    assert methods.index("type_text") < methods.index("key")  # after the first attempt


def test_type_step_after_key_step_skips_the_pre_click(monkeypatch):
    """REAL failure (thunar rename): F2 opens an inline edit with the name
    preselected; the type step re-clicked the entry (caret move / AT-SPI
    activate) and the select-all was swallowed, so typing APPENDED.  A type
    step that directly follows a key step must type into the already-focused
    widget without clicking."""
    driver = make_driver(
        ["form.png"] * 6,
        elements=[{"token": "e1", "role": "text", "label": "report.txt", "x": 10, "y": 20, "w": 300, "h": 30}],
    )
    gate = FakeGate()
    gate.ask_noul = 0.9
    steps = [
        agent.Step(action="key", keys="F2", expect="the rename field is open"),
        agent.Step(action="type", target="report.txt", text="renamed.txt",
                   expect="the list shows renamed.txt"),
    ]
    patch_plan(monkeypatch, [steps])
    gate.verify_results.append(VerifyResult(passed=True, yes=0.9, usage=_usage()))
    result = agent.run_task("rename it", driver, gate, {"max_steps": 5})
    assert result.state == "completed"
    rec = result.steps[1]
    assert rec.verdict in ("pass", "free")
    methods = [c["method"] for c in driver.calls]
    keys = [c["args"].get("keys") for c in driver.calls if c["method"] == "key"]
    assert keys == ["F2"]  # no ctrl+a between F2 and typing
    clicks = [c for c in driver.calls if c["method"] in ("click", "click_element")]
    assert not clicks, "no re-click after the key step opened the inline edit"
    typed = [c for c in driver.calls if c["method"] == "type_text"]
    assert typed and typed[-1]["args"]["text"] == "renamed.txt"


def test_box_type_step_grounds_to_nearest_editable(monkeypatch):
    """REAL failure (mousepad-save): the Save-As Name entry has an EMPTY a11y
    label; the Box locate finds the 'Name' LABEL and clicking it leaves the
    focus on the search box, so the typed filename landed wherever focus was.
    A type step must ground to the editable field next to the located label."""
    driver = make_driver(
        ["form.png", "form.png", "form.png", "form.png"],
        elements=[
            {"token": "lab", "role": "label", "label": "Name", "x": 10, "y": 60, "w": 50, "h": 30},
            {"token": "ent", "role": "text", "label": "", "x": 70, "y": 60, "w": 400, "h": 30},
        ],
    )
    gate = FakeGate()
    gate.locate_results.append(
        LocateResult(found=True, presence=0.9, confidence=0.9, box=(0.01, 0.07, 0.05, 0.12),
                     center=(35, 75), grounding="box", usage=_usage())
    )
    steps = [agent.Step(action="type", target="Name", text="hello.txt",
                        expect="the Name field shows hello.txt")]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("save it", driver, gate, {"max_steps": 5})
    rec = result.steps[0]
    assert rec.grounding == "a11y"
    clicked = [c for c in driver.calls if c["method"] == "click_element"]
    assert clicked and clicked[0]["args"]["token"] == "ent"


def test_type_step_without_target_types_into_focus(monkeypatch):
    """REAL failure: the planner's type step for a fresh text-editor document
    had an EMPTY target; the agent died in grounding ("grounding failed")
    instead of just typing into the focused window."""
    driver = make_driver(
        ["form.png", "form.png", "thanks.png", "thanks.png"], elements=[]
    )
    gate = FakeGate()
    steps = [agent.Step(action="type", target="", text="Hello from LoopGuard",
                        expect="the document shows Hello from LoopGuard")]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("type into the editor", driver, gate, {"max_steps": 5})
    assert result.state == "completed"
    rec = result.steps[0]
    assert rec.verdict in ("pass", "free")
    methods = [c["method"] for c in driver.calls]
    assert "click" not in methods and "click_element" not in methods
    typed = [c for c in driver.calls if c["method"] == "type_text"]
    assert typed and typed[0]["args"]["text"] == "Hello from LoopGuard"


def test_pad_region_grows_tiny_box_to_typeafe_minimum():
    """REAL failure: a zoomed-retry crop of 154x36 px was rejected by
    prepare_image (side >= 256, >= 78400 px), killing the zoom retry."""
    w, h = 1280, 800
    box = (0.40, 0.30, 0.52, 0.345)  # ~154x36 px on this display
    region = agent._pad_region(box, w, h)
    assert region is not None
    x0, y0, x1, y1 = region
    assert x1 - x0 >= 256
    assert y1 - y0 >= 256
    assert (x1 - x0) * (y1 - y0) >= 78_400
    assert 0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h


def test_clipboard_keys_are_never_verified_and_never_stall(monkeypatch):
    """REAL failure: ctrl+x (cut) has NO visible effect, so the verify Noul
    rated it ~0.13 and the stall detector called it a no-op; the move was
    retried, then blocked.  A clipboard-only key is neither verified on
    screen nor counted as a no-op/stall: the next visible step (the paste)
    carries the check."""
    driver = make_driver(["form.png"] * 8, elements=[])
    gate = FakeGate()
    gate.verify_results.append(VerifyResult(passed=False, yes=0.13, usage=_usage()))
    steps = [
        agent.Step(action="key", keys="ctrl+x", expect="the file is cut"),
        agent.Step(action="key", keys="ctrl+v", expect="the file appears here"),
    ]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("move it", driver, gate, {"max_steps": 5})
    cut = result.steps[0]
    paste = result.steps[1]
    assert cut.verdict == "free"  # not verified, not a no-op
    assert paste.verdict == "unverified"  # unchanged screen: invisible, not fail
    assert result.state == "completed"  # no stall, no retry ladder
    # the cut produced ZERO verify calls; only the paste + the final goal check
    assert len(gate.calls["verify"]) == 2
    assert [c["expected"] for c in gate.calls["verify"]] == [
        "the file appears here",
        "the overall task is done: move it",
    ]


def test_is_invisible_key_matches_clipboard_combos_only():
    assert agent._is_invisible_key("ctrl+x")
    assert agent._is_invisible_key("Ctrl+C")  # case/space tolerant
    assert agent._is_invisible_key("ctrl+insert")
    assert not agent._is_invisible_key("ctrl+v")  # the paste IS visible
    assert not agent._is_invisible_key("Return")
    assert not agent._is_invisible_key("ctrl+a")
    assert not agent._is_invisible_key("")
    assert not agent._is_invisible_key(None)


def test_goal_replan_warns_against_repeating_toggles(monkeypatch):
    """REAL failure (settings-gtk r2): the goal check failed once, the replan
    re-clicked 'Dark mode' (a toggle) which UNDID it, and the judge failed.
    The goal-replan evidence must forbid repeating toggles and allow an
    empty plan when the goal is already met."""
    driver = make_driver(
        ["form.png", "form.png", "form.png", "form.png", "form.png"],
        elements=[{"token": "e1", "role": "toggle", "label": "Dark mode", "x": 10, "y": 20, "w": 120, "h": 30}],
    )
    gate = FakeGate()
    gate.verify_results.append(VerifyResult(passed=False, yes=0.01, usage=_usage()))
    gate.verify_results.append(VerifyResult(passed=False, yes=0.01, usage=_usage()))
    steps = [agent.Step(action="click", target="Dark mode", expect="dark mode is on")]
    calls = patch_plan(monkeypatch, [steps, []])  # replan returns nothing: straight to terminal
    result = agent.run_task("enable dark mode", driver, gate, {"max_steps": 5})
    assert result.state == "blocked"
    evidence = calls[1]
    assert evidence is not None
    assert "UNDOES" in evidence
    assert "empty steps list" in evidence


def test_verify_fail_on_changed_screen_still_fails(monkeypatch):
    """Contrast: the screen DID change but the expectation is missing -> a
    real failure that must still go down the ladder (retry, then replan)."""
    driver = make_driver(
        ["form.png", "thanks.png", "form.png", "thanks.png", "thanks.png", "thanks.png"],
        elements=[],
    )
    gate = FakeGate()
    gate.verify_results.append(VerifyResult(passed=False, yes=0.1, usage=_usage()))
    steps = [agent.Step(action="key", keys="Return", expect="the thanks page is shown")]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("submit it", driver, gate, {"max_steps": 5})
    rec = result.steps[0]
    assert rec.verdict == "fail"  # visible change, expectation unmet: fail
    assert len(gate.calls["verify"]) > 2  # the ladder ran (retry/replan)


def test_flush_unverified_when_field_screen_unchanged(monkeypatch):
    """Batched field checks: a failing Noul on a screen that did not change
    since the typing is 'unverified', not a flush failure (no re-queue)."""
    driver = make_driver(
        ["form.png"] * 6,
        elements=[{"token": "e1", "role": "entry", "label": "Name", "x": 10, "y": 20, "w": 300, "h": 30}],
    )
    gate = FakeGate()
    gate.ask_noul = 0.1  # batched verify says the field does not show it
    steps = [agent.Step(action="type", target="Name", text="Ada",
                        expect="the Name field shows Ada")]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("type Ada", driver, gate, {"max_steps": 5})
    rec = result.steps[0]
    assert rec.verdict == "unverified"
    assert result.state == "completed"  # no 'field verification kept failing'


def test_goal_check_failure_replans_once(monkeypatch):
    """REAL failure: the cheap planner under-plans (1 step for a 4-step task);
    the goal check then fails and the run went terminal.  The agent must
    replan ONCE with that evidence before giving up."""
    driver = make_driver(
        ["form.png", "form.png", "thanks.png", "thanks.png", "thanks.png", "thanks.png"],
        elements=[{"token": "e1", "role": "entry", "label": "Name", "x": 10, "y": 20, "w": 300, "h": 30}],
    )
    gate = FakeGate()
    gate.verify_results.append(VerifyResult(passed=True, yes=0.9, usage=_usage()))   # step 1 verify
    gate.verify_results.append(VerifyResult(passed=False, yes=0.0, usage=_usage()))  # goal check #1
    for _ in range(3):  # box grounding for the planned clicks
        gate.locate_results.append(
            LocateResult(found=True, presence=0.9, confidence=0.9, box=(0.1, 0.2, 0.3, 0.4),
                         center=(200, 300), grounding="box", usage=_usage())
        )
    calls = patch_plan(monkeypatch, [
        [agent.Step(action="click", target="link", expect="the page is open")],
        [agent.Step(action="type", target="Name", text="Ada", expect="the Name field shows Ada")],
    ])
    result = agent.run_task("do it", driver, gate, {"max_steps": 8})
    assert result.state == "completed"
    assert calls[1] is not None  # the replan carried failure evidence


def test_goal_check_failure_replans_only_once(monkeypatch):
    driver = make_driver(["form.png"] * 10, elements=[])
    gate = FakeGate()
    gate.verify_results.append(VerifyResult(passed=True, yes=0.9, usage=_usage()))   # step 1 verify
    gate.verify_results.append(VerifyResult(passed=False, yes=0.0, usage=_usage()))  # goal check #1
    gate.verify_results.append(VerifyResult(passed=True, yes=0.9, usage=_usage()))   # step 2 verify
    gate.verify_results.append(VerifyResult(passed=False, yes=0.0, usage=_usage()))  # goal check #2
    for _ in range(5):  # box grounding for the planned clicks
        gate.locate_results.append(
            LocateResult(found=True, presence=0.9, confidence=0.9, box=(0.1, 0.2, 0.3, 0.4),
                         center=(200, 300), grounding="box", usage=_usage())
        )
    patch_plan(monkeypatch, [
        [agent.Step(action="click", target="link", expect="the page is open")],
        [agent.Step(action="click", target="other", expect="the page is open")],
    ])
    result = agent.run_task("do it", driver, gate, {"max_steps": 8})
    assert result.state == "blocked"
    assert "goal check" in result.reason


def test_pad_region_keeps_big_boxes_centered():
    w, h = 1280, 800
    box = (0.25, 0.25, 0.75, 0.75)
    region = agent._pad_region(box, w, h)
    x0, y0, x1, y1 = region
    assert abs((x0 + x1) / 2 - w / 2) < w * 0.05
    assert abs((y0 + y1) / 2 - h / 2) < h * 0.05


def test_planner_prompt_has_no_task_specific_strings():
    """src/ must stay generic: no bench task names or page names in the
    planner prompt."""
    prompt = agent._PLANNER_SYSTEM.lower()
    for banned in ("fm-task", "mousepad-save", "thunar", "settings-gtk", "editor-task",
                   "quickstart", "grace hopper", "notes.txt", "report.txt"):
        assert banned not in prompt, f"task-specific string {banned!r} leaked into src/"


def test_zoom_retry_uses_padded_region(monkeypatch):
    driver = make_driver(["form.png", "form.png", "thanks.png", "thanks.png"], elements=[])
    gate = FakeGate()
    box = (0.4, 0.4, 0.5, 0.5)  # 128x80 px at 1280x800
    gate.locate_results.append(
        LocateResult(found=False, presence=0.9, confidence=0.3, box=box, center=None,
                     grounding="box", usage=_usage())
    )
    gate.locate_results.append(
        LocateResult(found=True, presence=0.9, confidence=0.9, box=box, center=(576, 360),
                     grounding="box", usage=_usage())
    )
    steps = [agent.Step(action="click", target="Save", expect="saved")]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("save", driver, gate, {"max_steps": 5})
    rec = result.steps[0]
    assert rec.grounding == "zoom"
    assert len(gate.calls["locate"]) == 2
    # 25% padding of the 128x80 box gives (480,300,672,420) = 192x120 px,
    # which the zoom now GROWS to meet the TypeSafe minimums (>=256/side,
    # >=78400 px): 354x256 centered on the same point -> (398,232,753,488).
    assert gate.calls["locate"][1]["region"] == (398, 232, 753, 488)


def test_padded_region_crops_always_meet_typesafe_minimums():
    """REAL failure (mousepad-save r2): two Jev `400 Invalid image dimensions`
    responses came from crops too small to prepare.  Every region _pad_region
    returns must survive crop + prepare_image without raising or shrinking
    below the TypeSafe minimums, including corner-hugging and sliver boxes."""
    from hermes_loopguard.cu import frames

    frame_png = (FIX / "form.png").read_bytes()
    for frac_box in [
        (0.40, 0.30, 0.42, 0.315),  # tiny sliver mid-screen
        (0.05, 0.90, 0.30, 0.99),  # hugging the bottom-left corner
        (0.95, 0.05, 0.99, 0.40),  # hugging the right edge
        (0.48, 0.48, 0.52, 0.52),  # tiny square dead center
    ]:
        region = agent._pad_region(frac_box, 1280, 800)
        assert region is not None, frac_box
        x0, y0, x1, y1 = region
        prepared, w, h = frames.prepare_image(frames.crop(frame_png, x0, y0, x1, y1))
        assert w >= frames.MIN_SIDE and h >= frames.MIN_SIDE, frac_box
        assert w * h >= frames.MIN_PIXELS, frac_box


# --------------------------------------------------------------------- safety


def test_safety_pause_stops_before_the_click(monkeypatch):
    driver = make_driver(["form.png", "form.png", "payment.png", "payment.png"], elements=[])
    gate = FakeGate()
    gate.locate_results.append(
        LocateResult(found=True, presence=0.9, confidence=0.9, box=None, center=(100, 200),
                     grounding="box", usage=_usage())
    )
    # REAL recorded Jev answer for a payment dialog: pause (payment 1.0).
    gate.safety_result = _safety_from_case(SAFETY_PAYMENT)
    steps = [agent.Step(action="click", target="Pay $49", expect="paid")]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("buy thing", driver, gate, {"max_steps": 5})
    assert result.state == "paused_for_approval"
    assert "payment" in result.reason
    assert not [c for c in driver.calls if c["method"] == "click"], "must never click through a pause"
    rec = result.steps[0]
    assert rec.verdict == "paused" and rec.safety == "payment"


def test_safety_policy_on_recorded_form_fixture():
    """REAL recorded Jev response: a plain form puts 0.41 on send_or_publish.

    0.41 sits in the ambiguous band [0.2, 0.6), so the two-stage policy asks
    for the stage-2 action-level check instead of pausing outright (intentional
    change from the single-stage policy, which paused here).
    """
    result = _safety_from_case(SAFETY_FORM)
    # Unauthorized ("fill the fields") -> stage 2 required before any click.
    d = agent.safety_decision(
        result, None, agent.authorized_categories("Fill in the fields with Ada"), THRESHOLDS
    )
    assert d.pause and d.needs_stage2
    # Stage 2 says the action itself is not risky -> no pause.
    stage2 = SafetyActionResult(
        probs={"payment": 0.0, "deletion": 0.0, "account_or_security_change": 0.01,
               "send_or_publish": 0.1},
        usage=_usage(),
    )
    d = agent.safety_decision(
        result, stage2, agent.authorized_categories("Fill in the fields with Ada"), THRESHOLDS
    )
    assert not d.pause
    # Stage 2 agrees it IS a send -> pause.
    stage2 = SafetyActionResult(
        probs={"payment": 0.0, "deletion": 0.0, "account_or_security_change": 0.0,
               "send_or_publish": 0.9},
        usage=_usage(),
    )
    d = agent.safety_decision(
        result, stage2, agent.authorized_categories("Fill in the fields with Ada"), THRESHOLDS
    )
    assert d.pause and "send_or_publish" in d.reason
    # Authorized ("submit the form") -> no pause, no stage 2 needed at all.
    d = agent.safety_decision(
        result, None, agent.authorized_categories("Fill the form and submit it"), THRESHOLDS
    )
    assert not d.pause and not d.needs_stage2


def test_payment_is_never_pre_authorized():
    # REAL recorded payment dialog: payment 1.0 >= safety_hard -> pause at
    # stage 1, the stage-2 check is never consulted.
    result = _safety_from_case(SAFETY_PAYMENT)
    d = agent.safety_decision(result, None, {"send_or_publish"}, THRESHOLDS)
    assert d.pause and not d.needs_stage2 and "payment" in d.reason


def test_hard_threshold_pauses_without_stage2():
    """A category in [safety_hard, 1.0] pauses WITHOUT the stage-2 check."""
    result = SafetyResult(
        category="account_or_security_change",
        probs={"account_or_security_change": 0.75, "none": 0.2},
        confirm_dialog=0.0, pause=True, usage=_usage(),
    )
    d = agent.safety_decision(result, None, set(), THRESHOLDS)
    assert d.pause and not d.needs_stage2 and "account_or_security_change" in d.reason


def test_ambiguous_band_stage2_clears_and_confirms():
    """Band category: stage-2 low Noul clears, high Noul pauses (settings case)."""
    # The REAL settings false-pause shape: account_or_security_change 0.43.
    stage1 = SafetyResult(
        category="account_or_security_change",
        probs={"account_or_security_change": 0.43, "none": 0.5},
        confirm_dialog=0.0, pause=True, usage=_usage(),
    )
    low = SafetyActionResult(
        probs={"payment": 0.0, "deletion": 0.0, "account_or_security_change": 0.03,
               "send_or_publish": 0.0},
        usage=_usage(),
    )
    d = agent.safety_decision(stage1, low, set(), THRESHOLDS)
    assert not d.pause, "the mundane settings click must not pause"
    high = SafetyActionResult(
        probs={"payment": 0.0, "deletion": 0.0, "account_or_security_change": 0.97,
               "send_or_publish": 0.0},
        usage=_usage(),
    )
    d = agent.safety_decision(stage1, high, set(), THRESHOLDS)
    assert d.pause and "account_or_security_change" in d.reason


def test_stage2_error_pauses():
    stage1 = SafetyResult(
        category="send_or_publish", probs={"send_or_publish": 0.41, "none": 0.55},
        confirm_dialog=0.0, pause=True, usage=_usage(),
    )
    stage2 = SafetyActionResult(probs={}, usage=None, error="all vision models failed")
    d = agent.safety_decision(stage1, stage2, set(), THRESHOLDS)
    assert d.pause and "stage-2" in d.reason


def test_recorded_settings_false_pause_fixed_by_stage2():
    """REAL recorded pair: the plain GTK settings window puts 0.39 on
    account_or_security_change (stage 1, the old false pause) and clicking
    'Dark mode' scores 0.02 on that category (stage 2) -> no pause."""
    stage1 = _safety_from_case(
        json.loads((FIX / "cases" / "safety_stage1_settings_gtk_jev114.json").read_text())
    )
    assert 0.2 <= stage1.probs["account_or_security_change"] < 0.6, "band case"
    case2 = json.loads((FIX / "cases" / "safety_stage2_settings_gtk_dark_mode_jev114.json").read_text())
    stage2 = SafetyActionResult(
        probs={c: float(a["noul"]) for c, a in case2["answers"].items()}, usage=_usage()
    )
    d = agent.safety_decision(stage1, stage2, set(), THRESHOLDS)
    assert not d.pause, "the mundane settings click must not pause"

    # Same stage 1, but the action-level check CONFIRMS (REAL 'Update
    # password' answer: 0.98) -> pause.
    case3 = json.loads((FIX / "cases" / "safety_stage2_password_update_jev114.json").read_text())
    stage2_bad = SafetyActionResult(
        probs={c: float(a["noul"]) for c, a in case3["answers"].items()}, usage=_usage()
    )
    d = agent.safety_decision(stage1, stage2_bad, set(), THRESHOLDS)
    assert d.pause and "account_or_security_change" in d.reason


def test_recorded_payment_pauses_at_stage1_without_stage2():
    """REAL recorded pay dialog: payment 1.0 >= safety_hard -> pause at stage 1;
    the stage-2 request is never made (the recorded stage-2 pay answer is
    reference only)."""
    stage1 = _safety_from_case(
        json.loads((FIX / "cases" / "safety_stage1_pay_jev114.json").read_text())
    )
    assert stage1.probs["payment"] >= 0.6
    d = agent.safety_decision(stage1, None, set(), THRESHOLDS)
    assert d.pause and not d.needs_stage2 and "payment" in d.reason


def test_safety_error_pauses():
    result = SafetyResult(category="none", probs={}, confirm_dialog=0.0, pause=False,
                          usage=None, error="all models failed")
    d = agent.safety_decision(result, None, {"send_or_publish"}, THRESHOLDS)
    assert d.pause


def test_safety_decision_none_stage1_pauses():
    d = agent.safety_decision(None, None, set(), THRESHOLDS)
    assert d.pause and "no safety result" in d.reason


def test_authorized_send_or_publish_does_not_pause(monkeypatch):
    driver = make_driver(["form.png", "form.png", "thanks.png", "thanks.png"], elements=[])
    gate = FakeGate()
    gate.locate_results.append(
        LocateResult(found=True, presence=0.9, confidence=0.9, box=None, center=(100, 200),
                     grounding="box", usage=_usage())
    )
    # REAL recorded form answer: send_or_publish 0.41 on a plain form.
    gate.safety_result = _safety_from_case(SAFETY_FORM)
    steps = [agent.Step(action="click", target="Submit", expect="the thanks page is shown")]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("Fill the form and click Submit", driver, gate, {"max_steps": 5})
    assert result.state == "completed"
    # Authorized: the ambiguous band is skipped entirely, no stage-2 call.
    assert gate.calls["safety_action"] == []


def test_unauthorized_band_runs_stage2_then_acts(monkeypatch):
    """The settings false-pause shape, fixed: band risk -> stage 2 clears -> act."""
    driver = make_driver(["settings.png", "settings.png", "settings.png", "settings.png"],
                         elements=[])
    gate = FakeGate()
    gate.locate_results.append(
        LocateResult(found=True, presence=0.9, confidence=0.9, box=None, center=(100, 200),
                     grounding="box", usage=_usage())
    )
    # Stage 1: account_or_security_change 0.43 (ambiguous band).
    gate.safety_result = SafetyResult(
        category="account_or_security_change",
        probs={"account_or_security_change": 0.43, "none": 0.5},
        confirm_dialog=0.0, pause=True, usage=_usage(),
    )
    # Stage 2: clicking 'Dark mode' changes no security setting (low Noul).
    gate.safety_action_result = SafetyActionResult(
        probs={"payment": 0.0, "deletion": 0.0, "account_or_security_change": 0.02,
               "send_or_publish": 0.0},
        usage=_usage(),
    )
    steps = [agent.Step(action="click", target="Dark mode", expect="Dark mode is enabled")]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("Enable Dark mode", driver, gate, {"max_steps": 5})
    assert result.state == "completed"
    assert gate.calls["safety_action"] == [{"action": "click", "target": "Dark mode"}]
    clicks = [c for c in driver.calls if c["method"] == "click"]
    assert clicks, "stage 2 cleared the ambiguous band: the click must happen"


def test_unauthorized_band_stage2_confirms_pauses(monkeypatch):
    """Band risk where stage 2 AGREES -> pause before the click (password form)."""
    driver = make_driver(["settings.png", "settings.png", "settings.png", "settings.png"],
                         elements=[])
    gate = FakeGate()
    gate.locate_results.append(
        LocateResult(found=True, presence=0.9, confidence=0.9, box=None, center=(100, 200),
                     grounding="box", usage=_usage())
    )
    gate.safety_result = SafetyResult(
        category="account_or_security_change",
        probs={"account_or_security_change": 0.45, "none": 0.5},
        confirm_dialog=0.0, pause=True, usage=_usage(),
    )
    gate.safety_action_result = SafetyActionResult(
        probs={"payment": 0.0, "deletion": 0.0, "account_or_security_change": 0.93,
               "send_or_publish": 0.0},
        usage=_usage(),
    )
    steps = [agent.Step(action="click", target="Update password", expect="password updated")]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("Change my password", driver, gate, {"max_steps": 5})
    assert result.state == "paused_for_approval"
    assert "account_or_security_change" in result.reason
    assert not [c for c in driver.calls if c["method"] == "click"], "never click through a pause"


def test_safety_action_cached_by_dhash_action_target():
    """Stage-2 cache key is (frame dhash, action, target)."""

    class _F:
        def __init__(self, png):
            self.png, self.width, self.height, self.ts = png, 1280, 800, 0.0

    gate = FakeGate()
    frame = _F((FIX / "settings.png").read_bytes())
    cache: dict = {}
    first = agent._safety_action_cached(gate, frame, "click", "Dark mode", cache)
    assert agent._safety_action_cached(gate, frame, "click", "Dark mode", cache) is first
    assert len(gate.calls["safety_action"]) == 1, "same dhash+action+target must hit the cache"
    agent._safety_action_cached(gate, frame, "click", "Save", cache)
    agent._safety_action_cached(gate, frame, "type", "Dark mode", cache)
    assert len(gate.calls["safety_action"]) == 3, "a different action or target is a new key"


# --------------------------------------------------------------------- verdicts


def test_free_path_when_driver_confirms(monkeypatch):
    driver = make_driver(
        ["form.png", "form.png", "thanks.png", "thanks.png"],
        elements=[{"token": "e1", "role": "button", "label": "Submit", "x": 10, "y": 20, "w": 30, "h": 10}],
        actions={"click_element": [{"ok": True, "effect": "confirmed", "verified": True}]},
    )
    gate = FakeGate()
    steps = [agent.Step(action="click", target="Submit", expect="the thanks page is shown")]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("submit", driver, gate, {"max_steps": 5})
    rec = result.steps[0]
    assert rec.verdict == "free"
    # Only the final done-check used a verify call.
    assert len(gate.calls["verify"]) == 1


def test_type_fields_verified_in_one_batched_request(monkeypatch):
    driver = make_driver(["form.png"] * 2 + ["settings.png", "payment.png", "thanks.png",
                                             "form.png", "settings.png", "payment.png"],
                         elements=[
        {"token": "e1", "role": "entry", "label": "Name", "x": 10, "y": 20, "w": 300, "h": 30},
        {"token": "e2", "role": "entry", "label": "Email", "x": 10, "y": 60, "w": 300, "h": 30},
    ])
    gate = FakeGate()
    gate.locate_results.append(
        LocateResult(found=True, presence=0.9, confidence=0.9, box=None, center=(400, 400),
                     grounding="box", usage=_usage())
    )
    steps = [
        agent.Step(action="type", target="Name", text="Ada", expect="the Name field shows Ada"),
        agent.Step(action="type", target="Email", text="ada@x.io", expect="the Email field shows ada@x.io"),
        agent.Step(action="click", target="Submit", expect="the thanks page is shown"),
    ]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("fill form", driver, gate, {"max_steps": 8})
    assert result.state == "completed"
    assert len(gate.calls["ask"]) == 1, "both field checks must share ONE request"
    assert len(gate.calls["ask"][0]["questions"]) == 2
    # Field steps recorded the batch verdicts (flushed BEFORE the Submit click).
    assert [r.verdict for r in result.steps[:2]] == ["pass", "pass"]
    assert all(r.noul == pytest.approx(0.9) for r in result.steps[:2])
    assert result.steps[2].verdict == "pass"


# --------------------------------------------------------------------- recovery


def test_replan_once_after_two_failures(monkeypatch):
    driver = make_driver(["form.png", "thanks.png"] * 4, elements=[])
    gate = FakeGate()
    for _ in range(3):  # two attempts of the failing step + one of the new step
        gate.locate_results.append(
            LocateResult(found=True, presence=0.9, confidence=0.9, box=None, center=(100, 200),
                         grounding="box", usage=_usage())
        )
    gate.verify_results.append(VerifyResult(passed=False, yes=0.1, usage=_usage()))
    gate.verify_results.append(VerifyResult(passed=False, yes=0.1, usage=_usage()))
    steps = [agent.Step(action="click", target="Submit", expect="the thanks page is shown")]
    plan_calls = patch_plan(monkeypatch, [
        steps,
        [agent.Step(action="click", target="Submit button", expect="the thanks page is shown")],
    ])
    result = agent.run_task("submit", driver, gate, {"max_steps": 8})
    assert result.state == "completed"
    assert len(plan_calls) == 2 and plan_calls[1], "replan must carry the failure evidence"


def test_escalation_only_after_replan_and_only_for_that_step(monkeypatch):
    driver = make_driver(
        ["form.png", "form.png", "thanks.png", "payment.png", "settings.png", "thanks.png",
         "form.png", "thanks.png", "form.png"],
        elements=[],
    )
    gate = FakeGate()
    for center in (None, None, (500, 500)):  # c1 fails to ground twice, c2 grounds
        gate.locate_results.append(
            LocateResult(found=center is not None, presence=0.9, confidence=0.9, box=None,
                         center=center, grounding="box" if center else "none", usage=_usage())
        )

    def fake_escalate(frame, instruction, step, cfg, *, model=None):
        return {"action": "click", "x": 42, "y": 24}, _usage(model="claude-opus-4-6-thinking", usd=0.02)

    monkeypatch.setattr(agent, "escalate", fake_escalate)
    # Steps: a click whose target can't be grounded (fails twice), then an
    # easy click.  Replan returns nothing -> the failing step is escalated,
    # the next step runs on the cheap path again.
    steps = [
        agent.Step(action="click", target="Invisible button", expect="pressed"),
        agent.Step(action="click", target="Easy button", expect="pressed"),
    ]
    plan_calls = patch_plan(monkeypatch, [steps, []])
    result = agent.run_task("do it", driver, gate, {"max_steps": 10})
    escalated = [r for r in result.steps if r.grounding == "escalated"]
    assert len(escalated) == 1 and escalated[0].target == "Invisible button"
    assert result.calls_per_model.get("claude-opus-4-6-thinking") == 1
    assert result.big_model_calls == 1
    assert len(plan_calls) == 2  # initial + one replan; escalation is not a replan
    assert result.state == "completed"


def test_escalation_capped_at_two_per_task(monkeypatch):
    driver = make_driver(
        ["form.png", "thanks.png", "payment.png", "settings.png", "thanks.png",
         "form.png", "payment.png", "thanks.png", "form.png"],
        elements=[],
    )
    gate = FakeGate()
    steps = [agent.Step(action="click", target="Invisible", expect="pressed") for _ in range(3)]
    patch_plan(monkeypatch, [steps, [], []])

    count = {"n": 0}

    def fake_escalate(frame, instruction, step, cfg, *, model=None):
        count["n"] += 1
        return {"action": "click", "x": 1, "y": 2}, _usage(model="claude-opus-4-6-thinking", usd=0.02)

    monkeypatch.setattr(agent, "escalate", fake_escalate)
    result = agent.run_task("do it", driver, gate, {"max_steps": 12})
    assert count["n"] == 2, "at most 2 escalations per task"
    assert result.state == "blocked"


# --------------------------------------------------------------------- caps / stall


def test_budget_cap(monkeypatch):
    driver = make_driver(["form.png", "form.png", "thanks.png", "thanks.png"], elements=[])
    gate = FakeGate()
    gate.locate_results.append(
        LocateResult(found=True, presence=0.9, confidence=0.9, box=None, center=(100, 200),
                     grounding="box", usage=_usage(usd=0.01))
    )
    steps = [
        agent.Step(action="click", target="A", expect="a"),
        agent.Step(action="click", target="B", expect="b"),
    ]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("two clicks", driver, gate, {"max_steps": 5, "budget_usd": 0.005})
    assert result.state == "budget_exhausted"
    assert result.usd >= 0.005


def test_stall_terminates_when_screen_never_changes(monkeypatch):
    # Steps WITHOUT an expectation (nothing to verify): the hash path is the
    # only signal, so 4 unchanged screens in a row must go terminal stalled.
    driver = make_driver(["form.png"] * 12, elements=[
        {"token": "e1", "role": "button", "label": "nothing", "x": 10, "y": 20, "w": 30, "h": 10},
    ])
    gate = FakeGate()
    steps = [agent.Step(action="click", target="nothing") for _ in range(4)]
    patch_plan(monkeypatch, [steps, [agent.Step(action="click", target="nothing")]])
    result = agent.run_task("stuck task", driver, gate, {"max_steps": 12, "stall": {"stale_limit": 4}})
    assert result.state == "stalled"
    assert "no screen change" in result.reason
    assert any(r.stall for r in result.steps)


def test_stall_trusting_verify_over_hash(monkeypatch):
    # dhash cannot see a focus ring / typed text: when the (batched) verify
    # passes, unchanged-looking screens must NOT count as no-ops.
    driver = make_driver(["form.png"] * 8, elements=[
        {"token": "e1", "role": "entry", "label": "Name", "x": 10, "y": 20, "w": 300, "h": 30},
    ])
    gate = FakeGate()
    gate.verify_results.append(VerifyResult(passed=True, yes=0.98, usage=_usage()))
    steps = [
        agent.Step(action="type", target="Name", text="Ada", expect="the Name field shows Ada"),
        agent.Step(action="type", target="Name", text="Ada", expect="the Name field shows Ada"),
    ]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("type Ada", driver, gate, {"max_steps": 6, "stall": {"stale_limit": 2}})
    assert result.state == "completed"
    assert all(r.verdict == "pass" and not r.stall for r in result.steps)


def test_aba_loop_detected(monkeypatch):
    # Screenshot sequence s0..s8.  After-shots s2/s4/s6/s8 alternate
    # form/thanks/form/thanks while each pre-shot differs from its after-shot,
    # so the click alternates between two screens: A-B-A-B.
    frames = ["form.png", "thanks.png", "form.png", "form.png", "thanks.png",
              "thanks.png", "form.png", "form.png", "thanks.png"]
    driver = make_driver(frames, elements=[
        {"token": "e1", "role": "button", "label": "toggle", "x": 10, "y": 20, "w": 30, "h": 10},
    ])
    gate = FakeGate()
    steps = [agent.Step(action="click", target="toggle", expect="toggled") for _ in range(4)]
    patch_plan(monkeypatch, [steps])
    result = agent.run_task("loop task", driver, gate, {"max_steps": 10})
    assert result.state == "stalled"
    assert "A-B-A-B" in result.reason
