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
