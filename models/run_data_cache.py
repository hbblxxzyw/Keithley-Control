"""In-memory cache for recently loaded run payloads."""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Hashable

from models.project import clone_json


RunCacheKey = tuple[str, int, int, int | None]


@dataclass
class RunDataCacheEntry:
    payloads: list[dict[str, Any]]
    estimated_bytes: int


class RunDataCache:
    """Small LRU cache for full-run payload lists."""

    def __init__(self, max_runs: int = 8, max_bytes: int = 256 * 1024 * 1024) -> None:
        self.max_runs = max(1, int(max_runs))
        self.max_bytes = max(1024, int(max_bytes))
        self._entries: OrderedDict[RunCacheKey, RunDataCacheEntry] = OrderedDict()
        self._total_bytes = 0

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def size(self) -> int:
        return len(self._entries)

    def make_key(
        self,
        workspace_path: str | Path | None,
        project_id: int,
        run_id: int,
        run_storage_id: int | None = None,
    ) -> RunCacheKey | None:
        if workspace_path is None:
            return None
        try:
            normalized_path = str(Path(workspace_path).resolve()).lower()
        except OSError:
            normalized_path = str(workspace_path).lower()
        return (normalized_path, int(project_id), int(run_id), run_storage_id)

    def get(self, key: RunCacheKey | None) -> list[dict[str, Any]] | None:
        if key is None or key not in self._entries:
            return None
        entry = self._entries.pop(key)
        self._entries[key] = entry
        return clone_json(entry.payloads)

    def set(self, key: RunCacheKey | None, payloads: list[dict[str, Any]]) -> None:
        if key is None:
            return
        if key in self._entries:
            old = self._entries.pop(key)
            self._total_bytes -= old.estimated_bytes

        entry = RunDataCacheEntry(
            payloads=clone_json(payloads),
            estimated_bytes=self._estimate_payloads_bytes(payloads),
        )
        self._entries[key] = entry
        self._total_bytes += entry.estimated_bytes
        self._evict()

    def append(self, key: RunCacheKey | None, payload: dict[str, Any]) -> None:
        if key is None:
            return
        if key not in self._entries:
            self.set(key, [payload])
            return
        entry = self._entries.pop(key)
        item = clone_json(payload)
        entry.payloads.append(item)
        added_bytes = self._estimate_payloads_bytes([item])
        entry.estimated_bytes += added_bytes
        self._total_bytes += added_bytes
        self._entries[key] = entry
        self._evict()

    def invalidate(self, key: RunCacheKey | None) -> None:
        if key is None or key not in self._entries:
            return
        entry = self._entries.pop(key)
        self._total_bytes -= entry.estimated_bytes

    def invalidate_project(self, workspace_path: str | Path | None, project_id: int) -> None:
        normalized_path = self._normalize_workspace_path(workspace_path)
        if normalized_path is None:
            return
        self._delete_matching(
            lambda key: key[0] == normalized_path and key[1] == int(project_id)
        )

    def clear_workspace(self, workspace_path: str | Path | None) -> None:
        normalized_path = self._normalize_workspace_path(workspace_path)
        if normalized_path is None:
            self.clear()
            return
        self._delete_matching(lambda key: key[0] == normalized_path)

    def clear(self) -> None:
        self._entries.clear()
        self._total_bytes = 0

    def _delete_matching(self, predicate: Any) -> None:
        for key in list(self._entries.keys()):
            if predicate(key):
                self.invalidate(key)

    def _evict(self) -> None:
        while self._entries and (
            len(self._entries) > self.max_runs or self._total_bytes > self.max_bytes
        ):
            _key, entry = self._entries.popitem(last=False)
            self._total_bytes -= entry.estimated_bytes

    def _normalize_workspace_path(self, workspace_path: str | Path | None) -> str | None:
        if workspace_path is None:
            return None
        try:
            return str(Path(workspace_path).resolve()).lower()
        except OSError:
            return str(workspace_path).lower()

    def _estimate_payloads_bytes(self, payloads: list[dict[str, Any]]) -> int:
        try:
            json_bytes = len(json.dumps(payloads, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            json_bytes = sum(len(str(payload)) for payload in payloads)
        return max(1, json_bytes * 3)
