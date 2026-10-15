"""Persistence for computer-use runs (X3): cu_runs / cu_steps tables plus
frame files (full PNG + JPEG thumbnail) under the loopguard data dir
``cu/<run>/``.  Shadow mode logs only — nothing here ever influences the run
itself, and every db/filesystem failure is swallowed (logging must never
break a computer-use run).
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Any

__all__ = ["CuRunLogger"]


class CuRunLogger:
    """Writes runs and steps to the Store (cu_runs / cu_steps) and frames to
    ``<data_dir>/cu/<run_id>/``.  ``store`` may be None (frames only), and
    ``enabled=False`` makes every method a no-op."""

    def __init__(
        self,
        store: Any | None,
        *,
        base_dir: str | Path | None = None,
        enabled: bool = True,
        retention: int = 20,
    ):
        self.store = store
        self.enabled = bool(enabled)
        self.retention = max(1, int(retention))
        if base_dir is None:
            from hermes_loopguard import paths

            base_dir = paths.data_dir() / "cu"
        self.base_dir = Path(base_dir)

    # -- paths ---------------------------------------------------------------

    def run_dir(self, run_id: int) -> Path:
        return self.base_dir / str(run_id)

    # -- lifecycle -----------------------------------------------------------

    def start_run(self, task: str, instruction: str) -> int | None:
        """Insert the cu_runs row; returns the run id (None when disabled)."""
        if not self.enabled:
            return None
        now = time.time()
        if self.store is not None:
            try:
                cur = self.store.conn.execute(
                    """INSERT INTO cu_runs (created_at, task, instruction, state, shadow)
                       VALUES (?, ?, ?, 'running', 1)""",
                    (now, task, instruction),
                )
                run_id = int(cur.lastrowid)
            except Exception:
                run_id = None
        else:
            run_id = int(now * 1000) % 1_000_000_000
        try:
            self.run_dir(run_id).mkdir(parents=True, exist_ok=True)
            self._retain()
        except Exception:
            pass
        return run_id

    def save_frame(self, run_id: int | None, step_idx: int, kind: str, frame: Any) -> str | None:
        """Save one full PNG + JPEG thumbnail; returns the PNG path."""
        if not self.enabled or run_id is None:
            return None
        try:
            directory = self.run_dir(run_id)
            directory.mkdir(parents=True, exist_ok=True)
            png_path = directory / f"{step_idx:03d}-{kind}.png"
            png_path.write_bytes(frame.png)
            self._save_thumbnail(directory / f"{step_idx:03d}-{kind}.jpg", frame.png)
            return str(png_path)
        except Exception:
            return None

    def record_step(self, run_id: int | None, rec: Any) -> None:
        if not self.enabled or run_id is None or self.store is None:
            return
        try:
            usage = rec.usages[-1] if rec.usages else None
            self.store.conn.execute(
                """INSERT INTO cu_steps (run_id, idx, action, target, grounding, box, point,
                                         verdict, noul, effect, stall, safety, model, tokens_in,
                                         tokens_out, usd, frame_before, frame_after, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    rec.idx,
                    rec.action,
                    rec.target,
                    rec.grounding,
                    json.dumps(rec.box) if rec.box else None,
                    json.dumps(rec.point) if rec.point else None,
                    rec.verdict,
                    rec.noul,
                    rec.effect,
                    rec.stall,
                    rec.safety,
                    getattr(usage, "model", "") or "",
                    sum(int(u.input_tokens or 0) for u in rec.usages),
                    sum(int(u.output_tokens or 0) for u in rec.usages),
                    sum(float(u.usd or 0.0) for u in rec.usages),
                    rec.frame_before,
                    rec.frame_after,
                    time.time(),
                ),
            )
        except Exception:
            pass

    def finish_run(self, run_id: int | None, result: Any) -> None:
        if not self.enabled or run_id is None or self.store is None:
            return
        try:
            self.store.conn.execute(
                """UPDATE cu_runs SET finished_at = ?, state = ?, steps = ?, tokens_in = ?,
                                       tokens_out = ?, usd = ?, wall_s = ?, big_model_calls = ?,
                                       big_model_avoided = ?, reason = ?, done_check = ?
                   WHERE id = ?""",
                (
                    time.time(),
                    result.state,
                    len(result.steps),
                    result.input_tokens,
                    result.output_tokens,
                    result.usd,
                    result.wall_s,
                    result.big_model_calls,
                    result.big_model_avoided,
                    result.reason or "",
                    result.done_check,
                    run_id,
                ),
            )
        except Exception:
            pass

    # -- helpers ---------------------------------------------------------------

    def _save_thumbnail(self, path: Path, png: bytes) -> None:
        from PIL import Image  # lazy; thumbnails are best-effort

        img = Image.open(io.BytesIO(png))
        img.load()
        scale = min(1.0, 640 / max(img.size))
        if scale < 1.0:
            img = img.resize((max(1, round(img.size[0] * scale)), max(1, round(img.size[1] * scale))))
        img.convert("RGB").save(path, format="JPEG", quality=70)

    def _retain(self) -> None:
        """Keep frame dirs for the last N runs only (db rows are kept)."""
        dirs = sorted(
            (d for d in self.base_dir.iterdir() if d.is_dir() and d.name.isdigit()),
            key=lambda d: int(d.name),
        )
        for d in dirs[: -self.retention]:
            import shutil

            shutil.rmtree(d, ignore_errors=True)

    def recent_runs(self, limit: int = 5) -> list[dict]:
        if self.store is None:
            return []
        try:
            cur = self.store.conn.execute(
                """SELECT id, created_at, state, steps, usd, reason FROM cu_runs
                   ORDER BY id DESC LIMIT ?""",
                (int(limit),),
            )
            return list(cur.fetchall())
        except Exception:
            return []
