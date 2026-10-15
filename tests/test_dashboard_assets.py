from __future__ import annotations

import ast
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import pytest

from hermes_loopguard.db import Store


ALLOWED_ICONS = {
    "Activity", "BarChart3", "Clock", "Code", "Database", "Eye",
    "FileText", "Globe", "Heart", "KeyRound", "MessageSquare", "Package",
    "Puzzle", "Settings", "Shield", "Sparkles", "Star", "Terminal",
    "Wrench", "Zap",
}

REQUIRED_MANIFEST_KEYS = {
    "name", "label", "icon", "version", "description",
    "tab", "slots", "entry", "css", "api",
}


def test_manifest_is_valid_json_and_has_required_keys():
    manifest_path = Path("src/hermes_loopguard/dashboard/manifest.json")
    assert manifest_path.is_file(), "manifest.json file does not exist"

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "manifest.json must be a JSON object"
    assert REQUIRED_MANIFEST_KEYS.issubset(data.keys()), (
        f"Missing required keys: {REQUIRED_MANIFEST_KEYS - set(data.keys())}"
    )

    assert data["name"] == "loopguard"
    assert data["icon"] in ALLOWED_ICONS, f"Icon {data['icon']} not in allowed icons list"
    assert data["tab"]["path"] == "/loopguard"
    assert "cron:top" in data["slots"]
    assert data["entry"] == "dist/index.js"
    assert data["css"] == "dist/style.css"
    assert data["api"] == "plugin_api.py"


def test_dist_index_js_structure_and_no_external_imports():
    js_path = Path("src/hermes_loopguard/dashboard/dist/index.js")
    assert js_path.is_file(), "dist/index.js file does not exist"

    text = js_path.read_text(encoding="utf-8")
    assert '__HERMES_PLUGINS__.register("loopguard"' in text, (
        'dist/index.js must register "loopguard"'
    )
    assert 'registerSlot("loopguard", "cron:top"' in text, (
        'dist/index.js must register "cron:top" slot'
    )

    # Must not contain ES module imports or CommonJS require calls
    assert not re.search(r"\bimport\s+[\w*{]", text), "dist/index.js must not contain import statements"
    assert not re.search(r"\brequire\s*\(", text), "dist/index.js must not contain require(...) calls"


def test_style_css_classes_all_prefixed_with_lg():
    css_path = Path("src/hermes_loopguard/dashboard/dist/style.css")
    assert css_path.is_file(), "dist/style.css file does not exist"

    text = css_path.read_text(encoding="utf-8")
    # Strip comments
    text_no_comments = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)

    # Match class selectors
    class_selectors = set(re.findall(r"\.([a-zA-Z][a-zA-Z0-9_-]*)", text_no_comments))
    assert len(class_selectors) > 0, "dist/style.css must define classes"

    non_lg_classes = [cls_name for cls_name in class_selectors if not cls_name.startswith("lg-")]
    assert not non_lg_classes, f"Found classes without 'lg-' prefix: {non_lg_classes}"


def test_plugin_api_defines_router(tmp_path):
    api_path = Path("src/hermes_loopguard/dashboard/plugin_api.py")
    assert api_path.is_file(), "plugin_api.py file does not exist"

    # Always verify using ast that router is defined at module level
    tree = ast.parse(api_path.read_text(encoding="utf-8"))
    router_assigned = False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "router":
                    router_assigned = True
    assert router_assigned, "plugin_api.py must define 'router'"

    # If fastapi is available, test module import and routes
    try:
        import fastapi
        from hermes_loopguard.dashboard import plugin_api
        assert hasattr(plugin_api, "router")
    except ImportError:
        pass


def test_plugin_api_review_approve_and_drop_local(tmp_path):
    db_file = tmp_path / "test.db"
    store = Store(db_file)
    try:
        store.upsert_item("test-watcher", "item-1", fingerprint="fp1", title="Item 1")
        store.upsert_item("test-watcher", "item-2", fingerprint="fp2", title="Item 2")
        rev_id1 = store.enqueue_review(
            "test-watcher",
            "item-1",
            title="Item 1",
            reason="Low confidence",
            payload={"item": "data 1"},
        )
        rev_id2 = store.enqueue_review(
            "test-watcher",
            "item-2",
            title="Item 2",
            reason="Unclear issue",
            payload={"item": "data 2"},
        )

        from hermes_loopguard.dashboard import plugin_api

        # Test approve
        plugin_api.review_approve(store, rev_id1)
        assert store.get_kv("force_route:test-watcher:item-1") is not None
        item1 = store.get_item("test-watcher", "item-1")
        assert item1 is not None
        assert item1["fingerprint"] == ""

        # Test drop
        plugin_api.review_drop(store, rev_id2)
        item2 = store.get_item("test-watcher", "item-2")
        assert item2 is not None
        assert item2["state"] == "dropped"

        # Check queue has 0 open reviews
        open_reviews = store.review_queue(status="open")
        assert len(open_reviews) == 0
    finally:
        store.close()


def test_dist_index_js_route_mix_handles_custom_route_names():
    """Watchers define custom route names ("bug", "duplicate_or_spam"); the route mix
    cards must render whatever stats.routes actually contains, not just the five
    standard handler names, and the cron:top banner must link the path-routed tab."""
    js_path = Path("src/hermes_loopguard/dashboard/dist/index.js")
    text = js_path.read_text(encoding="utf-8")
    assert "function routeEntries(" in text, "route mix must derive entries from stats.routes"
    assert "lg-color-custom" in text, "custom route names need the fallback colour"
    # The dashboard is a path-routed SPA; a hash href would not navigate.
    assert "'#/loopguard'" not in text
    assert "href: '/loopguard'" in text

    css_path = Path("src/hermes_loopguard/dashboard/dist/style.css")
    css = css_path.read_text(encoding="utf-8")
    assert "--lg-color-custom:" in css, "style.css must define the custom route colour"


# --- D8 polish: route palette + clean y-axis ticks ---------------------------

# Runs the dashboard's own functions under node: extracts them (plus the two
# vars they close over) out of the IIFE and evaluates an expression against
# them, printing a LGRESULT-tagged JSON line.
_NODE_HARNESS = r"""const fs = require('fs');
// node argv: [0] node, [1] this harness, [2] index.js path, [3] the expression
const src = fs.readFileSync(process.argv[2], 'utf8');

function extract(name) {
  const marker = 'function ' + name + '(';
  const start = src.indexOf(marker);
  if (start === -1) throw new Error('function ' + name + ' not found in index.js');
  let depth = 0;
  for (let j = src.indexOf('{', start); j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error('unbalanced braces while extracting ' + name);
}

function preludeVar(name) {
  const marker = 'var ' + name + ' = ';
  const start = src.indexOf(marker);
  if (start === -1) throw new Error(marker.trim() + ' not found in index.js');
  return src.slice(start, src.indexOf(';', start) + 1);
}

const fns = new Function(
  ['KNOWN_ROUTES', 'CUSTOM_ROUTE_PALETTE'].map(preludeVar).join('\n') + '\n' +
  ['routeColor', 'customRouteColor', 'niceStep', 'chartTicks'].map(extract).join('\n') +
  '\nreturn { routeColor: routeColor, customRouteColor: customRouteColor, ' +
  'niceStep: niceStep, chartTicks: chartTicks };'
)();
const result = new Function('fns', 'return (' + process.argv[3] + ');')(fns);
console.log('LGRESULT' + JSON.stringify(result));
"""


def _node_js_fns(expression: str, js_path: Path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH; structural coverage only")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", encoding="utf-8", delete=False
    ) as fh:
        fh.write(_NODE_HARNESS)
        harness = Path(fh.name)
    try:
        r = subprocess.run(
            [node, str(harness), str(js_path), expression],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        harness.unlink(missing_ok=True)
    assert r.returncode == 0, f"node harness failed:\n{r.stdout}\n{r.stderr}"
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("LGRESULT")]
    assert lines, f"node harness produced no result:\n{r.stdout}\n{r.stderr}"
    return json.loads(lines[-1][len("LGRESULT"):])


def test_custom_route_palette_is_defined_once_on_lg_root():
    """Custom route names must get distinct, stable colours: a palette of
    --lg-color-custom-N props declared once on .lg-root (so a theme can
    override them in one place), sized to match CUSTOM_ROUTE_PALETTE in
    index.js."""
    css = Path("src/hermes_loopguard/dashboard/dist/style.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)

    root = re.search(r"\.lg-root\s*\{.*?\}", css, re.DOTALL)
    assert root, ".lg-root rule not found"
    slots = [int(n) for n in re.findall(r"--lg-color-custom-(\d+)\s*:", root.group(0))]
    assert len(slots) >= 4 and slots == list(range(len(slots))), slots

    js = Path("src/hermes_loopguard/dashboard/dist/index.js").read_text(encoding="utf-8")
    m = re.search(r"var CUSTOM_ROUTE_PALETTE = (\d+);", js)
    assert m, "CUSTOM_ROUTE_PALETTE not defined in index.js"
    assert int(m.group(1)) == len(slots), "palette size must match the CSS slots"

    # Defined once: no other rule block redeclares a palette slot.
    assert "--lg-color-custom-" not in css.replace(root.group(0), "")


def test_stacked_bar_chart_derives_ticks_from_chartticks():
    text = Path("src/hermes_loopguard/dashboard/dist/index.js").read_text(encoding="utf-8")
    assert "function chartTicks(" in text
    assert "chartTicks(maxVal)" in text, "StackedBarChart must derive its ticks from chartTicks"
    assert "[0, roundMax / 2, roundMax]" not in text, "old mid-tick pairing must be gone"


def test_chart_ticks_are_clean_integers_for_all_maxima():
    """The old [0, mid, top] pairing showed "0 3 5" for a max of 4 (2.5 rounded
    up). Ticks must be distinct integers from 0 to a top that covers the max,
    with a nice step, for small and large maxima alike."""
    js_path = Path("src/hermes_loopguard/dashboard/dist/index.js")
    all_maxima = _node_js_fns(
        "(function () { var out = [];"
        "for (var m = 1; m <= 200; m++) out.push(fns.chartTicks(m));"
        "return out; })()",
        js_path,
    )
    for m, info in enumerate(all_maxima, start=1):
        ticks = info["ticks"]
        assert ticks[0] == 0 and len(ticks) >= 2, (m, info)
        assert all(float(t).is_integer() for t in ticks), (m, info)
        assert ticks == sorted(set(ticks)), (m, info)
        assert info["top"] >= m and ticks[-1] == info["top"], (m, info)
        assert len(ticks) <= 8, (m, info)
        sig = info["step"] / 10 ** int(math.log10(info["step"]))
        assert round(sig, 6) in (1.0, 2.0, 5.0), (m, info)

    # Small maxima label every integer: max 4 is 0,1,2,3,4 — not "0 3 5".
    assert _node_js_fns("fns.chartTicks(4).ticks", js_path) == [0, 1, 2, 3, 4]
    assert _node_js_fns("fns.chartTicks(1).ticks", js_path) == [0, 1]


def test_custom_route_colors_are_distinct_and_stable():
    """Route Mix and the watchers-table bar render the same swatch for the same
    route name on every render, and different swatches for the two demo custom
    routes ("bug" vs "duplicate_or_spam"); standard routes keep their
    dedicated vars."""
    js_path = Path("src/hermes_loopguard/dashboard/dist/index.js")
    colors = _node_js_fns(
        "['bug', 'duplicate_or_spam', 'bug', 'feature', 'question'].map(fns.routeColor)",
        js_path,
    )
    assert colors[0] == colors[2], "the same route name must always map to one colour"
    assert colors[0] != colors[1], "the two demo custom routes must not share a swatch"
    # A 6-slot palette cannot promise uniqueness past 6 custom routes; it must
    # spread the demo's route-name set over several slots.
    assert len(set(colors)) >= 3, colors
    assert _node_js_fns("['agent', 'drop'].map(fns.routeColor)", js_path) == [
        "var(--lg-color-agent)", "var(--lg-color-drop)",
    ]


def test_plugin_api_endpoints_round_trip(tmp_path, monkeypatch):
    """GET handlers against a temp store: custom route names pass through verbatim
    and review actions resolve through the module-level functions."""
    monkeypatch.setenv("LOOPGUARD_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from hermes_loopguard.dashboard import plugin_api
    from hermes_loopguard.db import Store

    db_file = tmp_path / "api.db"
    store = Store(db_file)
    try:
        store.upsert_item("demo-issues", "ISS-1", fingerprint="fp1", title="Bug")
        store.log_poll(
            watcher="demo-issues", n_items=2, n_changed=1, n_admitted=1,
            woke=True, shadow=False, est_tokens_avoided=1200, est_usd_avoided=0.006,
            gate_tokens=100, gate_usd=0.0005,
        )
        store.log_decision(
            watcher="demo-issues", kind="route", item_id="ISS-1", route="bug",
            backend="typesafe", model="jev-1.13.0", outcome="done",
        )
        rev_id = store.enqueue_review(
            "demo-issues", "ISS-1", title="Bug", reason="low confidence", payload={},
        )
    finally:
        store.close()

    stats = plugin_api.get_stats()
    assert stats["polls"] == 1
    assert stats["routes"] == {"bug": 1}, "route names must pass through verbatim"

    decisions = plugin_api.get_decisions(limit=10)
    assert len(decisions) == 1
    assert decisions[0]["kind"] == "route"
    assert decisions[0]["route"] == "bug"

    watchers = plugin_api.get_watchers()
    assert watchers == []  # no watcher specs under the temp home

    review = plugin_api.get_review()
    assert len(review) == 1 and review[0]["id"] == rev_id

    result = plugin_api.post_review(rev_id, {"action": "approve"})
    assert result == {"ok": True, "action": "approve", "review_id": rev_id}
    store = Store(db_file)
    try:
        assert store.review_queue(status="open") == []
    finally:
        store.close()

    # Unknown review and bad action fail with HTTP-style errors
    with pytest.raises(Exception) as exc:
        plugin_api.post_review(rev_id + 999, {"action": "approve"})
    assert "not found" in str(exc.value)
    with pytest.raises(Exception) as exc:
        plugin_api.post_review(rev_id, {"action": "nonsense"})
    assert "approve" in str(exc.value)
