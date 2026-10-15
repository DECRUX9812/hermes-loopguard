"""Attribution ledger: turn Hermes runtime events into LoopGuard accounting.

Hooks fire in the same process as the agent (the gateway or `hermes cron run`).
Every handler is best-effort: it never raises and logs at debug on failure, so a
LoopGuard bug can never break a turn.
"""
from __future__ import annotations

import logging
import os
import re
import threading

log = logging.getLogger(__name__)

_DONE_RE = re.compile(r"^\s*DONE\s+([^\s:]+)", re.IGNORECASE)
_BLOCKED_RE = re.compile(r"^\s*BLOCKED\s+([^\s:]+)\s*(?::\s*(.*))?$", re.IGNORECASE)
_SHIM_RE = re.compile(r"^loopguard_(.+)\.py$")

BUDGET_MESSAGE = (
    "LoopGuard: spend cap reached for this task. Stop now and report BLOCKED "
    "with the reason 'budget exhausted'."
)
BUDGET_EXEMPT_TOOLS = {"kanban_block", "kanban_comment", "kanban_show"}


def _as_mapping(usage) -> dict:
    if isinstance(usage, dict):
        return usage
    if usage is None:
        return {}
    out = {}
    for key in (
        "input_tokens",
        "prompt_tokens",
        "output_tokens",
        "completion_tokens",
        "cost_usd",
    ):
        try:
            out[key] = getattr(usage, key)
        except Exception:
            continue
    return out


def _int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def tokens_from_usage(usage) -> tuple[int, int]:
    """Token counts from a Hermes usage summary, tolerating both spellings."""
    data = _as_mapping(usage)
    tokens_in = _int_or_none(data.get("input_tokens"))
    if tokens_in is None:
        tokens_in = _int_or_none(data.get("prompt_tokens")) or 0
    tokens_out = _int_or_none(data.get("output_tokens"))
    if tokens_out is None:
        tokens_out = _int_or_none(data.get("completion_tokens")) or 0
    return tokens_in, tokens_out


def cost_from_usage(usage) -> float | None:
    value = _as_mapping(usage).get("cost_usd")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Ledger:
    """Hook handlers that attribute spend and item outcomes to LoopGuard runs."""

    def __init__(self, store_factory, get_cfg, cron_execution=lambda: None):
        self._store_factory = store_factory
        self._get_cfg = get_cfg
        self._cron_execution = cron_execution or (lambda: None)
        self._run_cache: dict[str, int] = {}
        self._lock = threading.Lock()

    # --- plumbing -------------------------------------------------------
    def _store(self):
        return self._store_factory()

    def _cfg(self) -> dict:
        try:
            return self._get_cfg() or {}
        except Exception:
            return {}

    def _execution(self):
        try:
            return self._cron_execution()
        except Exception:
            return None

    def _run_row(self, store, run_id: int) -> dict | None:
        cur = store.conn.cursor()
        cur.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        return cur.fetchone()

    def _watcher_for_job(self, ex) -> str | None:
        job_id = getattr(ex, "job_id", None)
        job = None
        if job_id:
            try:
                from cron.jobs import get_job

                job = get_job(job_id)
            except Exception:
                job = None
        script = job.get("script") if isinstance(job, dict) else None
        if script:
            m = _SHIM_RE.match(os.path.basename(str(script)))
            if m:
                return m.group(1)
        name = ""
        if isinstance(job, dict):
            name = str(job.get("name") or "")
        if not name:
            name = str(getattr(ex, "job_name", "") or "")
        if name.startswith("lg-"):
            return name[3:]
        if name:
            try:
                from hermes_loopguard.config import list_watchers

                if name in list_watchers():
                    return name
            except Exception:
                pass
        return None

    def _resolve_run(self, store, ex) -> int | None:
        """Map a cron execution to the LoopGuard run it belongs to."""
        if ex is None:
            return None
        exec_id = getattr(ex, "execution_id", None)
        if exec_id:
            with self._lock:
                cached = self._run_cache.get(str(exec_id))
            if cached is not None:
                return cached

        job_id = getattr(ex, "job_id", None)
        run = store.find_open_run(job_id=job_id) if job_id else None
        if run is None:
            watcher = self._watcher_for_job(ex)
            if watcher:
                candidate = store.find_open_run(watcher=watcher)
                if candidate is not None and not candidate.get("job_id"):
                    run = candidate
                    if job_id:
                        store.bind_run(run["id"], job_id)
        if run is None:
            return None

        run_id = int(run["id"])
        if exec_id:
            with self._lock:
                self._run_cache[str(exec_id)] = run_id
        return run_id

    def _item_cap(self, watcher: str | None) -> int:
        if watcher:
            try:
                from hermes_loopguard.config import load_watcher

                return int(load_watcher(watcher).spend_cap_tokens)
            except Exception:
                pass
        return int(self._cfg().get("kanban", {}).get("spend_cap_tokens", 400000))

    def _run_caps(self, run: dict, n_items: int) -> int:
        if n_items <= 0:
            return 0
        return self._item_cap(run.get("watcher")) * n_items

    def _kanban_mapping(self, store, task_id) -> dict:
        if not task_id:
            return {}
        value = store.get_kv(f"kanban_task:{task_id}")
        return value if isinstance(value, dict) else {}

    # --- cron hooks -----------------------------------------------------
    def post_api_request(self, **kw) -> None:
        try:
            store = self._store()
            if store is None:
                return
            usage = kw.get("usage")
            tokens_in, tokens_out = tokens_from_usage(usage)
            tokens = tokens_in + tokens_out
            usd = cost_from_usage(usage)
            if usd is None:
                usd = tokens * float(self._cfg().get("pricing", {}).get("usd_per_mtok", 3.0)) / 1e6
            if tokens == 0 and not usd:
                return

            ex = self._execution()
            run_id = self._resolve_run(store, ex) if ex is not None else None
            if run_id is not None:
                store.add_run_usage(run_id, tokens_in, tokens_out, usd)
                run = self._run_row(store, run_id)
                items = store.run_items(run_id)
                if run is not None and items:
                    base = tokens // len(items)
                    remainder = tokens - base * len(items)
                    per_usd = usd / len(items)
                    for idx, item_id in enumerate(items):
                        store.add_item_spend(
                            run["watcher"], item_id, base + (remainder if idx == 0 else 0), per_usd
                        )

            self._attribute_kanban(store, tokens, usd)
        except Exception:
            log.debug("LoopGuard ledger: post_api_request failed", exc_info=True)

    def _attribute_kanban(self, store, tokens: int, usd: float) -> None:
        task_id = os.environ.get("HERMES_KANBAN_TASK")
        if not task_id:
            return
        mapping = self._kanban_mapping(store, task_id)
        watcher = mapping.get("watcher")
        item_id = mapping.get("item_id")
        if watcher and item_id:
            store.add_item_spend(watcher, item_id, tokens, usd)

    def post_llm_call(self, assistant_response=None, **kw) -> None:
        try:
            text = assistant_response
            if text is None:
                text = kw.get("final_response")
            if text is None:
                return
            text = text if isinstance(text, str) else str(text)
            if not text.strip():
                return

            store = self._store()
            if store is None:
                return
            ex = self._execution()
            if ex is None:
                return
            run_id = self._resolve_run(store, ex)
            if run_id is None:
                return
            run = self._run_row(store, run_id)
            if run is None:
                return

            watcher = run["watcher"]
            items = set(store.run_items(run_id))
            for line in text.splitlines():
                blocked = _BLOCKED_RE.match(line)
                if blocked:
                    self._finish_item(store, watcher, blocked.group(1), "blocked", run_id, items,
                                      (blocked.group(2) or "").strip()[:300] or None)
                    continue
                done = _DONE_RE.match(line)
                if done:
                    self._finish_item(store, watcher, done.group(1), "completed", run_id, items, None)
        except Exception:
            log.debug("LoopGuard ledger: post_llm_call failed", exc_info=True)

    def _finish_item(self, store, watcher, item_id, state, run_id, items, note) -> None:
        item_id = item_id.strip()
        if item_id not in items:
            return
        store.set_item_state(watcher, item_id, state)
        store.release_claim(watcher, item_id)
        store.log_decision(
            watcher=watcher,
            kind="route",
            item_id=item_id,
            outcome="done" if state == "completed" else "blocked",
            run_id=run_id,
            note=note,
        )

    def on_session_end(self, completed=False, interrupted=False, **kw) -> None:
        try:
            store = self._store()
            if store is None:
                return
            ex = self._execution()
            if ex is None:
                return
            run_id = self._resolve_run(store, ex)
            if run_id is None:
                return
            run = self._run_row(store, run_id)
            if run is None:
                return

            watcher = run["watcher"]
            items = store.run_items(run_id)
            success = bool(completed) and not bool(interrupted)
            for item_id in items:
                item = store.get_item(watcher, item_id)
                if item is not None and item.get("state") == "claimed":
                    store.set_item_state(watcher, item_id, "completed" if success else "failed")
                    store.release_claim(watcher, item_id)

            caps = self._run_caps(run, len(items))
            tokens = int(run.get("tokens_in") or 0) + int(run.get("tokens_out") or 0)
            if caps and tokens > caps:
                for item_id in items:
                    item = store.get_item(watcher, item_id)
                    if item is not None:
                        store.set_item_state(watcher, item_id, "budget_exhausted")
                        store.log_decision(
                            watcher=watcher,
                            kind="budget",
                            item_id=item_id,
                            outcome="budget_exhausted",
                            run_id=run_id,
                        )

            outcome = "completed" if success else ("interrupted" if interrupted else "failed")
            store.close_run(run_id, completed=success, outcome=outcome)

            exec_id = getattr(ex, "execution_id", None)
            if exec_id:
                with self._lock:
                    self._run_cache.pop(str(exec_id), None)
        except Exception:
            log.debug("LoopGuard ledger: on_session_end failed", exc_info=True)

    # --- kanban observers ----------------------------------------------
    def kanban_task_claimed(self, task_id=None, **kw) -> None:
        try:
            store = self._store()
            if store is None:
                return
            mapping = self._kanban_mapping(store, task_id)
            store.log_decision(
                watcher=mapping.get("watcher") or "kanban",
                kind="kanban",
                item_id=mapping.get("item_id"),
                outcome="claimed",
                note=str(task_id) if task_id else None,
            )
        except Exception:
            log.debug("LoopGuard ledger: kanban_task_claimed failed", exc_info=True)

    def kanban_task_completed(self, task_id=None, summary=None, **kw) -> None:
        self._kanban_finish(task_id, "completed", "completed", summary)

    def kanban_task_blocked(self, task_id=None, reason=None, **kw) -> None:
        self._kanban_finish(task_id, "blocked", "blocked", reason)

    def _kanban_finish(self, task_id, state, outcome, note) -> None:
        try:
            store = self._store()
            if store is None:
                return
            mapping = self._kanban_mapping(store, task_id)
            watcher = mapping.get("watcher")
            item_id = mapping.get("item_id")
            if watcher and item_id:
                store.set_item_state(watcher, item_id, state)
                store.release_claim(watcher, item_id)
            store.log_decision(
                watcher=watcher or "kanban",
                kind="kanban",
                item_id=item_id,
                outcome=outcome,
                note=(str(note)[:300] if note else (str(task_id) if task_id else None)),
            )
        except Exception:
            log.debug("LoopGuard ledger: kanban finish failed", exc_info=True)

    def on_kanban_worker_exited(self, task_id=None, exit_kind=None, **kw) -> None:
        try:
            store = self._store()
            if store is None:
                return
            mapping = self._kanban_mapping(store, task_id)
            store.log_decision(
                watcher=mapping.get("watcher") or "kanban",
                kind="kanban",
                item_id=mapping.get("item_id"),
                outcome="worker_exited",
                note=f"{task_id}: {exit_kind}" if exit_kind else (str(task_id) if task_id else None),
            )
        except Exception:
            log.debug("LoopGuard ledger: on_kanban_worker_exited failed", exc_info=True)

    # --- spend cap enforcement -----------------------------------------
    def budget_guard(self, tool_name=None, args=None, **kw) -> dict | None:
        try:
            if tool_name in BUDGET_EXEMPT_TOOLS:
                return None
            store = self._store()
            if store is None:
                return None

            ex = self._execution()
            if ex is not None:
                run_id = self._resolve_run(store, ex)
                if run_id is not None:
                    run = self._run_row(store, run_id)
                    if run is not None:
                        items = store.run_items(run_id)
                        caps = self._run_caps(run, len(items))
                        tokens = int(run.get("tokens_in") or 0) + int(run.get("tokens_out") or 0)
                        if caps and tokens > caps:
                            return self._block()

            task_id = os.environ.get("HERMES_KANBAN_TASK")
            if task_id:
                mapping = self._kanban_mapping(store, task_id)
                watcher = mapping.get("watcher")
                item_id = mapping.get("item_id")
                if watcher and item_id:
                    item = store.get_item(watcher, item_id)
                    if item is not None:
                        spent = int(item.get("spend_tokens") or 0)
                        cap = self._item_cap(watcher)
                        if item.get("state") == "budget_exhausted" or (cap and spent >= cap):
                            return self._block()
        except Exception:
            log.debug("LoopGuard ledger: budget_guard failed", exc_info=True)
        return None

    @staticmethod
    def _block() -> dict:
        return {"action": "block", "message": BUDGET_MESSAGE}
