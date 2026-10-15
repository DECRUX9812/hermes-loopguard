from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    from fastapi import APIRouter, HTTPException, Query, Request
except ImportError:  # pragma: no cover
    # Fallback mock for unit test environments where fastapi is not installed
    class APIRouter:  # type: ignore
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.routes: list[tuple[str, str, Any]] = []

        def get(self, path: str, *args: Any, **kwargs: Any) -> Any:
            def decorator(fn: Any) -> Any:
                self.routes.append(("GET", path, fn))
                return fn
            return decorator

        def post(self, path: str, *args: Any, **kwargs: Any) -> Any:
            def decorator(fn: Any) -> Any:
                self.routes.append(("POST", path, fn))
                return fn
            return decorator

    class HTTPException(Exception):  # type: ignore
        def __init__(self, status_code: int = 400, detail: str = "") -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    def Query(default: Any = None, *args: Any, **kwargs: Any) -> Any:  # type: ignore
        return default

    class Request:  # type: ignore
        pass


router = APIRouter()


def review_approve(store: Any, review_id: int) -> None:
    try:
        from hermes_loopguard.cli import review_approve as cli_review_approve
        cli_review_approve(store, review_id)
        return
    except (ImportError, AttributeError):
        pass

    cur = store.conn.cursor()
    cur.execute("SELECT * FROM reviews WHERE id = ?", (review_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Review {review_id} not found")

    watcher = row["watcher"]
    item_id = row["item_id"]
    forced_route = "agent"
    try:
        from hermes_loopguard.config import load_watcher
        spec = load_watcher(watcher)
        if spec.gate and isinstance(spec.gate, dict) and "routes" in spec.gate:
            for r_name, r_rule in spec.gate["routes"].items():
                h = r_rule.get("handler") if isinstance(r_rule, dict) else getattr(r_rule, "handler", None)
                if h == "agent":
                    forced_route = r_name
                    break
    except Exception:
        pass

    store.set_kv(f"force_route:{watcher}:{item_id}", forced_route)
    store.upsert_item(watcher, item_id, fingerprint="")
    store.resolve_review(review_id, "approved")


def review_drop(store: Any, review_id: int) -> None:
    try:
        from hermes_loopguard.cli import review_drop as cli_review_drop
        cli_review_drop(store, review_id)
        return
    except (ImportError, AttributeError):
        pass

    cur = store.conn.cursor()
    cur.execute("SELECT * FROM reviews WHERE id = ?", (review_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Review {review_id} not found")

    watcher = row["watcher"]
    item_id = row["item_id"]
    if store.get_item(watcher, item_id) is None:
        store.upsert_item(watcher, item_id, fingerprint="")
    store.set_item_state(watcher, item_id, "dropped")
    store.resolve_review(review_id, "dropped")


@router.get("/stats")
def get_stats(watcher: str | None = None, since: str | None = None) -> dict[str, Any]:
    from hermes_loopguard.config import load_config
    from hermes_loopguard.db import Store

    store = Store.open_default()
    try:
        cfg = load_config()
        w = watcher if watcher else None
        s = since if since else None
        try:
            from hermes_loopguard.stats import compute_stats
            return compute_stats(store, cfg, watcher=w, since=s)
        except (ImportError, AttributeError):
            import time
            return {
                "polls": 0,
                "skipped_runs": 0,
                "woken_runs": 0,
                "skip_rate": 0.0,
                "shadow_polls": 0,
                "shadow_would_skip": 0,
                "items_gated": 0,
                "routes": {},
                "outcomes": {},
                "gate_tokens": 0,
                "gate_usd": 0.0,
                "agent_tokens": 0,
                "agent_usd": 0.0,
                "tokens_avoided": 0,
                "usd_avoided": 0.0,
                "net_usd_saved": 0.0,
                "completed_items": 0,
                "cost_per_completed": None,
                "review_open": len(store.review_queue(status="open")),
                "errors": 0,
                "per_watcher": {},
                "timeline": [],
                "recent": store.decisions(watcher=w, limit=25),
                "generated_at": time.time(),
            }
    finally:
        store.close()


@router.get("/decisions")
def get_decisions(
    limit: int = 50,
    kind: str | None = None,
    watcher: str | None = None,
) -> list[dict[str, Any]]:
    from hermes_loopguard.db import Store

    store = Store.open_default()
    try:
        w = watcher if watcher else None
        k = kind if kind else None
        return store.decisions(watcher=w, kind=k, limit=limit)
    finally:
        store.close()


@router.get("/watchers")
def get_watchers() -> list[dict[str, Any]]:
    from hermes_loopguard.config import list_watchers, load_watcher, load_config
    from hermes_loopguard import paths

    cfg = load_config()
    global_shadow = bool(cfg.get("shadow", False))
    names = list_watchers()
    result = []
    scripts_dir = paths.scripts_dir()

    for name in names:
        shim_path = scripts_dir / f"loopguard_{name}.py"
        shim_present = shim_path.is_file()
        try:
            spec = load_watcher(name)
            source_kind = "unknown"
            if isinstance(spec.source, dict) and spec.source:
                source_kind = next(iter(spec.source.keys()))
            routes = ["agent"]
            if spec.gate and isinstance(spec.gate, dict) and "routes" in spec.gate:
                routes = list(spec.gate["routes"].keys())
            shadow = spec.shadow if spec.shadow is not None else global_shadow
            result.append({
                "name": name,
                "source_kind": source_kind,
                "routes": routes,
                "shadow": bool(shadow),
                "shim_present": shim_present,
            })
        except Exception:
            result.append({
                "name": name,
                "source_kind": "unknown",
                "routes": ["agent"],
                "shadow": global_shadow,
                "shim_present": shim_present,
            })
    return result


@router.get("/review")
def get_review() -> list[dict[str, Any]]:
    from hermes_loopguard.db import Store

    store = Store.open_default()
    try:
        return store.review_queue(status="open")
    finally:
        store.close()


@router.post("/review/{review_id}")
def post_review(review_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("action")
    if action not in ("approve", "drop"):
        raise HTTPException(status_code=400, detail="Action must be 'approve' or 'drop'")

    from hermes_loopguard.db import Store

    store = Store.open_default()
    try:
        if action == "approve":
            review_approve(store, review_id)
        else:
            review_drop(store, review_id)
        return {"ok": True, "action": action, "review_id": review_id}
    finally:
        store.close()
