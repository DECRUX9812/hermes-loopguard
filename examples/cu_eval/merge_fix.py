#!/usr/bin/env python3
"""GT corrections + result merging for the P6B eval (no model calls here).

Stage `gt`:    fix the two checkout-dialog ground-truth rects.  The DOM dump
               (?gt=1 --dump-dom run) centers the position:fixed overlay in a
               different viewport than the --screenshot run, so the stored
               rects sit ~46px above the real buttons.  The corrected rects
               are measured from the shipped frame's pixels (red #c62831 Pay
               button; Cancel mirrored from the dump's relative geometry).
Stage `merge`: fold the settings-subset re-run (recaptured settings_gtk.png;
               the first canonical run had a black settings frame) into the
               full report, rescore the checkout rows against the corrected
               rects, recompute aggregates/sweeps/calibration/thumbnails and
               rewrite results/final/{eval-cu-results.json,eval-cu-report.txt}.

    python3 merge_fix.py gt
    python3 merge_fix.py merge
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

FINAL = HERE / "results" / "final"
SETTINGS_OUT = HERE / "results" / "final-settings"

# measured from frames/checkout_dialog.png pixels (2026-10-15)
FIXED_GT = {
    "loc-checkout-pay": [648, 442, 180, 38],
    "loc-checkout-cancel": [452, 442, 184, 38],
}
GT_HTML_FIX = {
    "confirm-pay": [648, 442, 180, 38],
    "cancel": [452, 442, 184, 38],
}
SETTINGS_CASES = {"nat-settings-darkmode", "nat-settings-save", "saf-settings"}
# canonical predictions for the native thunar/mousepad cases were made on the
# FIRST capture session's frames, which the final recapture replaced; re-run
# on the shipped frames (results/final-native) and merge
NATIVE_LOCATE_CASES = {"nat-thunar-parent", "nat-thunar-search",
                       "nat-mousepad-textarea", "nat-mousepad-filemenu"}


def stage_gt() -> int:
    gt_path = HERE / "gt_html.json"
    gt = json.loads(gt_path.read_text())
    gt["checkout_dialog"].update(GT_HTML_FIX)
    gt_path.write_text(json.dumps(gt, indent=2, sort_keys=True) + "\n")

    rows = []
    for line in (HERE / "cases_html.jsonl").read_text().splitlines():
        row = json.loads(line)
        if row["id"] in FIXED_GT:
            row["gt"] = FIXED_GT[row["id"]]
            row["note"] = (row["note"] + "; gt re-measured from frame pixels "
                           "(dump-dom centers the fixed overlay in a different "
                           "viewport than --screenshot)").lstrip("; ")
        rows.append(row)
    (HERE / "cases_html.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))
    print("gt fixed:", FIXED_GT)
    return 0


def stage_merge() -> int:
    from hermes_loopguard.cu import eval_cu

    report = json.loads((FINAL / "eval-cu-results.json").read_text())
    cases_file = Path(report["cases_file"])
    buckets = eval_cu.load_cases(cases_file)
    base = cases_file.parent
    repeat = int(report["repeat"])

    # 1) rescore cached checkout locate rows against the corrected gt
    case_by_id = {c.id: c for c in buckets["locate"]}
    for arm in ("jev", "gemini"):
        for row in report["locate"][arm]["rows"]:
            case = case_by_id.get(row["case"])
            if case is None or case.id not in FIXED_GT or case.gt is None:
                continue
            gt = tuple(FIXED_GT[case.id])
            hit = False
            if row.get("center"):
                hit = eval_cu.point_in_rect(tuple(row["center"]), gt)
            iou_v = 0.0
            if row.get("box"):
                iou_v = eval_cu.iou(eval_cu.box_to_px(tuple(row["box"]), 1280, 800), gt)
            row["hit"] = hit
            row["iou"] = iou_v
    report["notes"].append(
        "checkout-dialog gt re-measured from frame pixels; cached jev/gemini "
        "predictions rescored against the corrected rects (no new model calls)")

    # 2) replace the black-settings-frame rows with the subset re-run
    sub = json.loads((SETTINGS_OUT / "eval-cu-results.json").read_text())
    for section in ("locate", "safety"):
        for arm in ("jev", "gemini"):
            kept = [r for r in report[section][arm]["rows"] if r["case"] not in SETTINGS_CASES]
            fresh = [r for r in sub[section][arm]["rows"] if r["case"] in SETTINGS_CASES]
            report[section][arm]["rows"] = sorted(kept + fresh,
                                                  key=lambda r: (r["case"], r["repeat"]))
    report["notes"].append(
        "settings_gtk.png was captured before its window mapped (black frame) "
        "in the canonical run; the 2 settings locate cases + saf-settings were "
        "re-captured and re-run as a subset (results/final-settings), rows merged")

    # 2b) native thunar/mousepad locate rows: re-run on the SHIPPED frames
    # (the canonical predictions answered the first capture session's frames,
    # which the final recapture replaced)
    sub2 = json.loads((HERE / "results" / "final-native" / "eval-cu-results.json").read_text())
    for arm in ("jev", "gemini"):
        kept = [r for r in report["locate"][arm]["rows"] if r["case"] not in NATIVE_LOCATE_CASES]
        fresh = [r for r in sub2["locate"][arm]["rows"] if r["case"] in NATIVE_LOCATE_CASES]
        report["locate"][arm]["rows"] = sorted(kept + fresh,
                                               key=lambda r: (r["case"], r["repeat"]))
    report["notes"].append(
        "native thunar/mousepad locate cases were re-run on the shipped frames "
        "(results/final-native): the canonical predictions answered the first "
        "capture session's frames, which the privacy re-capture replaced. "
        "saf-thunar/saf-mousepad keep their canonical rows (no pauses either "
        "way, on the earlier session's frames)")

    # 3) atspi arm: the canonical run's atspi rows were measured with the
    # native apps DOWN (0% = walk artifact, every lookup hit an empty tree).
    # A clean live re-score became impossible: X7 finished and shut the bench
    # stack down (stack.sh stop ~18:50; we may not restart it).  Probe data
    # (probe_atspi.py, 18:47): when the tree IS available, lookups are exact
    # (settings + thunar match gt rects pixel-perfect on the first walk), but
    # single-shot get_window_state walks return empty/degraded trees under a
    # loaded desktop (15/15 empty at canonical-run time; mousepad 5/5 empty
    # even fresh).  run_locate_atspi now retries (3 walks, like the agent's
    # find_element) but cannot be re-scored without the stack -> not scored.
    if "atspi" in report.get("locate", {}):
        stale = report["locate"].pop("atspi")
        report["atspi_not_scored"] = {
            "reason": "bench stack shut down by X7 before a clean live re-score; "
                      "canonical-run atspi rows were an artifact (apps down)",
            "stale_rows_kept_here": len(stale.get("rows", [])),
            "probe_findings": [
                "settings + thunar lookups exact when the tree is available",
                "single-shot walks empty under load: 15/15 at canonical time",
                "run_locate_atspi now retries 3 walks (agent find_element parity)",
            ],
        }
    report["notes"].append(
        "atspi arm NOT SCORED: canonical-run atspi rows were an artifact "
        "(native apps were down: every live lookup hit an empty tree); the "
        "stack was shut down by X7 before a clean re-run. probe_atspi.py: "
        "lookups are exact when the tree is available; single-shot walks are "
        "unreliable under load (15/15 empty), hence the 3-walk retry in "
        "run_locate_atspi")

    # 4) recompute everything downstream
    for arm in ("jev", "gemini"):
        rows = report["locate"][arm]["rows"]
        report["locate"][arm]["aggregate"] = eval_cu.aggregate_locate(rows)
        report["locate"][arm]["calibration"] = eval_cu.calibration_buckets(
            [r for r in rows if not r.get("absent")])
        report["sweep"]["locate"][arm] = eval_cu.sweep_locate(rows)
        vrows = report["verify"][arm]["rows"]
        report["verify"][arm]["aggregate"] = eval_cu.aggregate_verify(vrows)
        report["sweep"]["verify"][arm] = eval_cu.sweep_verify(vrows)
        srows = report["safety"][arm]["rows"]
        report["safety"][arm]["aggregate"] = eval_cu.aggregate_safety(srows)
        report["sweep"]["safety"][arm] = eval_cu.sweep_safety(srows)

    # 4) spend + tokens: recompute from the merged rows (they already carry
    # the fresh subset rows); preflight calls are tracked in the log, the
    # reports never priced them
    spend = {"usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0}
    for s in ("locate", "verify", "safety"):
        for arm in report[s]:
            agg = report[s][arm]["aggregate"]
            spend["usd"] += float(agg.get("usd", 0.0) or 0.0)
            spend["input_tokens"] += int(agg.get("input_tokens", 0) or 0)
            spend["output_tokens"] += int(agg.get("output_tokens", 0) or 0)
            spend["calls"] += int(agg.get("n_rows", 0) or 0)
    for k in ("usd",):
        spend[k] = round(spend[k], 4)
    report["spend"] = spend

    # 5) thumbnails: wipe + regenerate from the final rows
    thumbs = FINAL / "thumbs"
    if thumbs.exists():
        shutil.rmtree(thumbs)
    report["thumbs"] = eval_cu._miss_thumbnails(report, buckets, base, thumbs)

    eval_cu._write_json(FINAL / "eval-cu-results.json", report)
    (FINAL / "eval-cu-report.txt").write_text(
        eval_cu.format_report(report) + "\n", encoding="utf-8")
    print("merged + recomputed; report:")
    print(eval_cu.format_report(report))
    return 0


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else ""
    if stage == "gt":
        raise SystemExit(stage_gt())
    if stage == "merge":
        raise SystemExit(stage_merge())
    print("usage: merge_fix.py gt|merge")
    raise SystemExit(2)
