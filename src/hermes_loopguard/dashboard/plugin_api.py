from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    from fastapi import APIRouter, HTTPException, Query, Request
    from fastapi.responses import Response
except ImportError:  # pragma: no cover
    # Import fallback so unit tests can import this module without fastapi
    # installed; the real fastapi types above are used whenever importable.
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

    Response = None  # type: ignore


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


# --- computer use (X5): read-only views over the cu_runs / cu_steps log -----

_CU_KINDS = ("before", "after", "thumb")


def _cu_run_dict(row: dict[str, Any]) -> dict[str, Any]:
    tokens_in = int(row.get("tokens_in") or 0)
    tokens_out = int(row.get("tokens_out") or 0)
    return {
        "id": row["id"],
        "task": row.get("task") or "",
        "instruction": row.get("instruction") or "",
        "state": row.get("state") or "running",
        "steps": int(row.get("steps") or 0),
        "tokens": tokens_in + tokens_out,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "usd": float(row.get("usd") or 0.0),
        "wall_s": float(row.get("wall_s") or 0.0),
        "big_model_calls": int(row.get("big_model_calls") or 0),
        "big_model_avoided": int(row.get("big_model_avoided") or 0),
        "reason": row.get("reason") or "",
        "done_check": row.get("done_check"),
        "shadow": bool(row.get("shadow", 1)),
        "created_at": row.get("created_at"),
        "finished_at": row.get("finished_at"),
    }


def _cu_num_list(raw: Any, length: int) -> list[float] | None:
    """Parse a JSON-encoded numeric tuple ('[x, y, ...]'); None on anything else."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, (list, tuple)) or len(parsed) != length:
        return None
    try:
        return [float(v) for v in parsed]
    except (TypeError, ValueError):
        return None


def _cu_step_dict(row: dict[str, Any]) -> dict[str, Any]:
    tokens_in = int(row.get("tokens_in") or 0)
    tokens_out = int(row.get("tokens_out") or 0)
    return {
        "id": row["id"],
        "run_id": row.get("run_id"),
        "idx": row.get("idx"),
        "action": row.get("action") or "",
        "target": row.get("target") or "",
        "grounding": row.get("grounding") or "none",
        # box: [x0, y0, x1, y1] as fractions of the frame; point: [x, y] in
        # desktop pixels.  Frames themselves are referenced by STEP id, never
        # by filesystem path.
        "box": _cu_num_list(row.get("box"), 4),
        "point": _cu_num_list(row.get("point"), 2),
        "verdict": row.get("verdict") or "",
        "noul": row.get("noul"),
        "effect": row.get("effect") or "",
        "stall": row.get("stall") or "",
        "safety": row.get("safety") or "",
        "model": row.get("model") or "",
        "tokens": tokens_in + tokens_out,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "usd": float(row.get("usd") or 0.0),
        "frame_before": int(row["id"]) if row.get("frame_before") else None,
        "frame_after": int(row["id"]) if row.get("frame_after") else None,
        "created_at": row.get("created_at"),
    }


@router.get("/cu/runs")
def get_cu_runs(limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    from hermes_loopguard.db import Store

    store = Store.open_default()
    try:
        cur = store.conn.execute(
            "SELECT * FROM cu_runs ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [_cu_run_dict(r) for r in cur.fetchall()]
    finally:
        store.close()


@router.get("/cu/steps")
def get_cu_steps(run: int) -> list[dict[str, Any]]:
    from hermes_loopguard.db import Store

    store = Store.open_default()
    try:
        cur = store.conn.execute("SELECT id FROM cu_runs WHERE id = ?", (run,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Run {run} not found")
        cur = store.conn.execute(
            "SELECT * FROM cu_steps WHERE run_id = ? ORDER BY idx, id", (run,)
        )
        rows = cur.fetchall()
    finally:
        store.close()
    return [_cu_step_dict(r) for r in rows]


def _cu_frame_payload(step_id: int, kind: str) -> tuple[bytes, str]:
    """Resolve the frame file a cu_steps row points to.  Serves ONLY files
    under the loopguard data dir; anything else (including paths smuggled into
    the db) is a 404.  Returns (bytes, media_type)."""
    if kind not in _CU_KINDS:
        raise HTTPException(
            status_code=400, detail="kind must be 'before', 'after' or 'thumb'"
        )

    from hermes_loopguard import paths
    from hermes_loopguard.db import Store

    store = Store.open_default()
    try:
        cur = store.conn.execute(
            "SELECT frame_before, frame_after FROM cu_steps WHERE id = ?", (step_id,)
        )
        row = cur.fetchone()
    finally:
        store.close()

    if not row:
        raise HTTPException(status_code=404, detail=f"Step {step_id} not found")

    column = "frame_after" if kind == "after" else "frame_before"
    raw = row.get(column)
    if not raw:
        raise HTTPException(status_code=404, detail=f"Step {step_id} has no {kind} frame")

    frame_path = Path(str(raw))
    if kind == "thumb":
        # The thumbnail is the JPEG sibling the logger wrote next to the PNG.
        frame_path = frame_path.with_suffix(".jpg")
    try:
        resolved = frame_path.resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Invalid frame path")

    data_root = paths.data_dir().resolve()
    if not str(resolved).startswith(str(data_root) + os.sep):
        raise HTTPException(
            status_code=404, detail="Frame path is outside the LoopGuard data dir"
        )
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="Frame file not found")

    media_type = "image/jpeg" if resolved.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return resolved.read_bytes(), media_type


@router.get("/cu/frame/{step_id}")
def get_cu_frame(step_id: int, kind: str = "before") -> Any:
    data, media_type = _cu_frame_payload(step_id, kind)
    if Response is not None:
        return Response(content=data, media_type=media_type)
    # Fallback (no fastapi, unit tests only): plain dict so callers can still
    # inspect the payload and media type.
    return {"status_code": 200, "media_type": media_type, "body": data}
