"""jev_computer agent loop (X3): Jev drives the computer through cua-driver.

Cheapest first, escalate only when needed:

1. ONE cheap text call plans the task into steps (no screenshot).
2. Ground each target on the free AT-SPI tree first, then a Jev Box locate
   (one zoomed retry at low confidence).
3. Safety gate before mutating actions, TWO-STAGE and still biased to ask:
   a screen-level Choice + confirm-dialog Noul (cached by frame dhash), then
   ONE action-level check on the same screenshot only when a risky category
   sits in the ambiguous band [safety, safety_hard).  A pure authorization
   policy (`safety_decision`): only `send_or_publish` can be pre-authorized
   by the instruction; payment / deletion / account changes always pause.
4. Act through the driver; the free pass fires only on
   ``effect=="confirmed" and verified`` (the driver checked the expectation
   itself over a11y — rare on the real stack, where every cua click reports
   ``unverifiable``), so the Jev verify is the real check.  Consecutive type
   steps verify together in ONE batched request with several Nouls.
5. Stall/loop detection via dhash ring (cu.stall).
6. Recover: retry once -> replan once (cheap) -> escalate THAT ONE step to a
   strong vision model (max 2 per task) -> blocked.
7. One final Jev verify of the overall goal (the agent's own belief; the
   bench judge is separate and authoritative).

Every model call is accounted (model, tokens, USD at list price, latency).
"""
from __future__ import annotations

import base64
import json
import math
import re
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any

from hermes_loopguard.cu import frames, pricing
from hermes_loopguard.cu.driver import ActionResult, ComputerDriver, Element, Frame
from hermes_loopguard.cu.drivers.cua import best_match
from hermes_loopguard.cu.stall import StallDetector
from hermes_loopguard.cu.vision import VisionError, VisionGate, VisionUsage
from hermes_loopguard.questions import Noul

__all__ = [
    "Step",
    "StepRecord",
    "RunResult",
    "SafetyDecision",
    "run_task",
    "plan",
    "escalate",
    "ground",
    "authorized_categories",
    "safety_decision",
    "PLANNER_MAX_STEPS",
]

TERMINAL_STATES = ("completed", "paused_for_approval", "stalled", "blocked", "budget_exhausted")

# Safety policy: categories the user's instruction may pre-authorize (all
# other risky categories — payment, deletion, account/security changes — are
# ALWAYS asked about, never pre-authorized).
PRE_AUTHORIZABLE = ("send_or_publish",)
_SEND_OR_PUBLISH_RE = re.compile(
    r"\b(submit|sends?|publish|posts?|buy|buying|purchase|purchasing|checkout|order|share|"
    r"save|apply|confirm|join|register|signup|sign\s?up|enroll)\b"
)
PLANNER_MAX_STEPS = 12
# Actions whose safety gate runs before the action (scroll is excluded: it
# cannot change app state, only the viewport).
_SAFETY_GATED = ("click", "type", "key")
_PLANNER_REF = "screen"  # fixed image reference for batched verifications

# Keys whose effect lives entirely in the clipboard/selection and therefore
# has NO visible outcome: the screen cannot prove or refute them, so they
# are neither verified on screen nor counted as no-ops/stalls; the next
# visible step (the paste that lands the cut/copied file) carries the check.
INVISIBLE_KEYS = ("ctrl+x", "ctrl+c", "ctrl+insert", "shift+insert", "shift+del")


def _is_invisible_key(keys: str | None) -> bool:
    """True for clipboard-only key combos (cut/copy): no visible outcome."""
    norm = "+".join(p.strip().lower() for p in str(keys or "").split("+") if p.strip())
    return norm in INVISIBLE_KEYS


# --- planner steps --------------------------------------------------------------


@dataclass
class Step:
    action: str  # click | type | key | scroll | wait
    target: str = ""
    text: str | None = None
    keys: str | None = None
    expect: str = ""  # visible outcome used by the verify gate
    direction: str = "down"
    amount: int = 3

    def sig(self) -> str:
        out = self.action
        if self.target:
            out += f":{self.target}"
        if self.action == "type" and self.text:
            out += f"={self.text}"
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        d = d if isinstance(d, dict) else {}
        action = str(d.get("action", "")).strip().lower()
        if action not in ("click", "type", "key", "scroll", "wait"):
            action = ""
        try:
            amount = max(1, min(int(d.get("amount", 3) or 3), 50))
        except (TypeError, ValueError):
            amount = 3
        return cls(
            action=action,
            target=str(d.get("target", "") or "").strip(),
            text=d.get("text"),
            keys=d.get("keys"),
            expect=str(d.get("expect", "") or "").strip(),
            direction=str(d.get("direction", "down") or "down"),
            amount=amount,
        )


def _steps_from_json(content: str) -> list[Step]:
    """Parse the planner's JSON answer (tolerates prose around the object)."""
    match = re.search(r"\{.*\}", content or "", re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    raw_steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(raw_steps, list):
        return []
    steps = [s for s in (Step.from_dict(r) for r in raw_steps) if s.action]
    return steps[:PLANNER_MAX_STEPS]


# --- run bookkeeping -------------------------------------------------------------


@dataclass
class StepRecord:
    idx: int
    action: str
    target: str
    grounding: str = "none"  # a11y | box | zoom | escalated | none
    box: tuple[float, float, float, float] | None = None
    point: tuple[int, int] | None = None
    verdict: str = "pending"  # free | pass | fail | error | noop | paused
    noul: float | None = None
    effect: str = ""
    stall: str = ""  # comma-joined stall flags
    safety: str = ""  # safety category (or "error")
    usages: list[VisionUsage] = field(default_factory=list)
    frame_before: str | None = None
    frame_after: str | None = None
    before_hash: int | None = None  # dhash of the pre-action frame (flush use)


@dataclass
class RunResult:
    state: str
    steps: list[StepRecord] = field(default_factory=list)
    calls_per_model: dict[str, int] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    wall_s: float = 0.0
    big_model_calls: int = 0
    big_model_avoided: int = 0
    reason: str = ""
    done_check: float | None = None
    # Reasons recorded when a cheaper model was skipped before a costlier one
    # answered (VisionUsage.fallbacks across the whole run).  Empty on a clean
    # cheap-path run; the bench flags rows that fell back off the Jev chain.
    fallbacks: list[str] = field(default_factory=list)
    # Vision-gate calls per model (planner text calls excluded).  A non-jev
    # model here means the run did NOT drive on the cheap path alone.
    vision_calls_by_model: dict[str, int] = field(default_factory=dict)


class _Totals:
    """Accumulates every model call (planner, gate, escalator)."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.vision_calls: dict[str, int] = {}  # gate/escalation calls only
        self.fallbacks: list[str] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.usd = 0.0

    def add(self, usage: VisionUsage | None, *, vision: bool = True) -> None:
        if usage is None:
            return
        model = usage.model or "failed"
        self.calls[model] = self.calls.get(model, 0) + 1
        if vision and usage.model:
            self.vision_calls[usage.model] = self.vision_calls.get(usage.model, 0) + 1
        if usage.fallbacks:
            self.fallbacks.extend(usage.fallbacks)
        self.input_tokens += int(usage.input_tokens or 0)
        self.output_tokens += int(usage.output_tokens or 0)
        self.usd += float(usage.usd or 0.0)

    def big_model_calls(self) -> int:
        return sum(n for m, n in self.calls.items() if "opus" in m or "thinking" in m)


# --- safety policy (pure) ---------------------------------------------------------


@dataclass
class SafetyDecision:
    pause: bool
    reason: str = ""
    # True when stage 1 left a risky category in the ambiguous band and the
    # caller must run the stage-2 action-level check and re-decide.  `pause`
    # is ALSO True in that case: ignoring needs_stage2 must fail safe.
    needs_stage2: bool = False


def authorized_categories(instruction: str) -> set[str]:
    """Categories the instruction EXPLICITLY asks for.

    Only `send_or_publish` is ever pre-authorizable ("submit the form",
    "send the message", "click Save" — the user explicitly asks for the
    committing action).  Payment, deletion and account/security changes are
    NEVER pre-authorized.
    """
    text = (instruction or "").lower()
    if text and _SEND_OR_PUBLISH_RE.search(text):
        return {"send_or_publish"}
    return set()


def _pre_authorized(category: str, authorized: set[str]) -> bool:
    return category in PRE_AUTHORIZABLE and category in authorized


def safety_decision(
    stage1: Any,
    stage2: Any,
    authorized: set[str],
    thresholds: dict,
) -> SafetyDecision:
    """Pure two-stage safety policy, still biased to ask.

    stage1: gate.safety result (screen-level Choice + confirm-dialog Noul),
    or None when the gate totally failed.  stage2: gate.safety_action result
    (per-category action-level Nouls), or None when it has not run yet.
    thresholds needs "safety" (the low pause bar) and "safety_hard".

    - Gate errors at either stage pause.
    - A risky category at or above safety_hard pauses WITHOUT stage 2.
    - A risky category in the ambiguous band [safety, safety_hard) needs the
      stage-2 action-level check: pause when the Noul for THAT category is
      >= safety; with stage 2 not run yet, return pause + needs_stage2 so the
      caller fetches it and re-decides (skipping stage 2 must fail safe).
    - `send_or_publish` in the band is skipped when the instruction explicitly
      asked for it; payment / deletion / account_or_security_change are never
      pre-authorized.
    """
    safety = float((thresholds or {}).get("safety", 0.2))
    hard = float((thresholds or {}).get("safety_hard", 0.6))
    if stage1 is None or getattr(stage1, "error", None):
        error = getattr(stage1, "error", None) or "no safety result"
        return SafetyDecision(pause=True, reason=f"safety gate error, asking a human: {error}")
    probs = getattr(stage1, "probs", {}) or {}
    risky = sorted(
        ((c, float(p)) for c, p in probs.items() if c != "none" and float(p) >= safety),
        key=lambda cp: -cp[1],
    )
    for category, prob in risky:
        if prob >= hard and not _pre_authorized(category, authorized):
            return SafetyDecision(
                pause=True,
                reason=(
                    f"{category} risk {prob:.2f} >= hard threshold {hard:.2f} "
                    "on this screen; pausing without a second opinion"
                ),
            )
    band = [
        (c, p) for c, p in risky if p < hard and not _pre_authorized(c, authorized)
    ]
    if not band:
        return SafetyDecision(pause=False)
    if stage2 is None:
        return SafetyDecision(
            pause=True,
            reason="stage-1 risk is ambiguous; the action-level check has not run yet",
            needs_stage2=True,
        )
    if getattr(stage2, "error", None):
        return SafetyDecision(
            pause=True,
            reason=f"stage-2 action gate error, asking a human: {stage2.error}",
        )
    action_probs = getattr(stage2, "probs", {}) or {}
    for category, prob in band:
        action_prob = float(action_probs.get(category, 0.0))
        if action_prob >= safety:
            return SafetyDecision(
                pause=True,
                reason=(
                    f"{category} screen-level risk {prob:.2f} is ambiguous and the "
                    f"action-level check agrees ({action_prob:.2f} >= {safety:.2f})"
                ),
            )
    return SafetyDecision(pause=False)


# --- planner (one cheap text call) -------------------------------------------------


_PLANNER_SYSTEM = (
    "You plan computer-use steps for an agent that drives a desktop. Answer with ONLY a JSON "
    "object: {\"steps\": [{\"action\": \"click|type|key|scroll|wait\", \"target\": \"<exact visible "
    "label of the element>\", \"text\": \"<text to type, type steps only>\", \"keys\": \"<key name, "
    "key steps only>\", \"expect\": \"<visible outcome that proves the step worked>\"}]}. "
    f"Maximum {PLANNER_MAX_STEPS} steps. Rules: "
    "- To fill a text field use ONE step with action \"type\" (the agent clicks the field and "
    "types `text`); use \"click\" only for buttons, checkboxes, links and menus. "
    "- In a window with one obvious text area and no field labels (e.g. a fresh text-editor "
    "document), use ONE \"type\" step with an EMPTY target: the agent types into the focused "
    "window. "
    "- `target` is the exact visible label text WITHOUT the role prefix (the element shown as "
    "`entry 'Name'` has target \"Name\"). "
    "- `expect` must be visible on a screenshot afterwards, e.g. \"the Name field shows Ada "
    "Lovelace\". Use action \"key\" with keys like \"Return\" or \"ctrl+a\". "
    "- When typing into a rename or save-as name field, make `expect` the OUTCOME (the new "
    "name shown in the file list, the window title), not the text inside the field: such "
    "fields often close as soon as the step is confirmed. "
    "- To move or copy a file in a file manager: click the file to select it, press key "
    "\"ctrl+x\" (or \"ctrl+c\"), click the destination folder, then press key \"ctrl+v\"; set "
    "the expect of that final paste to the file appearing in the destination. "
    "- To rename an item in a file manager: click the item, press key \"F2\", then ONE type "
    "step with the NEW name (target = the item's current name; the field opens with the old "
    "name preselected, so do not add select-all or extra clicks between F2 and typing), then "
    "key \"Return\"; set the expect of the type step to the new name shown in the list. "
    "- A step whose outcome shows no visible change (saving, selecting) must NOT claim one: "
    "give it the smallest honestly-checkable expect or none. "
    "- Ignore window-manager panels, docks and browser toolbars; act only in the app window "
    "the task is about."
)


def _planner_backend(cfg: dict):
    from hermes_loopguard.backends.openai_compat import OpenAICompatBackend

    return OpenAICompatBackend(
        {"openai": {**cfg.get("openai", {}), "timeout": cfg.get("timeout_s", 30)}}
    )


def _element_labels(driver: ComputerDriver, limit: int = 60) -> str:
    try:
        elements = driver.elements()
    except Exception:
        elements = []
    if not elements:
        return "(accessibility tree unavailable)"
    return "\n".join(
        f"- {el.role} {el.label!r} at ({el.center[0]}, {el.center[1]})"
        + (f" app={el.app}" if el.app else "")
        for el in elements[:limit]
    )


def plan(
    instruction: str,
    driver: ComputerDriver,
    cfg: dict,
    *,
    model: str | None = None,
    failure_evidence: str | None = None,
) -> tuple[list[Step], VisionUsage]:
    """ONE cheap text call: instruction + AT-SPI labels -> steps.  No screenshot."""
    model = model or str(cfg.get("planner_model", "gemini-3.1-flash-lite"))
    backend = _planner_backend(cfg)
    user = f"Task: {instruction}\n\nOn-screen elements (accessibility tree):\n{_element_labels(driver)}"
    if failure_evidence:
        user += f"\n\nA previous plan failed: {failure_evidence}\nPlan the remaining work differently."
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _PLANNER_SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
    }
    started = time.perf_counter()
    try:
        resp = backend._post(payload)
        content = str(resp["choices"][0]["message"]["content"])
        usage = backend._calc_usage(resp.get("usage", {}))
    except Exception as e:
        return [], VisionUsage(backend="openai", model=model, fallbacks=[f"planner call failed: {e}"])
    steps = _steps_from_json(content)
    planner_usage = VisionUsage(
        backend="openai",
        model=model,
        input_tokens=int(usage.input_tokens or 0),
        output_tokens=int(usage.output_tokens or 0),
        usd=pricing.usd(model, int(usage.input_tokens or 0), int(usage.output_tokens or 0)),
        latency_s=time.perf_counter() - started,
    )
    return steps, planner_usage


# --- escalation (strong vision model, one step) --------------------------------------


_ESCALATION_SYSTEM = (
    "You are a computer-use expert. ONE action is failing. Look at the screenshot and answer "
    "with ONLY a JSON object for that single action: "
    '{"action": "click|type|key", "x": <pixel x>, "y": <pixel y>, "text": "<if type>", '
    '"keys": "<if key>"}. Coordinates are PIXELS in the screenshot, origin top-left.'
)


def escalate(
    frame: Frame,
    instruction: str,
    step: Step,
    cfg: dict,
    *,
    model: str | None = None,
) -> tuple[dict | None, VisionUsage]:
    """Ask the strong vision model for ONE corrective action with pixel coords."""
    cfg = cfg or {}
    esc = cfg.get("escalation") or {}
    model = model or str(esc.get("model") or "openai:claude-opus-4-6-thinking")
    explicit = model.split(":", 1)[1] if model.startswith("openai:") else model
    backend = _planner_backend(cfg)
    try:
        prepared, _w, _h = frames.prepare_image(frame.png)
    except (RuntimeError, ValueError) as e:
        return None, VisionUsage(backend="openai", model=explicit, fallbacks=[f"prepare_image: {e}"])
    ref = _uuid.uuid4().hex
    b64 = base64.b64encode(prepared).decode("ascii")
    payload = {
        "model": explicit,
        "messages": [
            {"role": "system", "content": _ESCALATION_SYSTEM},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Task: {instruction}\nFailing step: {json.dumps(step.__dict__, default=str)}",
                    },
                    {"type": "text", "text": f"Image `{ref}`:"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            },
        ],
        "temperature": 0,
    }
    started = time.perf_counter()
    try:
        resp = backend._post(payload)
        content = str(resp["choices"][0]["message"]["content"])
        usage = backend._calc_usage(resp.get("usage", {}))
    except Exception as e:
        return None, VisionUsage(
            backend="openai", model=explicit, fallbacks=[f"escalation call failed: {e}"]
        )
    esc_usage = VisionUsage(
        backend="openai",
        model=explicit,
        input_tokens=int(usage.input_tokens or 0),
        output_tokens=int(usage.output_tokens or 0),
        usd=pricing.usd(explicit, int(usage.input_tokens or 0), int(usage.output_tokens or 0)),
        latency_s=time.perf_counter() - started,
    )
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return None, esc_usage
    try:
        action = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None, esc_usage
    if not isinstance(action, dict) or action.get("action") not in ("click", "type", "key"):
        return None, esc_usage
    return action, esc_usage


# --- grounding -------------------------------------------------------------------


_ZOOM_MIN_SIDE = 256  # TypeSafe image minimum per side
_ZOOM_MIN_PIXELS = 78_400  # TypeSafe image minimum total pixels


def _pad_region(box: tuple[float, float, float, float], w: int, h: int, pad: float = 0.25):
    """Padded desktop-px crop around a detected box, grown to the minimum
    image size the vision API accepts.  A tiny detected box (e.g. a short
    link) would otherwise produce a sub-minimum crop that prepare_image
    rejects, killing the zoomed retry."""
    left, top, right, bottom = box
    bw, bh = (right - left) * w, (bottom - top) * h
    x0 = max(0, left * w - bw * pad)
    y0 = max(0, top * h - bh * pad)
    x1 = min(w, right * w + bw * pad)
    y1 = min(h, bottom * h + bh * pad)
    cw, ch = x1 - x0, y1 - y0
    if cw <= 0 or ch <= 0:
        return None
    # Grow (keeping the region's aspect) until the TypeSafe minimums hold.
    need_w = max(cw, float(_ZOOM_MIN_SIDE), (_ZOOM_MIN_PIXELS * cw / ch) ** 0.5)
    need_h = max(ch, float(_ZOOM_MIN_SIDE), (_ZOOM_MIN_PIXELS * ch / cw) ** 0.5)
    if need_w > cw or need_h > ch:
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        x0, x1 = cx - need_w / 2.0, cx + need_w / 2.0
        y0, y1 = cy - need_h / 2.0, cy + need_h / 2.0
        # Slide, don't shrink, when the grown region overflows the frame.
        if x0 < 0:
            x0, x1 = 0.0, need_w
        if y0 < 0:
            y0, y1 = 0.0, need_h
        if x1 > w:
            x0, x1 = float(max(0, w - need_w)), float(w)
        if y1 > h:
            y0, y1 = float(max(0, h - need_h)), float(h)
    region = (
        max(0, int(x0)),
        max(0, int(y0)),
        min(w, int(math.ceil(x1))),
        min(h, int(math.ceil(y1))),
    )
    # int() truncation can shave a hair below the pixel minimum (REAL failure:
    # a 289x271 crop = 78,319 px < 78,400 killed the zoom retry); grow back
    # within the frame if the clamp allows it.
    while (region[2] - region[0]) * (region[3] - region[1]) < _ZOOM_MIN_PIXELS and (
        region[2] < w or region[3] < h
    ):
        if region[2] < w:
            region = (region[0], region[1], region[2] + 1, region[3])
        if region[3] < h:
            region = (region[0], region[1], region[2], region[3] + 1)
    if region[2] - region[0] < 4 or region[3] - region[1] < 4:
        return None
    return region


# AT-SPI roles that accept keyboard input, most specific first.  A `type`
# step should prefer these over a same-named static label: clicking a label
# ("Feedback" heading) never focuses the field under it.  ("text" covers GTK
# entries whose walk exposes an empty label, e.g. a Save-As name field.)
# Roles are matched EXACTLY (role_is_editable): a substring rule would let
# "text" match AT-SPI's "static text" — a plain label is not an editable.
EDITABLE_ROLES = (
    "entry",
    "text box",
    "textbox",
    "text area",
    "textarea",
    "text",
    "password text",
    "spin button",
    "combo box",
)

_EDITABLE_ROLE_SET = frozenset(r.strip().lower() for r in EDITABLE_ROLES)


def role_is_editable(role: str) -> bool:
    """True when the AT-SPI role name IS an editable role (exact match per
    role name, whitespace/case-insensitive)."""
    return role.strip().lower() in _EDITABLE_ROLE_SET


# Browsers expose their web-content a11y tree only from the second walk on
# (cua-driver quirk): only these app drivers justify extra walks when an
# in-memory match over the first walk finds nothing.
_BROWSER_APPS = ("chrome", "chromium", "brave", "edge", "firefox")


def _is_browser_driver(driver: ComputerDriver) -> bool:
    app = str(getattr(driver, "app", "") or "").lower()
    return any(name in app for name in _BROWSER_APPS)


def _walk_elements(driver: ComputerDriver) -> list[Element]:
    """ONE fresh AT-SPI walk.  A walk of a GTK app plus its dialog is SLOW
    (~20 s on this stack), so grounding must never walk repeatedly (X8: the
    per-role find_element loop cost up to ~20 min for one type step)."""
    try:
        return list(driver.elements())
    except Exception:
        return []


def _rect_distance(el: Element, x: float, y: float) -> float:
    """Distance from a desktop point to the element's rectangle (0 inside)."""
    dx = max(el.x - x, 0, x - (el.x + el.w))
    dy = max(el.y - y, 0, y - (el.y + el.h))
    return (dx * dx + dy * dy) ** 0.5


def _nearest_editable(driver: ComputerDriver, point: tuple[int, int], els: list[Element] | None = None) -> Element | None:
    """The editable a11y element closest to a Box-found point.

    REAL failure (mousepad-save): the Save-As dialog's Name entry has an
    EMPTY label in the walk, so a type step grounded by label can never find
    it; the Box locate finds the 'Name' LABEL TEXT left of the entry, and
    clicking that leaves the focus on the search box / list — the typed path
    lands wherever focus was.  The editable field sits on the same row as the
    label the planner meant; pick it.  `els` lets the caller reuse the walk
    it already took for this grounding (one walk per grounding, X8).
    """
    if els is None:
        els = _walk_elements(driver)
    candidates = [
        el for el in els
        if role_is_editable(el.role) and el.w > 0 and el.h > 0
    ]
    if not candidates:
        return None
    best = min(candidates, key=lambda el: _rect_distance(el, point[0], point[1]))
    # Within one row height of the located point, else the box point was
    # probably over something else entirely.
    if _rect_distance(best, point[0], point[1]) > 80:
        return None
    return best


def _best_element(els: list[Element], target: str, *, editable_only: bool) -> Element | None:
    """Best-specificity in-memory label match.  For type steps ONLY editable
    roles qualify: a static element ("Name" label, role "static text") can be
    clicked forever without focusing any field."""
    if editable_only:
        els = [el for el in els if role_is_editable(el.role)]
    return best_match(els, target)


def ground(
    driver: ComputerDriver,
    gate: VisionGate,
    frame: Frame,
    target: str,
    *,
    prefer_editable: bool = False,
) -> tuple[
    str,
    tuple[float, float, float, float] | None,
    tuple[int, int] | None,
    Element | None,
    list[VisionUsage],
]:
    """AT-SPI first (zero tokens), then Jev Box, then one zoomed retry.

    prefer_editable (type steps): only editable-role AT-SPI elements ground a
    type step, so a static label with the same text cannot shadow the field.

    ONE AT-SPI walk per grounding: roles are filtered in memory over the
    walk's elements.  Extra walks happen ONLY for the Chrome quirk (the
    web-content tree is exposed from the second walk on) — a GTK walk is far
    too slow to burn ten of them per step (X8: ~20 min for one type step).
    """
    usages: list[VisionUsage] = []
    if target:
        els = _walk_elements(driver)
        el = _best_element(els, target, editable_only=prefer_editable)
        if el is None and _is_browser_driver(driver):
            # Chrome's web tree appears only on a later walk; a couple of
            # fresh walks (which also refresh element tokens) are the ONLY
            # permitted retries.
            for _ in range(2):
                time.sleep(0.8)
                els = _walk_elements(driver)
                el = _best_element(els, target, editable_only=prefer_editable)
                if el is not None:
                    break
        if el is not None:
            return "a11y", None, el.center, el, usages
    if not target:
        return "none", None, None, None, usages
    loc = gate.locate(frame, target)
    usages.append(loc.usage)
    if loc.found and loc.center:
        # A type step's Box hit is often the field's LABEL ("Name:"); the
        # editable it belongs to sits right next to it (same row).  Ground to
        # the editable so the click focuses the FIELD, not the label.
        if prefer_editable:
            near = _nearest_editable(driver, loc.center, els=els)
            if near is not None:
                return "a11y", None, near.center, near, usages
        return "box", loc.box, loc.center, None, usages
    if loc.box:
        region = _pad_region(loc.box, frame.width, frame.height)
        if region is not None:
            retry = gate.locate(frame, target, region=region)
            usages.append(retry.usage)
            if retry.found and retry.center:
                return "zoom", retry.box, retry.center, None, usages
    return "none", loc.box, None, None, usages


def _usage_failed(usage: VisionUsage | None) -> bool:
    """True when a gate call consumed no real model answer (all models failed)."""
    return usage is not None and not usage.model and bool(usage.fallbacks)


def _safety_cached(gate: VisionGate, frame: Frame, cache: dict[int, Any]):
    """gate.safety memoized by frame dhash so unchanged screens aren't re-asked."""
    try:
        key: int | None = frames.dhash(frame.png)
    except (RuntimeError, ValueError):
        key = None
    if key is not None and key in cache:
        return cache[key]
    result = gate.safety(frame)
    if key is not None:
        cache[key] = result
        if len(cache) > 32:
            cache.pop(next(iter(cache)))
    return result


def _safety_action_cached(
    gate: VisionGate, frame: Frame, action: str, target: str, cache: dict[tuple, Any]
):
    """Stage-2 gate.safety_action memoized by (frame dhash, action, target):
    the same check on the same screen for the same step is not re-asked."""
    try:
        frame_key: int | None = frames.dhash(frame.png)
    except (RuntimeError, ValueError):
        frame_key = None
    key = (frame_key, action, target)
    if frame_key is not None and key in cache:
        return cache[key]
    result = gate.safety_action(frame, action, target)
    if frame_key is not None:
        cache[key] = result
        if len(cache) > 32:
            cache.pop(next(iter(cache)))
    return result


# --- the agent loop ----------------------------------------------------------------


def run_task(
    instruction: str,
    driver: ComputerDriver,
    gate: VisionGate,
    cfg: dict | None,
    *,
    log: Any = None,
) -> RunResult:
    cfg = dict(cfg or {})
    started = time.time()
    totals = _Totals()
    authorized = authorized_categories(instruction)
    budget = float(cfg.get("budget_usd", 0.5) or 0.5)
    max_steps = int(cfg.get("max_steps", 12) or 12)
    max_escalations = 2
    thresholds = gate.thresholds
    detector = StallDetector(**{k: v for k, v in (cfg.get("stall") or {}).items()})
    safety_cache: dict[int, Any] = {}
    safety_action_cache: dict[tuple, Any] = {}
    records: list[StepRecord] = []
    pending: list[tuple[Step, StepRecord]] = []  # type steps awaiting one batched verify

    run_id = None
    if log is not None:
        try:
            run_id = log.start_run("jev_computer", instruction)
        except Exception:
            run_id = None

    def _snap() -> Frame:
        return driver.screenshot()

    def _save_frames(rec: StepRecord, before: Frame, after: Frame) -> None:
        if log is None or run_id is None:
            return
        try:
            rec.frame_before = log.save_frame(run_id, rec.idx, "before", before)
            rec.frame_after = log.save_frame(run_id, rec.idx, "after", after)
        except Exception:
            pass

    def _finish(state: str, reason: str = "", done_check: float | None = None) -> RunResult:
        result = RunResult(
            state=state,
            steps=records,
            calls_per_model=dict(totals.calls),
            input_tokens=totals.input_tokens,
            output_tokens=totals.output_tokens,
            usd=totals.usd,
            wall_s=time.time() - started,
            big_model_calls=totals.big_model_calls(),
            # A baseline big-model loop pays a screenshot turn for every step;
            # each step we completed on the cheap path is one of those avoided.
            big_model_avoided=sum(1 for r in records if r.verdict in ("pass", "free")),
            reason=reason,
            done_check=done_check,
            fallbacks=list(totals.fallbacks),
            vision_calls_by_model=dict(totals.vision_calls),
        )
        if log is not None and run_id is not None:
            try:
                for rec in records:
                    log.record_step(run_id, rec)
                log.finish_run(run_id, result)
            except Exception:
                pass
        return result

    def _flush(frame_now: Frame) -> list[tuple[Step, StepRecord]]:
        """Verify all pending type steps in ONE request (several Nouls)."""
        if not pending:
            return []
        batch, pending[:] = pending[:], []
        try:
            prepared, _w, _h = frames.prepare_image(frame_now.png)
        except (RuntimeError, ValueError) as e:
            for _s, rec in batch:
                rec.verdict = "error"
                rec.usages.append(VisionUsage(fallbacks=[f"prepare_image: {e}"]))
            return []
        questions = {
            f"s{i}": Noul(instructions=f"Does `{_PLANNER_REF}` show: {s.expect}?")
            for i, (s, _r) in enumerate(batch)
        }
        try:
            answers, usage = gate.ask(
                {_PLANNER_REF: prepared}, f"Screenshot `{_PLANNER_REF}`.", questions
            )
        except VisionError as e:
            totals.add(e.usage)
            for _s, rec in batch:
                rec.verdict = "error"
                if e.usage is not None:
                    rec.usages.append(e.usage)
            return []
        totals.add(usage)
        failed = []
        now_hash = None
        try:
            now_hash = frames.dhash(frame_now.png)
        except (RuntimeError, ValueError):
            now_hash = None
        for i, (s, rec) in enumerate(batch):
            noul = float(getattr(answers.get(f"s{i}"), "noul", 0.0))
            rec.noul = noul
            if noul >= thresholds["verify"]:
                rec.verdict = "pass"
                sig_verdicts[s.sig()] = "pass"
            elif (
                now_hash is not None
                and rec.before_hash is not None
                and frames.hamming(rec.before_hash, now_hash)
                <= detector.unchanged_max
            ):
                # Screen unchanged since the type happened: the field's state
                # is not visible (e.g. typing behind a modal dialog), not a
                # visible failure.  Don't fail, don't re-queue.
                rec.verdict = "unverified"
                sig_verdicts[s.sig()] = "unverified"
            else:
                rec.verdict = "fail"
                sig_verdicts[s.sig()] = "fail"
                failed.append((s, rec))
        return failed

    # 1. Plan (one cheap text call, zero screenshots).
    steps, usage = plan(instruction, driver, cfg)
    totals.add(usage, vision=False)
    queue: list[Step] = list(steps)
    if not queue:
        # Flash-Lite sometimes answers an empty/invalid steps list on the
        # FIRST planning call (jev-x9 mousepad-save r2: blocked at 0 steps /
        # 1.3 s, unmarked).  Replan ONCE; if the second plan is also empty
        # the run is an infrastructure error (empty_planner_response), not a
        # task failure, so bench summaries count it separately.
        steps, usage = plan(instruction, driver, cfg, failure_evidence="empty steps list")
        totals.add(usage, vision=False)
        queue = list(steps)
        if not queue:
            return _finish("infra_error", "empty_planner_response")

    try:
        frame = _snap()
    except Exception as e:
        return _finish("blocked", f"driver screenshot failed: {e}")

    state = "completed"
    reason = ""
    step_no = 0
    step_failures = 0
    replanned = False
    goal_replanned = False
    escalations_used = 0
    flush_failures = 0
    type_attempts: dict[str, int] = {}  # step.sig() -> attempts so far
    sig_verdicts: dict[str, str] = {}  # step.sig() -> latest final verdict

    def _escalate_act(step: Step, rec: StepRecord, frame_before: Frame) -> bool:
        """Escalate THIS ONE failing step to the strong vision model and act."""
        action, esc_usage = escalate(frame_before, instruction, step, cfg)
        totals.add(esc_usage)
        rec.usages.append(esc_usage)
        if action is None:
            return False
        kind = str(action.get("action"))
        x, y = int(action.get("x", 0) or 0), int(action.get("y", 0) or 0)
        rec.grounding = "escalated"
        rec.point = (x, y)
        if kind == "click":
            res = driver.click(x, y)
        elif kind == "type":
            driver.click(x, y)
            time.sleep(0.3)
            driver.key("ctrl+a")  # replace, don't append (idempotent retry)
            time.sleep(0.15)
            res = driver.type_text(str(action.get("text") or step.text or ""))
        else:
            res = driver.key(str(action.get("keys") or step.keys or ""))
        rec.effect = res.effect or ("error" if res.error else "")
        time.sleep(0.6)
        try:
            after = _snap()
        except Exception:
            return False
        _save_frames(rec, frame_before, after)
        if step.expect:
            verify = gate.verify(after, step.expect, before=frame_before)
            totals.add(verify.usage)
            rec.usages.append(verify.usage)
            rec.noul = verify.yes
            rec.verdict = "pass" if verify.passed else ("error" if _usage_failed(verify.usage) else "fail")
            return rec.verdict == "pass"
        rec.verdict = "pass"
        return True

    def _filter_replan(new_steps: list[Step]) -> list[Step]:
        """Drop replanned steps that EXACTLY repeat a step that already
        verified (pass/free).  REAL failure (settings-gtk): the replanner
        repeated the already-run "Dark mode" toggle, UNDOING it, and the
        re-save then committed the wrong state."""
        passed = {sig for sig, v in sig_verdicts.items() if v in ("pass", "free")}
        return [s for s in new_steps if s.sig() not in passed]

    def _handle_failure(step: Step, rec: StepRecord, frame_before: Frame) -> bool:
        """Retry once -> replan once -> escalate that one step -> blocked.

        Returns True when the run must go terminal ('blocked').
        """
        nonlocal queue, step_failures, replanned, escalations_used, state, reason
        if step_failures == 1:
            queue.insert(0, step)  # one cheap retry of the same step
            return False
        if not replanned:
            replanned = True
            evidence = (
                f"step {step.action} on {step.target!r} failed twice "
                f"(effect={rec.effect}, verdict={rec.verdict}, noul={rec.noul})"
            )
            new_steps, plan_usage = plan(instruction, driver, cfg, failure_evidence=evidence)
            totals.add(plan_usage, vision=False)
            new_steps = _filter_replan(new_steps)
            if new_steps:
                queue[:] = new_steps
                step_failures = 0
                return False
        if escalations_used < max_escalations and step.action in ("click", "type"):
            escalations_used += 1
            if _escalate_act(step, rec, frame_before):
                step_failures = 0  # drop back to cheap for the remaining steps
                return False
        state = "blocked"
        reason = f"step {step.action} on {step.target!r} failed twice"
        return True

    while True:
        # Budget cap first: never start a step we cannot pay for.
        if totals.usd >= budget:
            state, reason = "budget_exhausted", f"spend ${totals.usd:.4f} >= budget ${budget:.2f}"
            break

        if not queue:
            # Out of steps: flush the batched field checks, then the done check.
            try:
                frame = _snap()
            except Exception as e:
                state, reason = "blocked", f"driver screenshot failed: {e}"
                break
            failed = _flush(frame)
            if failed:
                names = ", ".join(s.target or s.action for s, _r in failed)
                state, reason = "blocked", f"field verification failed: {names}"
                break
            final = gate.verify(
                frame,
                f"the overall task is done: {instruction} — judge ONLY by what is "
                "visible on the screen now; an action that is already complete "
                "and left no trace (a click on a button, a saved file) must not "
                "count against it",
            )
            totals.add(final.usage)
            if final.passed:
                return _finish("completed", done_check=final.yes)
            # The goal check says the task is NOT done.  A short plan is the
            # usual cause (the cheap planner under-plans multi-step tasks);
            # replan ONCE with that evidence before going terminal.
            if not goal_replanned:
                goal_replanned = True
                new_steps, plan_usage = plan(
                    instruction,
                    driver,
                    cfg,
                    failure_evidence=(
                        "the planned steps all ran, but the overall goal check "
                        "says the goal is NOT met yet; look at what is still "
                        "missing on screen and plan ONLY the remaining work. "
                        "If the screen already shows everything done, return "
                        "an empty steps list. NEVER repeat a toggle or "
                        "checkbox step that already ran: clicking it again "
                        "UNDOES it."
                    ),
                )
                totals.add(plan_usage, vision=False)
                new_steps = _filter_replan(new_steps)
                if new_steps:
                    queue[:] = new_steps
                    continue
            state, reason = "blocked", f"final goal check failed (noul={final.yes:.2f})"
            break

        if step_no >= max_steps:
            state, reason = "blocked", f"max_steps={max_steps} reached"
            break

        step = queue.pop(0)
        if step.action == "wait":
            step_no += 1
            records.append(StepRecord(idx=step_no - 1, action="wait", target=step.target, verdict="pass"))
            time.sleep(min(max(float(step.amount or 2), 0.1), 10.0))
            frame = _snap()
            continue

        step_no += 1
        rec = StepRecord(idx=step_no - 1, action=step.action, target=step.target)
        records.append(rec)

        # Refresh the frame we ground and act on.
        try:
            frame = _snap()
        except Exception as e:
            state, reason = "blocked", f"driver screenshot failed: {e}"
            break

        # 2. Ground (AT-SPI free, then Jev Box, then one zoomed retry).
        el: Element | None = None
        point: tuple[int, int] | None = None
        # A type step WITHOUT a target types straight into the focused window
        # (e.g. a freshly opened editor with its document focused): there is
        # nothing to ground and clicking would only guess.
        needs_point = step.action in ("click", "type") or (
            step.action == "scroll" and step.target
        )
        if step.action == "type" and not step.target:
            needs_point = False
        if needs_point:
            grounding, box, point, el, usages = ground(
                driver, gate, frame, step.target, prefer_editable=(step.action == "type")
            )
            for u in usages:
                totals.add(u)
                rec.usages.append(u)
            rec.grounding, rec.box, rec.point = grounding, box, point
            if point is None:
                rec.verdict, rec.effect = "fail", "grounding failed"
                step_failures += 1
                if _handle_failure(step, rec, frame):
                    break
                continue
        else:
            rec.grounding = "none"

        # 3. Safety gate before mutating actions (stage 1 cached by frame
        # dhash; stage 2, when the policy needs it, by dhash+action+target).
        if step.action in _SAFETY_GATED:
            sf = _safety_cached(gate, frame, safety_cache)
            if sf.usage is not None:
                totals.add(sf.usage)
                rec.usages.append(sf.usage)
            rec.safety = "error" if sf.error else (sf.category or "none")
            decision = safety_decision(sf, None, authorized, thresholds)
            if decision.needs_stage2:
                sa = _safety_action_cached(
                    gate, frame, step.action, step.target, safety_action_cache
                )
                if sa.usage is not None:
                    totals.add(sa.usage)
                    rec.usages.append(sa.usage)
                decision = safety_decision(sf, sa, authorized, thresholds)
            if decision.pause:
                rec.verdict = "paused"
                _flush(frame)  # record any pending field checks against this frame
                return _finish(
                    "paused_for_approval",
                    f"{decision.reason}; frame={rec.frame_after or frame.path or ''}",
                )

        # Flush batched field checks BEFORE the screen changes again (e.g.
        # verify the typed form fields before the Submit click navigates).
        if pending and step.action in ("click", "key", "scroll"):
            failed = _flush(frame)
            if failed:
                flush_failures += 1
                if flush_failures >= 3:
                    state, reason = "blocked", "field verification kept failing"
                    break
                queue[0:0] = [s for s, _r in failed] + [step]
                continue

        # 4. Act.
        if step.action == "click":
            res = driver.click_element(el) if el is not None else driver.click(point[0], point[1])
        elif step.action == "type":
            # X9: when grounding found an a11y editable element, deliver the
            # text with AT-SPI set_value FIRST — it REPLACES the element's
            # whole value with no keystrokes and no focus change, which is
            # exactly what the flaky XSendEvent path keeps losing (GTK inline
            # rename drops its preselection; the Save-As field never got the
            # text).  Focus+type_text stays as the fallback for a set_value
            # error and as THE path for Box-grounded steps.
            res: ActionResult | None = None
            if el is not None and role_is_editable(el.role):
                res = driver.set_value(el.token, str(step.text or ""))
            if res is None or not res.ok or res.effect == "error":
                # The previous step was a KEY step (e.g. F2/rename opens an
                # inline edit with the text preselected): the field is already
                # focused and clicking it again only moves the caret / drops the
                # selection — type straight into the focused widget.
                after_key = len(records) >= 2 and records[-2].action == "key"
                if not after_key:
                    if el is not None:
                        driver.click_element(el)
                    elif point is not None:
                        driver.click(point[0], point[1])
                    time.sleep(0.3)
                # Fill semantics on RETRIES: select whatever is in the field and
                # replace it (typing alone appends, so a retried step would
                # duplicate text).  On the FIRST attempt the field starts empty
                # (forms) or with the text preselected (inline rename), and the
                # select-all is actively harmful there (GTK icon views swallow
                # ctrl+a and the preselection is lost — typing then appends).
                # A SUCCESSFUL set_value never lands here: it is replace-
                # semantics already, so retries of it are idempotent.
                if type_attempts.get(step.sig(), 0) >= 1:
                    driver.key("ctrl+a")
                    time.sleep(0.15)
                type_attempts[step.sig()] = type_attempts.get(step.sig(), 0) + 1
                res = driver.type_text(str(step.text or ""))
        elif step.action == "key":
            res = driver.key(str(step.keys or ""))
        elif step.action == "scroll":
            x, y = point if point else (frame.width // 2, frame.height // 2)
            res = driver.scroll(x, y, step.direction, step.amount)
        else:  # unreachable: Step.from_dict filters actions
            res = ActionResult(ok=False, effect="error", error=f"unknown action {step.action}")
        rec.effect = res.effect or ("error" if res.error else "")

        time.sleep(0.6)  # let the UI settle before the after-shot
        try:
            after = _snap()
        except Exception as e:
            state, reason = "blocked", f"driver screenshot failed: {e}"
            break
        _save_frames(rec, frame, after)

        # Frame hashes (needed by the verdict above AND the stall detector).
        before_hash = frames.dhash(frame.png)
        after_hash = frames.dhash(after.png)
        rec.before_hash = before_hash

        # 5. Verdict: free path -> batched verify (type) -> Jev verify.
        ok = True
        if step.action == "key" and _is_invisible_key(step.keys):
            # Clipboard-only combo (cut/copy): the screen can never show the
            # outcome, so don't verify it and don't let the unchanged frame
            # count as a no-op/stall — the paste carries the real check.
            rec.verdict = "free"
        elif res.effect == "confirmed" and res.verified is True and step.expect:
            # Free path: the driver itself confirmed the a11y-checkable outcome.
            rec.verdict = "free"
        elif step.action == "type" and step.expect:
            rec.verdict = "pending"
            pending.append((step, rec))
        elif step.expect:
            verify = gate.verify(after, step.expect, before=frame)
            totals.add(verify.usage)
            rec.usages.append(verify.usage)
            rec.noul = verify.yes
            if verify.passed:
                rec.verdict = "pass"
            elif _usage_failed(verify.usage):
                rec.verdict = "error"
            elif frames.hamming(before_hash, after_hash) <= detector.unchanged_max:
                # Verify said no, but the screen did not change AT ALL: the
                # step's outcome is not visible (cut, save, submit behind a
                # dialog).  That is an INVISIBLE outcome, not a visible
                # failure — do not burn the retry ladder on it; the final
                # goal check and the (code) judge stay authoritative.
                rec.verdict = "unverified"
            else:
                rec.verdict = "fail"
        else:
            rec.verdict = "unverified"

        if rec.verdict != "pending":
            sig_verdicts[step.sig()] = rec.verdict
        if rec.verdict == "fail":
            ok = False

        # 5b. Stall/loop detection (hash first, Jev only when ambiguous).
        # dhash cannot see small changes (typed text, focus rings), so when
        # the step already verified (or is pending its batched verify) the
        # screen counts as changed; the verify is the authority.  An
        # "unverified" step (invisible outcome) also counts as progress: a
        # commit step that legitimately shows nothing must not accumulate
        # staleness.
        changed = None
        # "unverified" with an `expect` means the verify ran, said no, and the
        # screen did not change at all (an invisible outcome): progress, not
        # staleness.  An expectation-LESS step with an unchanged screen is the
        # classic no-op the detector exists to catch.
        if rec.verdict in ("pass", "free", "pending") or (
            rec.verdict == "unverified" and step.expect
        ):
            changed = True
        else:
            dist = (before_hash ^ after_hash).bit_count()
            if detector.unchanged_max < dist < detector.changed_min:
                ch = gate.changed(frame, after)
                totals.add(ch.usage)
                rec.usages.append(ch.usage)
                changed = ch.changed
        stall_v = detector.observe(
            step.sig(),
            before_hash,
            after_hash,
            changed=changed,
            # A clipboard-only key cannot be a no-op on screen evidence; the
            # driver's "suspected_noop" (also screen-derived) must not veto
            # the carry-forward decision above.
            force_noop=(
                res.effect == "suspected_noop"
                and not (step.action == "key" and _is_invisible_key(step.keys))
            ),
        )
        rec.stall = ",".join(f for f in ("noop", "loop", "stale") if getattr(stall_v, f))
        if stall_v.loop or stall_v.stale:
            state = "stalled"
            reason = (
                "A-B-A-B loop detected" if stall_v.loop
                else f"no screen change for {detector.stale_steps} steps"
            )
            break
        if stall_v.noop:
            ok = False
            if rec.verdict == "pending":  # drop from the batched verify
                pending[:] = [(s, r) for (s, r) in pending if r is not rec]
            if rec.verdict != "fail":
                rec.verdict = "noop"

        if not ok:
            step_failures += 1
            if _handle_failure(step, rec, frame):
                break
            continue

        step_failures = 0

    return _finish(state, reason)
