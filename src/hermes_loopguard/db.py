from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

from hermes_loopguard import paths
from hermes_loopguard.fingerprint import canonical


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.path),
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None,
        )
        self.conn.row_factory = _dict_factory
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA busy_timeout=5000;")
        self._init_schema()

    @classmethod
    def open_default(cls) -> Store:
        return cls(paths.db_path())

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _immediate_tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                    watcher TEXT,
                    item_id TEXT,
                    fingerprint TEXT,
                    title TEXT,
                    state TEXT DEFAULT 'new',
                    route TEXT,
                    attempts INTEGER DEFAULT 0,
                    spend_tokens INTEGER DEFAULT 0,
                    spend_usd REAL DEFAULT 0.0,
                    first_seen REAL,
                    last_seen REAL,
                    last_attempt_fp TEXT,
                    PRIMARY KEY (watcher, item_id)
                );

                CREATE TABLE IF NOT EXISTS claims (
                    watcher TEXT,
                    item_id TEXT,
                    fingerprint TEXT,
                    owner TEXT,
                    claimed_at REAL,
                    expires_at REAL,
                    PRIMARY KEY (watcher, item_id)
                );

                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL,
                    watcher TEXT,
                    kind TEXT,
                    item_id TEXT,
                    route TEXT,
                    proposed_route TEXT,
                    shadow INTEGER,
                    backend TEXT,
                    model TEXT,
                    question_version TEXT,
                    answers TEXT,
                    outcome TEXT,
                    tokens_in INTEGER,
                    tokens_out INTEGER,
                    cost_usd REAL,
                    run_id INTEGER,
                    note TEXT
                );

                CREATE TABLE IF NOT EXISTS polls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL,
                    watcher TEXT,
                    n_items INTEGER,
                    n_changed INTEGER,
                    n_admitted INTEGER,
                    woke INTEGER,
                    shadow INTEGER,
                    est_tokens_avoided INTEGER,
                    est_usd_avoided REAL,
                    gate_tokens INTEGER,
                    gate_usd REAL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL,
                    closed_at REAL,
                    watcher TEXT,
                    owner TEXT,
                    job_id TEXT,
                    items TEXT,
                    status TEXT DEFAULT 'open',
                    tokens_in INTEGER DEFAULT 0,
                    tokens_out INTEGER DEFAULT 0,
                    usd REAL DEFAULT 0.0,
                    completed INTEGER DEFAULT 0,
                    outcome TEXT
                );

                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL,
                    resolved_at REAL,
                    watcher TEXT,
                    item_id TEXT,
                    title TEXT,
                    reason TEXT,
                    payload TEXT,
                    status TEXT DEFAULT 'open'
                );

                CREATE TABLE IF NOT EXISTS counters (
                    key TEXT PRIMARY KEY,
                    val INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    val TEXT
                );
                """
            )

    # items: state in {"new","claimed","completed","blocked","budget_exhausted","dropped","review","failed"}
    def get_item(self, watcher: str, item_id: str) -> dict | None:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT watcher, item_id, fingerprint, title, state, route, attempts, spend_tokens, spend_usd, first_seen, last_seen, last_attempt_fp FROM items WHERE watcher = ? AND item_id = ?",
            (watcher, item_id),
        )
        return cur.fetchone()

    def upsert_item(self, watcher: str, item_id: str, *, fingerprint: str, title: str | None = None) -> None:
        now = time.time()
        with self._immediate_tx():
            cur = self.conn.cursor()
            cur.execute(
                """INSERT INTO items (watcher, item_id, fingerprint, title, state, attempts, spend_tokens, spend_usd, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, 'new', 0, 0, 0.0, ?, ?)
                   ON CONFLICT(watcher, item_id) DO UPDATE SET
                   fingerprint = excluded.fingerprint,
                   title = COALESCE(excluded.title, items.title),
                   last_seen = excluded.last_seen""",
                (watcher, item_id, fingerprint, title, now, now),
            )

    def set_item_state(self, watcher: str, item_id: str, state: str, *, route: str | None = None) -> None:
        now = time.time()
        with self._immediate_tx():
            if route is not None:
                self.conn.execute(
                    "UPDATE items SET state = ?, route = ?, last_seen = ? WHERE watcher = ? AND item_id = ?",
                    (state, route, now, watcher, item_id),
                )
            else:
                self.conn.execute(
                    "UPDATE items SET state = ?, last_seen = ? WHERE watcher = ? AND item_id = ?",
                    (state, now, watcher, item_id),
                )

    def bump_attempt(self, watcher: str, item_id: str, fingerprint: str) -> int:
        now = time.time()
        with self._immediate_tx():
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE items SET attempts = attempts + 1, last_attempt_fp = ?, last_seen = ? WHERE watcher = ? AND item_id = ?",
                (fingerprint, now, watcher, item_id),
            )
            cur.execute("SELECT attempts FROM items WHERE watcher = ? AND item_id = ?", (watcher, item_id))
            row = cur.fetchone()
            return int(row["attempts"]) if row else 1

    def add_item_spend(self, watcher: str, item_id: str, tokens: int, usd: float) -> None:
        now = time.time()
        with self._immediate_tx():
            self.conn.execute(
                "UPDATE items SET spend_tokens = spend_tokens + ?, spend_usd = spend_usd + ?, last_seen = ? WHERE watcher = ? AND item_id = ?",
                (tokens, usd, now, watcher, item_id),
            )

    # claims
    def try_claim(self, watcher: str, item_id: str, fingerprint: str, owner: str, ttl_s: float = 3600) -> bool:
        with self._immediate_tx():
            cur = self.conn.cursor()
            cur.execute("SELECT owner, expires_at FROM claims WHERE watcher = ? AND item_id = ?", (watcher, item_id))
            row = cur.fetchone()
            now = time.time()
            if row is None or row["expires_at"] <= now:
                cur.execute(
                    """INSERT INTO claims (watcher, item_id, fingerprint, owner, claimed_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(watcher, item_id) DO UPDATE SET
                       fingerprint = excluded.fingerprint,
                       owner = excluded.owner,
                       claimed_at = excluded.claimed_at,
                       expires_at = excluded.expires_at""",
                    (watcher, item_id, fingerprint, owner, now, now + ttl_s),
                )
                return True
            return False

    def release_claim(self, watcher: str, item_id: str) -> None:
        with self._immediate_tx():
            self.conn.execute("DELETE FROM claims WHERE watcher = ? AND item_id = ?", (watcher, item_id))

    def claims_for_owner(self, owner: str) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM claims WHERE owner = ?", (owner,))
        return cur.fetchall()

    def claim_live(self, watcher: str, item_id: str, now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        cur = self.conn.cursor()
        cur.execute("SELECT expires_at FROM claims WHERE watcher = ? AND item_id = ?", (watcher, item_id))
        row = cur.fetchone()
        return bool(row is not None and row["expires_at"] > now)

    # decisions
    def log_decision(
        self,
        *,
        watcher: str,
        kind: str,
        item_id: str | None = None,
        route: str | None = None,
        proposed_route: str | None = None,
        shadow: bool = False,
        backend: str | None = None,
        model: str | None = None,
        question_version: str | None = None,
        answers: Any = None,
        outcome: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        run_id: int | None = None,
        note: str | None = None,
    ) -> int:
        now = time.time()
        ans_text = canonical(answers) if answers is not None else None
        with self._immediate_tx():
            cur = self.conn.cursor()
            cur.execute(
                """INSERT INTO decisions (created_at, watcher, kind, item_id, route, proposed_route, shadow,
                                          backend, model, question_version, answers, outcome, tokens_in,
                                          tokens_out, cost_usd, run_id, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now,
                    watcher,
                    kind,
                    item_id,
                    route,
                    proposed_route,
                    1 if shadow else 0,
                    backend,
                    model,
                    question_version,
                    ans_text,
                    outcome,
                    tokens_in,
                    tokens_out,
                    cost_usd,
                    run_id,
                    note,
                ),
            )
            return int(cur.lastrowid)

    def decisions(
        self,
        *,
        watcher: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        query = "SELECT * FROM decisions"
        clauses = []
        params = []
        if watcher is not None:
            clauses.append("watcher = ?")
            params.append(watcher)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cur = self.conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        res = []
        for r in rows:
            item = dict(r)
            item["shadow"] = bool(item["shadow"])
            if item.get("answers"):
                try:
                    item["answers"] = json.loads(item["answers"])
                except Exception:
                    pass
            res.append(item)
        return res

    # polls
    def log_poll(
        self,
        *,
        watcher: str,
        n_items: int,
        n_changed: int,
        n_admitted: int,
        woke: bool,
        shadow: bool,
        est_tokens_avoided: int = 0,
        est_usd_avoided: float = 0.0,
        gate_tokens: int = 0,
        gate_usd: float = 0.0,
    ) -> int:
        now = time.time()
        with self._immediate_tx():
            cur = self.conn.cursor()
            cur.execute(
                """INSERT INTO polls (created_at, watcher, n_items, n_changed, n_admitted, woke, shadow,
                                      est_tokens_avoided, est_usd_avoided, gate_tokens, gate_usd)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now,
                    watcher,
                    n_items,
                    n_changed,
                    n_admitted,
                    1 if woke else 0,
                    1 if shadow else 0,
                    est_tokens_avoided,
                    est_usd_avoided,
                    gate_tokens,
                    gate_usd,
                ),
            )
            return int(cur.lastrowid)

    def polls(self, *, watcher: str | None = None, since: float | None = None) -> list[dict]:
        query = "SELECT * FROM polls"
        clauses = []
        params = []
        if watcher is not None:
            clauses.append("watcher = ?")
            params.append(watcher)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC"
        cur = self.conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        res = []
        for r in rows:
            item = dict(r)
            item["woke"] = bool(item["woke"])
            item["shadow"] = bool(item["shadow"])
            res.append(item)
        return res

    # runs
    def open_run(self, *, watcher: str, owner: str, items: list[str], job_id: str | None = None) -> int:
        now = time.time()
        items_text = json.dumps(items)
        with self._immediate_tx():
            cur = self.conn.cursor()
            cur.execute(
                """INSERT INTO runs (created_at, watcher, owner, job_id, items, status, tokens_in, tokens_out, usd, completed, outcome)
                   VALUES (?, ?, ?, ?, ?, 'open', 0, 0, 0.0, 0, NULL)""",
                (now, watcher, owner, job_id, items_text),
            )
            return int(cur.lastrowid)

    def add_run_usage(self, run_id: int, tokens_in: int, tokens_out: int, usd: float) -> None:
        with self._immediate_tx():
            self.conn.execute(
                "UPDATE runs SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ?, usd = usd + ? WHERE id = ?",
                (tokens_in, tokens_out, usd, run_id),
            )

    def close_run(self, run_id: int, *, completed: bool, outcome: str) -> None:
        now = time.time()
        with self._immediate_tx():
            self.conn.execute(
                "UPDATE runs SET status = 'closed', closed_at = ?, completed = ?, outcome = ? WHERE id = ?",
                (now, 1 if completed else 0, outcome, run_id),
            )

    def bind_run(self, run_id: int, job_id: str | None) -> None:
        with self._immediate_tx():
            self.conn.execute("UPDATE runs SET job_id = ? WHERE id = ?", (job_id, run_id))

    def run_items(self, run_id: int) -> list[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT items FROM runs WHERE id = ?", (run_id,))
        row = cur.fetchone()
        if row is None or not row["items"]:
            return []
        try:
            return [str(x) for x in json.loads(row["items"])]
        except Exception:
            return []

    def find_open_run(
        self,
        *,
        job_id: str | None = None,
        watcher: str | None = None,
        owner: str | None = None,
    ) -> dict | None:
        query = "SELECT * FROM runs WHERE status = 'open'"
        clauses = []
        params = []
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(job_id)
        if watcher is not None:
            clauses.append("watcher = ?")
            params.append(watcher)
        if owner is not None:
            clauses.append("owner = ?")
            params.append(owner)
        if clauses:
            query += " AND " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT 1"
        cur = self.conn.cursor()
        cur.execute(query, params)
        row = cur.fetchone()
        if row is None:
            return None
        res = dict(row)
        res["completed"] = bool(res["completed"])
        if res.get("items"):
            try:
                res["items"] = json.loads(res["items"])
            except Exception:
                pass
        return res

    def runs(self, *, watcher: str | None = None, since: float | None = None) -> list[dict]:
        query = "SELECT * FROM runs"
        clauses = []
        params = []
        if watcher is not None:
            clauses.append("watcher = ?")
            params.append(watcher)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC"
        cur = self.conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        res = []
        for r in rows:
            item = dict(r)
            item["completed"] = bool(item["completed"])
            if item.get("items"):
                try:
                    item["items"] = json.loads(item["items"])
                except Exception:
                    pass
            res.append(item)
        return res

    def recent_run_tokens(self, watcher: str, n: int = 20) -> list[int]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT tokens_in + tokens_out AS total FROM runs WHERE watcher = ? AND status = 'closed' ORDER BY id DESC LIMIT ?",
            (watcher, n),
        )
        return [int(r["total"]) for r in cur.fetchall()]

    # review queue
    def enqueue_review(self, watcher: str, item_id: str, *, title: str, reason: str, payload: dict) -> int:
        now = time.time()
        payload_text = canonical(payload)
        with self._immediate_tx():
            cur = self.conn.cursor()
            cur.execute(
                """INSERT INTO reviews (created_at, watcher, item_id, title, reason, payload, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'open')""",
                (now, watcher, item_id, title, reason, payload_text),
            )
            return int(cur.lastrowid)

    def review_queue(self, status: str = "open") -> list[dict]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM reviews WHERE status = ? ORDER BY id ASC", (status,))
        rows = cur.fetchall()
        res = []
        for r in rows:
            item = dict(r)
            if item.get("payload"):
                try:
                    item["payload"] = json.loads(item["payload"])
                except Exception:
                    pass
            res.append(item)
        return res

    def resolve_review(self, review_id: int, status: str) -> None:
        now = time.time()
        with self._immediate_tx():
            self.conn.execute(
                "UPDATE reviews SET status = ?, resolved_at = ? WHERE id = ?",
                (status, now, review_id),
            )

    # counters + kv
    def bump_counter(self, key: str) -> int:
        with self._immediate_tx():
            cur = self.conn.cursor()
            cur.execute(
                "INSERT INTO counters (key, val) VALUES (?, 1) ON CONFLICT(key) DO UPDATE SET val = counters.val + 1",
                (key,),
            )
            cur.execute("SELECT val FROM counters WHERE key = ?", (key,))
            row = cur.fetchone()
            return int(row["val"])

    def get_counter(self, key: str) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT val FROM counters WHERE key = ?", (key,))
        row = cur.fetchone()
        return int(row["val"]) if row else 0

    def reset_counter(self, key: str) -> None:
        with self._immediate_tx():
            self.conn.execute("DELETE FROM counters WHERE key = ?", (key,))

    def get_kv(self, key: str, default: Any = None) -> Any:
        cur = self.conn.cursor()
        cur.execute("SELECT val FROM kv WHERE key = ?", (key,))
        row = cur.fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["val"])
        except Exception:
            return row["val"]

    def set_kv(self, key: str, value: Any) -> None:
        val_text = canonical(value)
        with self._immediate_tx():
            self.conn.execute(
                "INSERT INTO kv (key, val) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET val = excluded.val",
                (key, val_text),
            )
