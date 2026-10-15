from __future__ import annotations

import json
import pytest

from hermes_loopguard.fingerprint import canonical, dotted_get, fingerprint, item_identity


def test_canonical():
    obj1 = {"b": 2, "a": 1, "nested": {"z": 9, "y": 8}}
    obj2 = {"a": 1, "nested": {"y": 8, "z": 9}, "b": 2}
    assert canonical(obj1) == canonical(obj2)
    assert canonical(obj1) == '{"a":1,"b":2,"nested":{"y":8,"z":9}}'


def test_dotted_get():
    data = {
        "title": "Bug",
        "nested": {"author": {"name": "Alice"}},
        "items": ["x", "y"],
        "foo.bar": "direct",
    }
    assert dotted_get(data, "title") == "Bug"
    assert dotted_get(data, "nested.author.name") == "Alice"
    assert dotted_get(data, "items.1") == "y"
    assert dotted_get(data, "foo.bar") == "direct"
    assert dotted_get(data, "nested.missing", default=42) == 42
    assert dotted_get(data, "missing") is None


def test_item_identity():
    item = {"id": 123, "meta": {"uuid": "abc-456"}}
    assert item_identity(item, "id") == "123"
    assert item_identity(item, "meta.uuid") == "abc-456"

    with pytest.raises(ValueError):
        item_identity(item, "nonexistent")


def test_fingerprint():
    item1 = {"id": 1, "title": "A", "body": "Hello", "extra": "ignored"}
    item2 = {"id": 1, "title": "A", "body": "Hello", "extra": "different"}

    fp_whole1 = fingerprint(item1)
    fp_whole2 = fingerprint(item2)
    assert fp_whole1.startswith("fp-")
    assert fp_whole1 != fp_whole2

    fp_fields1 = fingerprint(item1, ["id", "title", "body"])
    fp_fields2 = fingerprint(item2, ["id", "title", "body"])
    assert fp_fields1.startswith("fp-")
    assert fp_fields1 == fp_fields2
