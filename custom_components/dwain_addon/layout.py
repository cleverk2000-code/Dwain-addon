"""Transactional, allowlisted layout changes for Dwains Lovelace 3.x."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

import yaml

SCOPES = ("areas", "entities")
ORDER = {"sort_order", "grouped_sort_order"}
BOOL = {
    "areas": {"disabled", "hide_icon"},
    "entities": {"hidden", "hidden_in_area", "excluded", "disabled", "favorite"},
}
SPAN = {f"{axis}_span{suffix}": maximum
        for axis in ("row", "col")
        for suffix, maximum in (("", 2), ("_lg", 3), ("_xl", 4))}
MAX_BYTES = 8 * 1024 * 1024


class LayoutError(ValueError):
    """Invalid, stale, or unsafe layout operation."""


class StrictLoader(yaml.SafeLoader):
    """Reject duplicate mapping keys instead of silently losing settings."""


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str) or key in result:
            raise LayoutError("YAML mapping has a duplicate or non-string key")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _parse(raw):
    try:
        value = yaml.load(raw, Loader=StrictLoader) if raw is not None else {}
        if value is None:
            value = {}
        if not isinstance(value, dict) or any(
            not isinstance(item, dict) for item in value.values()
        ):
            raise LayoutError("Expected a mapping of IDs to settings")
        # Reject non-JSON types, circular aliases, and non-finite numbers.
        return json.loads(json.dumps(value, allow_nan=False))
    except (yaml.YAMLError, TypeError, ValueError, RecursionError) as exc:
        raise LayoutError("Unsupported or malformed Dwains YAML") from exc


def _revision(raw):
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _read(path):
    if path.is_symlink():
        raise LayoutError("Symbolic links are not supported")
    try:
        with path.open("rb") as handle:
            value = handle.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(value) > MAX_BYTES:
        raise LayoutError("Layout file is too large")
    return value.decode("utf-8")


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic(path, raw):
    if path.is_symlink():
        raise LayoutError("Symbolic links are not supported")
    if raw is None:
        path.unlink(missing_ok=True)
        if path.parent.exists():
            _sync_dir(path.parent)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".dwain-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(name, path.stat().st_mode & 0o777)
        os.replace(name, path)
        _sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _value(scope, key, value):
    if key in BOOL[scope]:
        if type(value) is bool:
            return value
    elif key in ORDER or (scope == "entities" and key in {
        "favorite_sort_order", "devices_sort_order", "devices_grouped_sort_order"
    }):
        if type(value) is int and 1 <= value <= 10000:
            return value
    elif scope == "entities" and key in SPAN:
        if type(value) is int:
            value = str(value)
        if isinstance(value, str) and value in {str(i) for i in range(1, SPAN[key] + 1)}:
            return value
    elif key == "icon" and scope == "areas":
        if isinstance(value, str) and re.fullmatch(r"[a-z0-9_-]+:[a-z0-9_-]+", value):
            return value
    elif key == "friendly_name" and scope == "entities":
        if isinstance(value, str) and len(value) <= 150:
            return value
    raise LayoutError(f"Unsupported setting or value: {scope}.{key}")


class LayoutStore:
    def __init__(self, config_dir):
        self.root = Path(config_dir).resolve()
        self.config = self.root / "dwains-dashboard" / "configs"
        self.backups = self.root / ".storage" / "dwain_addon_layout"
        self.pending = self.backups / "pending.json"

    def _guard(self):
        for path in (self.config, self.backups):
            if path.resolve() != path:
                raise LayoutError("Unexpected symbolic-link directory")

    def raw(self):
        self._guard()
        return {scope: _read(self.config / (scope + ".yaml")) for scope in SCOPES}

    def snapshot(self):
        raw = self.raw()
        return {"revision": _revision(raw),
                "configuration": {scope: _parse(raw[scope]) for scope in SCOPES},
                "pending": self.pending_id(), "backups": self.list_backups()}

    def pending_id(self):
        self._guard()
        raw = _read(self.pending)
        if raw is None:
            return None
        value = json.loads(raw)
        self._backup_path(value)
        return value

    def _backup_path(self, backup_id):
        if not isinstance(backup_id, str) or not re.fullmatch("[0-9a-f]{32}", backup_id):
            raise LayoutError("Invalid backup ID")
        return self.backups / (backup_id + ".json")

    def _record(self, backup_id):
        self._guard()
        raw = _read(self._backup_path(backup_id))
        if raw is None:
            raise LayoutError("Backup not found")
        record = json.loads(raw)
        if not isinstance(record, dict) or record.get("id") != backup_id:
            raise LayoutError("Invalid backup")
        for field in ("before", "after"):
            mapping = record.get(field)
            if not isinstance(mapping, dict) or set(mapping) != set(SCOPES):
                raise LayoutError("Invalid backup scopes")
            for value in mapping.values():
                if value is not None and not isinstance(value, str):
                    raise LayoutError("Invalid backup content")
                _parse(value)
        return record

    def list_backups(self):
        self._guard()
        if not self.backups.exists():
            return []
        files = sorted(self.backups.glob("[0-9a-f]" * 32 + ".json"),
                       key=lambda p: p.stat().st_mtime_ns, reverse=True)
        return [p.stem for p in files[:100]]

    def _check(self, raw, expected):
        if self.pending_id() is not None:
            raise LayoutError("Interrupted transaction: recover before changing layout")
        if not isinstance(expected, str) or _revision(raw) != expected:
            raise LayoutError("Stale revision: read and preview again")

    def plan(self, patch, expected, allowed):
        raw = self.raw()
        self._check(raw, expected)
        if not isinstance(patch, dict) or not patch or set(patch) - set(SCOPES):
            raise LayoutError("Patch must contain areas and/or entities")
        result = dict(raw)
        diff = []
        for scope, updates in patch.items():
            if not isinstance(updates, dict) or len(updates) > 5000:
                raise LayoutError("Invalid patch mapping")
            data = _parse(raw[scope])
            for ident, fields in updates.items():
                if not isinstance(ident, str) or ident not in allowed[scope]:
                    raise LayoutError(f"Unknown {scope} ID: {ident}")
                if not isinstance(fields, dict) or not fields:
                    raise LayoutError("Expected nonempty settings mapping")
                current = data.get(ident, {})
                for key, new in fields.items():
                    if not isinstance(key, str):
                        raise LayoutError("Invalid setting key")
                    new = _value(scope, key, new)
                    old = current.get(key)
                    if key not in current or type(old) is not type(new) or old != new:
                        diff.append({"scope": scope, "id": ident, "key": key,
                                     "before": old, "after": new})
                        current[key] = new
                data[ident] = current
            if any(item["scope"] == scope for item in diff):
                result[scope] = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
                if _parse(result[scope]) != data:
                    raise LayoutError("YAML round-trip verification failed")
        return raw, result, diff

    def preview(self, patch, expected, allowed):
        raw, after, diff = self.plan(patch, expected, allowed)
        return {"revision": _revision(raw), "next_revision": _revision(after),
                "changes": diff}

    def _write_changes(self, before, after):
        for scope in SCOPES:
            if before[scope] != after[scope]:
                _atomic(self.config / (scope + ".yaml"), after[scope])
        if self.raw() != after:
            raise OSError("Layout read-back verification failed")

    def _transaction(self, before, after):
        if self.raw() != before:
            raise LayoutError("Layout changed during planning")
        if before == after:
            return {"revision": _revision(before), "backup_id": None, "changed": False}
        backup_id = uuid.uuid4().hex
        record = {"id": backup_id, "before": before, "after": after}
        serialized = json.dumps(record, ensure_ascii=False)
        if len(serialized.encode()) > MAX_BYTES:
            raise LayoutError("Backup is too large")
        _atomic(self._backup_path(backup_id), serialized)
        _atomic(self.pending, json.dumps(backup_id))
        try:
            self._write_changes(before, after)
        except Exception:
            # Recover only if files still match one of the known transaction versions.
            self.recover(backup_id)
            raise
        _atomic(self.pending, None)
        return {"revision": _revision(after), "backup_id": backup_id, "changed": True}

    def apply(self, patch, expected, allowed):
        before, after, _ = self.plan(patch, expected, allowed)
        return self._transaction(before, after)

    def restore_plan(self, backup_id, expected):
        before = self.raw()
        self._check(before, expected)
        record = self._record(backup_id)
        after = dict(before)
        for scope in SCOPES:
            if record["before"][scope] != record["after"][scope]:
                if before[scope] != record["after"][scope]:
                    raise LayoutError("Later changes exist; restore would overwrite them")
                after[scope] = record["before"][scope]
        return before, after

    def restore_preview(self, backup_id, expected):
        before, after = self.restore_plan(backup_id, expected)
        return {"revision": _revision(before), "next_revision": _revision(after),
                "files": [s for s in SCOPES if before[s] != after[s]]}

    def restore(self, backup_id, expected):
        before, after = self.restore_plan(backup_id, expected)
        return self._transaction(before, after)

    def recover(self, backup_id):
        if self.pending_id() != backup_id or backup_id is None:
            raise LayoutError("Not the pending transaction")
        record = self._record(backup_id)
        current = self.raw()
        for scope in SCOPES:
            if current[scope] not in (record["before"][scope], record["after"][scope]):
                raise LayoutError("External change detected; manual recovery required")
        self._write_changes(current, record["before"])
        _atomic(self.pending, None)
        return {"revision": _revision(record["before"]), "recovered": True}
