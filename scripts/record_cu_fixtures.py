#!/usr/bin/env python3
"""Record REAL vision fixtures for the cu unit tests (spends cents).

Run with the lg-venv python (has typesafe-sdk + Pillow):

    cd /path/to/hermes-loopguard
    PYTHONPATH=src ~/Code/ship-test/lg-venv/bin/python \
        scripts/record_cu_fixtures.py

Captures real Jev (TypeSafe System One) and CLIProxy (OpenAI-compatible)
responses for a fixed set of locate/verify/safety cases over the local
screenshot fixtures, plus one real 400-style error from a Box question on
jev-1.13.0. Only request METADATA (model, headers, question shapes, token
usage) and answers are written to tests/fixtures/cu/vision/cases/*.json --
no API keys and no image bytes. Unit tests replay these recorded responses
through a fake transport.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "cu" / "vision"
CASES_DIR = FIXTURES / "cases"
sys.path.insert(0, str(REPO / "src"))

# Load secrets from ~/.hermes/.env into the environment (never printed).
_env = Path.home() / ".hermes" / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        if "=" in line and line.strip() and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

from hermes_loopguard.backends.typesafe import TypeSafeBackend  # noqa: E402
from hermes_loopguard.cu.pricing import usd  # noqa: E402
from hermes_loopguard.cu.vision import VisionGate  # noqa: E402
from hermes_loopguard.questions import Box, Noul  # noqa: E402

TOTALS = {"in": 0, "out": 0}

LOCATE_QUESTIONS = {
    "presence": {"type": "noul", "instructions": "Is there <target> in `<ref>`?"},
    "target": {"type": "box", "instructions": "<target> in `<ref>`"},
}


def load(name: str) -> bytes:
    return (FIXTURES / f"{name}.png").read_bytes()


class _Frame:  # duck-typed Frame (P2 owns the real dataclass)
    def __init__(self, png: bytes, w: int = 1280, h: int = 800):
        self.png, self.width, self.height, self.ts = png, w, h, time.time()


def _box_answer(r) -> dict | None:
    if r.box is None:
        return None
    return {
        "type": "box", "left": r.box[0], "top": r.box[1],
        "right": r.box[2], "bottom": r.box[3], "confidence": r.confidence,
    }


def dump(name: str, doc: dict) -> None:
    doc.update({"recorded": True, "case": name})
    doc.setdefault("note", "REAL recorded response, replayed by a fake transport in unit tests")
    doc.setdefault("request_headers", {"TypeSafe-Danger-Zone-Images": "true"})
    (CASES_DIR / f"{name}.json").write_text(json.dumps(doc, indent=2, sort_keys=True))


def dump_locate(name: str, r, expect: str, headers: dict | None = None) -> None:
    TOTALS["in"] += r.usage.input_tokens
    TOTALS["out"] += r.usage.output_tokens
    dump(name, {
        "backend": r.usage.backend, "model": r.usage.model, "grounding": r.grounding,
        "expect": expect, "request_headers": headers or {"TypeSafe-Danger-Zone-Images": "true"},
        "questions": LOCATE_QUESTIONS,
        "answers": {"presence": {"type": "noul", "noul": r.presence}, "target": _box_answer(r)},
        "usage": {"input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens},
        "usd_list_price": r.usage.usd, "found": r.found, "ok": r.found,
        "fallbacks": r.usage.fallbacks,
    })
    print(f"  recorded {name}: model={r.usage.model} in={r.usage.input_tokens} "
          f"out={r.usage.output_tokens} found={r.found} presence={r.presence:.3f} conf={r.confidence:.3f}")


def dump_verify(name: str, v, expect: str) -> None:
    TOTALS["in"] += v.usage.input_tokens
    TOTALS["out"] += v.usage.output_tokens
    dump(name, {
        "backend": v.usage.backend, "model": v.usage.model, "expect": expect,
        "questions": {"ok": {"type": "noul", "instructions": "Does `<ref>` show: <expected>?"}},
        "answers": {"ok": {"type": "noul", "noul": v.yes}},
        "usage": {"input_tokens": v.usage.input_tokens, "output_tokens": v.usage.output_tokens},
        "usd_list_price": v.usage.usd, "passed": v.passed, "ok": v.passed,
    })
    print(f"  recorded {name}: model={v.usage.model} in={v.usage.input_tokens} "
          f"out={v.usage.output_tokens} yes={v.yes:.3f} passed={v.passed}")


def dump_safety(name: str, s, expect: str, want_pause: bool) -> None:
    TOTALS["in"] += s.usage.input_tokens
    TOTALS["out"] += s.usage.output_tokens
    dump(name, {
        "backend": s.usage.backend, "model": s.usage.model, "expect": expect,
        "questions": {
            "category": {"type": "choice",
                         "instructions": "What kind of action is `<ref>` about to carry out on this screen?",
                         "criteria": {"payment": None, "deletion": None, "send_or_publish": None,
                                      "account_or_security_change": None, "none": None}},
            "confirm": {"type": "noul", "instructions": "Is a confirmation dialog visible in `<ref>`?"},
        },
        "answers": {
            "category": {"type": "choice", "choice": s.category,
                         "confidence": max(s.probs.values()) if s.probs else 0.0,
                         "probabilities": s.probs},
            "confirm": {"type": "noul", "noul": s.confirm_dialog},
        },
        "usage": {"input_tokens": s.usage.input_tokens, "output_tokens": s.usage.output_tokens},
        "usd_list_price": s.usage.usd, "pause": s.pause, "ok": s.pause == want_pause,
    })
    print(f"  recorded {name}: model={s.usage.model} in={s.usage.input_tokens} "
          f"out={s.usage.output_tokens} category={s.category} pause={s.pause}")


def main() -> int:
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, bool]] = []
    openai_cfg = {"openai": {"base_url": "http://127.0.0.1:8317/v1",
                             "api_key_env": "CLIPROXY_API_KEY"}, "timeout_s": 60}

    # --- CLIProxy image-input probe (one tiny call) -------------------------
    print("Probing CLIProxy image input (tiny call)...")
    tiny = FIXTURES / "tiny.png"
    if not tiny.exists():
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (300, 300), (200, 30, 30)).save(buf, format="PNG")
        tiny.write_bytes(buf.getvalue())
    from hermes_loopguard.backends.openai_compat import OpenAICompatBackend

    oa = OpenAICompatBackend({"openai": {"base_url": "http://127.0.0.1:8317/v1",
                                         "api_key_env": "CLIPROXY_API_KEY", "timeout": 60}})
    cliproxy_ok = False
    try:
        res = oa.ask(
            "A tiny test image is attached as `img`.",
            {"color": Noul(instructions="Is the image `img` mostly red?")},
            images={"img": tiny.read_bytes()},
            model="gemini-3.8-flash-high",
        )
        cliproxy_ok = True
        noul = res.answers["color"].noul
        TOTALS["in"] += res.usage.input_tokens
        TOTALS["out"] += res.usage.output_tokens
        dump("cliproxy_image_probe", {
            "backend": "openai", "model": "gemini-3.8-flash-high", "ok": True,
            "expect": "noul high: the image is a solid red square",
            "questions": {"color": {"type": "noul", "instructions": "Is the image `img` mostly red?"}},
            "answers": {"color": {"type": "noul", "noul": noul}},
            "usage": {"input_tokens": res.usage.input_tokens,
                      "output_tokens": res.usage.output_tokens},
            "usd_list_price": usd("gemini-3.8-flash-high", res.usage.input_tokens,
                                  res.usage.output_tokens),
            "note": "proves image input works on the CLIProxy OpenAI-compatible route",
        })
        print(f"  CLIProxy image input works: noul={noul:.3f} "
              f"in={res.usage.input_tokens} out={res.usage.output_tokens}")
    except Exception as e:
        dump("cliproxy_image_probe", {
            "backend": "openai", "model": "gemini-3.8-flash-high", "ok": False,
            "questions": {"color": {"type": "noul", "instructions": "Is the image `img` mostly red?"}},
            "answers": {}, "usage": {"input_tokens": 0, "output_tokens": 0},
            "note": f"image input probe FAILED: {e}",
        })
        print(f"  CLIProxy image probe FAILED: {e}")

    # --- VisionGate cases (default chain, jev-1.14.0 answers) ---------------
    gate = VisionGate(openai_cfg)
    form_f, thanks_f, payment_f = _Frame(load("form")), _Frame(load("thanks")), _Frame(load("payment"))

    print("Recording locate Submit (jev-1.14.0)...")
    r = gate.locate(form_f, "the Submit button")
    dump_locate("locate_submit_jev114", r, "found: Submit on the form")
    results.append(("locate Submit", r.found))

    print("Recording locate Email field (jev-1.14.0)...")
    r = gate.locate(form_f, "the Email text field")
    dump_locate("locate_email_jev114", r, "found: Email field on the form")
    results.append(("locate Email field", r.found))

    print("Recording locate non-existent Delete account (jev-1.14.0, expect not found)...")
    r = gate.locate(form_f, "the Delete account button")
    dump_locate("locate_delete_account_jev114", r, "NOT found: no such button on the form")
    results.append(("locate non-existent Delete account (expect low)", not r.found))

    print("Recording verify 'Thanks, Ada' (jev-1.14.0, expect pass)...")
    v = gate.verify(thanks_f, "a message thanking Ada for her order")
    dump_verify("verify_thanks_pass_jev114", v, "pass: thanks page")
    results.append(("verify Thanks-Ada (expect pass)", v.passed))

    print("Recording verify on the pre-submit frame (jev-1.14.0, expect fail)...")
    v = gate.verify(form_f, "a message thanking Ada for her order")
    dump_verify("verify_thanks_fail_jev114", v, "fail: pre-submit form page")
    results.append(("verify pre-submit frame (expect fail)", not v.passed))

    print("Recording safety on the payment page (jev-1.14.0, expect pause)...")
    s = gate.safety(payment_f)
    dump_safety("safety_payment_jev114", s, "pause: payment dialog", want_pause=True)
    results.append(("safety payment page (expect pause)", s.pause))

    print("Recording safety on the form (jev-1.14.0, expect no pause)...")
    s = gate.safety(form_f)
    dump_safety("safety_form_jev114", s, "no pause: plain form", want_pause=False)
    results.append(("safety form page (expect no pause)", not s.pause))

    # --- Box on jev-1.13.0: record the real 400 -----------------------------
    print("Recording Box on jev-1.13.0 (expect a real error)...")
    ts113 = TypeSafeBackend({"typesafe": {"timeout": 60}})
    ref = "jev113-box-probe"
    err_text = ""
    try:
        ts113.ask(
            f"Screenshot `{ref}`.",
            {"target": Box(instructions=f"the Submit button in `{ref}`")},
            images={ref: gate._prepare(load("form"))[0]},
            model="jev-1.13.0",
        )
        err_text = ""  # unexpected success; the fixture still records it
    except Exception as e:
        err_text = str(e)
    dump("box_jev113_error", {
        "backend": "typesafe", "model": "jev-1.13.0",
        "expect": "error (Box is a Jev 1.14 primitive)",
        "questions": {"target": {"type": "box", "instructions": "the Submit button in `<ref>`"}},
        "error": err_text, "ok": bool(err_text),
        "note": "REAL recorded error from jev-1.13.0 on a Box question; no key material stored",
    })
    print(f"  jev-1.13.0 Box error recorded: {err_text[:200]}")

    # --- same locate through the openai fallback model ----------------------
    if cliproxy_ok:
        print("Recording locate Submit through openai:gemini-3.8-flash-high...")
        oa_gate = VisionGate({**openai_cfg, "models": ["openai:gemini-3.8-flash-high"]})
        r = oa_gate.locate(form_f, "the Submit button")
        dump_locate("locate_submit_openai_fallback", r,
                    "found via fallback JSON grounding: Submit on the form",
                    headers={"Authorization": "Bearer <CLIPROXY_API_KEY> (present, value not stored)"})
        results.append(("locate Submit via openai fallback", r.found))

    print(f"\nToken totals: input={TOTALS['in']} output={TOTALS['out']}")
    print("Expected-outcome check:")
    for label, ok in results:
        print(f"  [{'OK' if ok else 'WRONG'}] {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
