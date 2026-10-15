from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def dotted_get(obj: Any, path: str, default: Any = None) -> Any:
    if not path:
        return obj if obj is not None else default
    if isinstance(obj, dict) and path in obj:
        return obj[path]
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
            else:
                return default
        elif isinstance(cur, (list, tuple)):
            try:
                idx = int(part)
                cur = cur[idx]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return cur


def item_identity(item: dict, id_field: str) -> str:
    val = dotted_get(item, id_field)
    if val is None:
        raise ValueError(f"Missing id field '{id_field}' in item")
    return str(val)


def fingerprint(item: dict, fields: Sequence[str] = ()) -> str:
    if fields:
        data = {f: dotted_get(item, f) for f in fields}
    else:
        data = item
    digest = hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()[:16]
    return f"fp-{digest}"
