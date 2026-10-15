from __future__ import annotations

import ast
import json
from pathlib import Path
import re
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
