"""Workspace file model for Keithley Control.

A workspace is the user-level document. It owns reusable quick configurations
and multiple projects. Each project owns its own RUN id sequence and measured
run records.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKSPACE_FORMAT = "keithley-control-workspace"
PROJECT_FORMAT = "keithley-control-project"
LEGACY_CONFIG_FORMAT = "keithley-gui-config"
WORKSPACE_VERSION = 1
PROJECT_VERSION = 1
MAX_NAME_LENGTH = 30
MAX_QUICK_CONFIG_NAME_LENGTH = 24
MAX_QUICK_ACCESS_CONFIGS = 3
DEFAULT_GRAPH_SETTINGS = {
    "x_axis": "SMU1 Voltage",
    "y_axis": "SMU1 Current",
    "display_mode": "linear",
    "show_ramping": True,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clone_json(value: Any) -> Any:
    return copy.deepcopy(value)


def clamp_name(name: str, fallback: str = "") -> str:
    cleaned = " ".join(str(name or fallback).split())
    return cleaned[:MAX_NAME_LENGTH]


def clamp_run_name(name: str) -> str:
    return clamp_name(name)


def clamp_quick_config_name(name: str, fallback: str = "") -> str:
    cleaned = " ".join(str(name or fallback).split())
    return cleaned[:MAX_QUICK_CONFIG_NAME_LENGTH]


@dataclass
class QuickConfig:
    id: int
    name: str
    settings: dict[str, Any]
    quick_access: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": int(self.id),
            "name": str(self.name),
            "settings": clone_json(self.settings),
            "quick_access": bool(self.quick_access),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "QuickConfig":
        return cls(
            id=int(payload.get("id", 0)),
            name=clamp_quick_config_name(str(payload.get("name", "Config")), "Config"),
            settings=clone_json(payload.get("settings", {})),
            quick_access=bool(payload.get("quick_access", False)),
        )


@dataclass
class RunRecord:
    id: int
    name: str
    settings: dict[str, Any]
    storage_id: int | None = None
    status: str = "ready"
    created_at: str = field(default_factory=utc_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    graph: dict[str, Any] = field(default_factory=dict)
    data: list[dict[str, Any]] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        suffix = f"  {self.name}" if self.name else ""
        return f"RUN {self.id}{suffix}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": int(self.id),
            "name": clamp_run_name(self.name),
            "status": str(self.status),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "settings": clone_json(self.settings),
            "graph": clone_json(self.graph),
            "data": clone_json(self.data),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RunRecord":
        graph = clone_json(DEFAULT_GRAPH_SETTINGS)
        loaded_graph = payload.get("graph", {})
        if isinstance(loaded_graph, dict):
            graph.update(clone_json(loaded_graph))
        data = payload.get("data", [])
        if not isinstance(data, list):
            data = []
        return cls(
            id=int(payload.get("id", 0)),
            name=clamp_run_name(str(payload.get("name", ""))),
            storage_id=None,
            status=str(payload.get("status", "ready")),
            created_at=str(payload.get("created_at") or utc_now_iso()),
            started_at=payload.get("started_at"),
            finished_at=payload.get("finished_at"),
            settings=clone_json(payload.get("settings", {})),
            graph=graph,
            data=clone_json([item for item in data if isinstance(item, dict)]),
        )


@dataclass
class ProjectRecord:
    id: int
    name: str
    default_settings: dict[str, Any]
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    next_run_id: int = 1
    active_run_id: int | None = None
    runs: list[RunRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": int(self.id),
            "name": clamp_name(self.name, f"Project {self.id}"),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "next_run_id": int(self.next_run_id),
            "active_run_id": self.active_run_id,
            "default_settings": clone_json(self.default_settings),
            "runs": [run.to_dict() for run in self.runs],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProjectRecord":
        runs = [
            RunRecord.from_dict(item)
            for item in payload.get("runs", [])
            if isinstance(item, dict)
        ]
        project_id = int(payload.get("id", 0))
        max_run_id = max((run.id for run in runs), default=0)
        return cls(
            id=project_id,
            name=clamp_name(str(payload.get("name", "")), f"Project {project_id}"),
            created_at=str(payload.get("created_at") or utc_now_iso()),
            updated_at=str(payload.get("updated_at") or utc_now_iso()),
            next_run_id=max(int(payload.get("next_run_id", max_run_id + 1)), max_run_id + 1),
            active_run_id=payload.get("active_run_id"),
            default_settings=clone_json(payload.get("default_settings", {})),
            runs=runs,
        )

    def mark_dirty(self) -> None:
        self.updated_at = utc_now_iso()

    def get_run(self, run_id: int | None) -> RunRecord | None:
        if run_id is None:
            return None
        for run in self.runs:
            if run.id == int(run_id):
                return run
        return None

    def active_run(self) -> RunRecord | None:
        return self.get_run(self.active_run_id)

    def add_run(self, settings: dict[str, Any], name: str = "") -> RunRecord:
        run = RunRecord(
            id=int(self.next_run_id),
            name=clamp_run_name(name),
            settings=clone_json(settings),
            graph=clone_json(DEFAULT_GRAPH_SETTINGS),
        )
        self.next_run_id += 1
        self.runs.append(run)
        self.active_run_id = run.id
        self.default_settings = clone_json(settings)
        self.mark_dirty()
        return run

    def delete_run(self, run_id: int) -> None:
        self.runs = [run for run in self.runs if run.id != int(run_id)]
        if self.active_run_id == int(run_id):
            self.active_run_id = self.runs[-1].id if self.runs else None
        self.mark_dirty()

    def rename_run(self, run_id: int, name: str) -> None:
        run = self.get_run(run_id)
        if run is None:
            return
        run.name = clamp_run_name(name)
        self.mark_dirty()


@dataclass
class WorkspaceDocument:
    name: str = "Untitled Workspace"
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    next_project_id: int = 1
    next_config_id: int = 1
    active_project_id: int | None = None
    quick_configs: list[QuickConfig] = field(default_factory=list)
    projects: list[ProjectRecord] = field(default_factory=list)
    path: Path | None = None
    dirty: bool = False

    @classmethod
    def new(cls, default_settings: dict[str, Any] | None = None) -> "WorkspaceDocument":
        workspace = cls(dirty=False)
        workspace.add_project("Project 1", default_settings or {}, mark_dirty=False)
        workspace.dirty = False
        return workspace

    @classmethod
    def from_dict(cls, payload: dict[str, Any], path: Path | None = None) -> "WorkspaceDocument":
        if payload.get("format") == LEGACY_CONFIG_FORMAT:
            settings = payload.get("settings", {})
            workspace = cls.new(settings if isinstance(settings, dict) else {})
            workspace.name = path.stem if path else "Imported Configuration"
            workspace.add_quick_config("Imported Config", settings if isinstance(settings, dict) else {})
            workspace.path = path
            workspace.dirty = True
            return workspace

        if payload.get("format") == PROJECT_FORMAT:
            return cls.from_legacy_project(payload, path)

        if payload.get("format") != WORKSPACE_FORMAT:
            raise ValueError("Not a Keithley Control workspace file.")

        meta = payload.get("workspace", {})
        projects = [
            ProjectRecord.from_dict(item)
            for item in payload.get("projects", [])
            if isinstance(item, dict)
        ]
        quick_configs = [
            QuickConfig.from_dict(item)
            for item in payload.get("quick_configs", [])
            if isinstance(item, dict)
        ]
        if quick_configs and not any(
            "quick_access" in item
            for item in payload.get("quick_configs", [])
            if isinstance(item, dict)
        ):
            for config in quick_configs[:MAX_QUICK_ACCESS_CONFIGS]:
                config.quick_access = True
        max_project_id = max((project.id for project in projects), default=0)
        max_config_id = max((config.id for config in quick_configs), default=0)
        workspace = cls(
            name=str(meta.get("name") or (path.stem if path else "Untitled Workspace")),
            created_at=str(meta.get("created_at") or utc_now_iso()),
            updated_at=str(meta.get("updated_at") or utc_now_iso()),
            next_project_id=max(
                int(meta.get("next_project_id", max_project_id + 1)),
                max_project_id + 1,
            ),
            next_config_id=max(
                int(meta.get("next_config_id", max_config_id + 1)),
                max_config_id + 1,
            ),
            active_project_id=payload.get("active_project_id"),
            quick_configs=quick_configs,
            projects=projects,
            path=path,
            dirty=False,
        )
        workspace._ensure_active_project()
        return workspace

    @classmethod
    def from_legacy_project(
        cls,
        payload: dict[str, Any],
        path: Path | None = None,
    ) -> "WorkspaceDocument":
        meta = payload.get("project", {})
        runs = [
            RunRecord.from_dict(item)
            for item in payload.get("runs", [])
            if isinstance(item, dict)
        ]
        quick_configs = [
            QuickConfig.from_dict(item)
            for item in payload.get("quick_configs", [])
            if isinstance(item, dict)
        ]
        if quick_configs and not any(
            "quick_access" in item
            for item in payload.get("quick_configs", [])
            if isinstance(item, dict)
        ):
            for config in quick_configs[:MAX_QUICK_ACCESS_CONFIGS]:
                config.quick_access = True
        max_run_id = max((run.id for run in runs), default=0)
        project_name = str(meta.get("name") or (path.stem if path else "Imported Project"))
        default_settings = runs[-1].settings if runs else {}
        project = ProjectRecord(
            id=1,
            name=clamp_name(project_name, "Imported Project"),
            created_at=str(meta.get("created_at") or utc_now_iso()),
            updated_at=str(meta.get("updated_at") or utc_now_iso()),
            next_run_id=max(int(meta.get("next_run_id", max_run_id + 1)), max_run_id + 1),
            active_run_id=payload.get("active_run_id"),
            default_settings=clone_json(default_settings),
            runs=runs,
        )
        max_config_id = max((config.id for config in quick_configs), default=0)
        workspace = cls(
            name=project_name,
            next_project_id=2,
            next_config_id=max_config_id + 1,
            active_project_id=1,
            quick_configs=quick_configs,
            projects=[project],
            path=path,
            dirty=True,
        )
        return workspace

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": WORKSPACE_FORMAT,
            "version": WORKSPACE_VERSION,
            "workspace": {
                "name": self.name,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "next_project_id": int(self.next_project_id),
                "next_config_id": int(self.next_config_id),
            },
            "active_project_id": self.active_project_id,
            "quick_configs": [config.to_dict() for config in self.quick_configs],
            "projects": [project.to_dict() for project in self.projects],
        }

    @classmethod
    def load(cls, file_path: str | Path) -> "WorkspaceDocument":
        path = Path(file_path)
        from models import workspace_store

        if workspace_store.is_sqlite_workspace_path(path):
            return workspace_store.load_workspace_document(path)

        sqlite_path = workspace_store.migrate_json_workspace_to_sqlite(path)
        return workspace_store.load_workspace_document(sqlite_path)

    @classmethod
    def load_json(cls, file_path: str | Path) -> "WorkspaceDocument":
        path = Path(file_path)
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict):
            raise ValueError("Workspace file must contain a JSON object.")
        return cls.from_dict(payload, path=path)

    def save(self, file_path: str | Path | None = None) -> None:
        previous_path = self.path
        if file_path is not None:
            self.path = Path(file_path)
            self.name = self.path.stem
        if self.path is None:
            raise ValueError("Workspace has no save path.")
        self.updated_at = utc_now_iso()
        from models import workspace_store

        if workspace_store.is_sqlite_workspace_path(self.path):
            previous_was_sqlite = (
                previous_path is not None
                and workspace_store.is_sqlite_workspace_path(previous_path)
                and previous_path.exists()
            )
            if (
                file_path is not None
                and previous_was_sqlite
                and previous_path != self.path
            ):
                import shutil

                self.path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(previous_path, self.path)
                workspace_store.save_workspace_document(
                    self,
                    self.path,
                    include_run_data=False,
                    replace=False,
                )
                self.dirty = False
                return

            include_run_data = (file_path is not None and not previous_was_sqlite) or not self.path.exists()
            replace = file_path is not None and previous_path != self.path and not previous_was_sqlite
            workspace_store.save_workspace_document(
                self,
                self.path,
                include_run_data=include_run_data,
                replace=replace,
            )
            self.dirty = False
            return

        with self.path.open("w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
        self.dirty = False

    def append_run_point(
        self,
        project_id: int,
        run_id: int,
        payload: dict[str, Any],
    ) -> None:
        if self.path is None:
            return
        from models import workspace_store

        if not workspace_store.is_sqlite_workspace_path(self.path):
            return
        project = self.get_project(project_id)
        run = project.get_run(run_id) if project is not None else None
        if run is None:
            return
        if run.storage_id is None:
            self.save()
        if run.storage_id is None:
            return
        workspace_store.append_run_point(self.path, int(run.storage_id), payload)

    def load_run_points(
        self,
        run: RunRecord,
        progress_callback: Any | None = None,
    ) -> list[dict[str, Any]]:
        if self.path is None or run.storage_id is None:
            return list(run.data)
        from models import workspace_store

        if not workspace_store.is_sqlite_workspace_path(self.path):
            return list(run.data)
        run.data = workspace_store.load_run_points(
            self.path,
            int(run.storage_id),
            progress_callback=progress_callback,
        )
        return list(run.data)

    def mark_dirty(self) -> None:
        self.updated_at = utc_now_iso()
        self.dirty = True

    def _ensure_active_project(self) -> None:
        if not self.projects:
            self.add_project("Project 1", {}, mark_dirty=False)
        if self.get_project(self.active_project_id) is None:
            self.active_project_id = self.projects[-1].id

    def get_project(self, project_id: int | None) -> ProjectRecord | None:
        if project_id is None:
            return None
        for project in self.projects:
            if project.id == int(project_id):
                return project
        return None

    def active_project(self) -> ProjectRecord:
        self._ensure_active_project()
        project = self.get_project(self.active_project_id)
        if project is None:
            raise RuntimeError("Workspace has no active project.")
        return project

    def add_project(
        self,
        name: str | None = None,
        default_settings: dict[str, Any] | None = None,
        *,
        mark_dirty: bool = True,
    ) -> ProjectRecord:
        project_id = int(self.next_project_id)
        project = ProjectRecord(
            id=project_id,
            name=clamp_name(name or f"Project {project_id}", f"Project {project_id}"),
            default_settings=clone_json(default_settings or {}),
        )
        self.next_project_id += 1
        self.projects.append(project)
        self.active_project_id = project.id
        if mark_dirty:
            self.mark_dirty()
        return project

    def delete_project(self, project_id: int) -> None:
        if len(self.projects) <= 1:
            return
        self.projects = [
            project for project in self.projects if project.id != int(project_id)
        ]
        if self.active_project_id == int(project_id):
            self.active_project_id = self.projects[-1].id if self.projects else None
        self._ensure_active_project()
        self.mark_dirty()

    def rename_project(self, project_id: int, name: str) -> None:
        project = self.get_project(project_id)
        if project is None:
            return
        project.name = clamp_name(name, project.name)
        project.mark_dirty()
        self.mark_dirty()

    def add_quick_config(self, name: str, settings: dict[str, Any]) -> QuickConfig:
        quick_access_count = sum(1 for config in self.quick_configs if config.quick_access)
        config = QuickConfig(
            id=int(self.next_config_id),
            name=clamp_quick_config_name(
                name or f"Config {self.next_config_id}",
                f"Config {self.next_config_id}",
            ),
            settings=clone_json(settings),
            quick_access=quick_access_count < MAX_QUICK_ACCESS_CONFIGS,
        )
        self.next_config_id += 1
        self.quick_configs.append(config)
        self.mark_dirty()
        return config

    def get_quick_config(self, config_id: int) -> QuickConfig | None:
        for config in self.quick_configs:
            if config.id == int(config_id):
                return config
        return None

    def rename_quick_config(self, config_id: int, name: str) -> None:
        config = self.get_quick_config(config_id)
        if config is None:
            return
        config.name = clamp_quick_config_name(name, config.name)
        self.mark_dirty()

    def set_quick_config_quick_access(self, config_id: int, enabled: bool) -> bool:
        config = self.get_quick_config(config_id)
        if config is None:
            return False
        enabled = bool(enabled)
        if enabled and not config.quick_access:
            quick_access_count = sum(
                1 for item in self.quick_configs if item.quick_access
            )
            if quick_access_count >= MAX_QUICK_ACCESS_CONFIGS:
                return False
        config.quick_access = enabled
        self.mark_dirty()
        return True

    def delete_quick_config(self, config_id: int) -> None:
        self.quick_configs = [
            config for config in self.quick_configs if config.id != int(config_id)
        ]
        self.mark_dirty()


# Backward-compatible import name for older code during migration.
ProjectDocument = WorkspaceDocument
