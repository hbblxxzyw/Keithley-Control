"""SQLite storage for Keithley Control workspaces."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from models.project import (
    DEFAULT_GRAPH_SETTINGS,
    QuickConfig,
    RunRecord,
    ProjectRecord,
    WorkspaceDocument,
    clone_json,
    utc_now_iso,
)


SQLITE_WORKSPACE_SUFFIX = ".keithley-workspace.sqlite"
ProgressCallback = Callable[[int, int, str], None]


def is_sqlite_workspace_path(path: str | Path) -> bool:
    return str(path).lower().endswith((".sqlite", ".db", ".sqlite3"))


def default_sqlite_path_for_json(path: str | Path) -> Path:
    source = Path(path)
    name = source.name
    if name.lower().endswith(".keithley-workspace.json"):
        return source.with_name(f"{name[:-len('.keithley-workspace.json')]}{SQLITE_WORKSPACE_SUFFIX}")
    if name.lower().endswith(".json"):
        return source.with_name(f"{name[:-len('.json')]}{SQLITE_WORKSPACE_SUFFIX}")
    return source.with_suffix(SQLITE_WORKSPACE_SUFFIX)


@contextmanager
def _connect(path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return clone_json(fallback)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return clone_json(fallback)


def initialize_database(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS workspace_meta(
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS projects(
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT,
            next_run_id INTEGER NOT NULL,
            active_run_id INTEGER,
            default_settings_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS quick_configs(
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            quick_access INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS runs(
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            run_number INTEGER NOT NULL,
            name TEXT,
            status TEXT,
            created_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            settings_json TEXT NOT NULL,
            graph_json TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(project_id, run_number)
        );

        CREATE TABLE IF NOT EXISTS run_points(
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            point_index INTEGER NOT NULL,
            time_s REAL,
            series_name TEXT,
            primary_name TEXT,
            stepper_name TEXT,
            smu1_source_v REAL,
            smu1_voltage REAL,
            smu1_current REAL,
            smu1_resistance REAL,
            smu2_source_v REAL,
            smu2_voltage REAL,
            smu2_current REAL,
            smu2_resistance REAL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE,
            UNIQUE(run_id, point_index)
        );

        CREATE INDEX IF NOT EXISTS idx_run_points_run_index
            ON run_points(run_id, point_index);
        """
    )
    _ensure_quick_config_schema(conn)


def _ensure_quick_config_schema(conn: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(quick_configs)")
    }
    if "quick_access" in columns:
        return
    conn.execute(
        "ALTER TABLE quick_configs ADD COLUMN quick_access INTEGER NOT NULL DEFAULT 0"
    )
    conn.execute(
        """
        UPDATE quick_configs
        SET quick_access = 1
        WHERE id IN (
            SELECT id FROM quick_configs ORDER BY id LIMIT 3
        )
        """
    )


def save_workspace_document(
    document: WorkspaceDocument,
    path: str | Path,
    *,
    include_run_data: bool = False,
    replace: bool = False,
) -> None:
    sqlite_path = Path(path)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    if replace and sqlite_path.exists():
        sqlite_path.unlink()

    with _connect(sqlite_path) as conn:
        initialize_database(conn)
        with conn:
            _save_workspace_meta(conn, document)
            _save_quick_configs(conn, document)
            _save_projects_and_runs(conn, document)
            if include_run_data:
                _save_all_run_points(conn, document)


def load_workspace_document(path: str | Path) -> WorkspaceDocument:
    sqlite_path = Path(path)
    with _connect(sqlite_path) as conn:
        initialize_database(conn)
        meta = {
            str(row["key"]): row["value"]
            for row in conn.execute("SELECT key, value FROM workspace_meta")
        }
        quick_configs = [
            QuickConfig(
                id=int(row["id"]),
                name=str(row["name"]),
                settings=_json_loads(row["settings_json"], {}),
                quick_access=bool(row["quick_access"]),
            )
            for row in conn.execute("SELECT * FROM quick_configs ORDER BY id")
        ]
        projects: list[ProjectRecord] = []
        for project_row in conn.execute("SELECT * FROM projects ORDER BY id"):
            runs: list[RunRecord] = []
            for run_row in conn.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY run_number",
                (int(project_row["id"]),),
            ):
                graph = clone_json(DEFAULT_GRAPH_SETTINGS)
                loaded_graph = _json_loads(run_row["graph_json"], {})
                if isinstance(loaded_graph, dict):
                    graph.update(loaded_graph)
                runs.append(
                    RunRecord(
                        id=int(run_row["run_number"]),
                        storage_id=int(run_row["id"]),
                        name=str(run_row["name"] or ""),
                        status=str(run_row["status"] or "ready"),
                        created_at=str(run_row["created_at"] or utc_now_iso()),
                        started_at=run_row["started_at"],
                        finished_at=run_row["finished_at"],
                        settings=_json_loads(run_row["settings_json"], {}),
                        graph=graph,
                        data=[],
                    )
                )
            projects.append(
                ProjectRecord(
                    id=int(project_row["id"]),
                    name=str(project_row["name"]),
                    created_at=str(project_row["created_at"] or utc_now_iso()),
                    updated_at=str(project_row["updated_at"] or utc_now_iso()),
                    next_run_id=int(project_row["next_run_id"]),
                    active_run_id=project_row["active_run_id"],
                    default_settings=_json_loads(project_row["default_settings_json"], {}),
                    runs=runs,
                )
            )

    max_project_id = max((project.id for project in projects), default=0)
    max_config_id = max((config.id for config in quick_configs), default=0)
    document = WorkspaceDocument(
        name=str(meta.get("name") or sqlite_path.stem),
        created_at=str(meta.get("created_at") or utc_now_iso()),
        updated_at=str(meta.get("updated_at") or utc_now_iso()),
        next_project_id=max(int(meta.get("next_project_id") or max_project_id + 1), max_project_id + 1),
        next_config_id=max(int(meta.get("next_config_id") or max_config_id + 1), max_config_id + 1),
        active_project_id=_nullable_int(meta.get("active_project_id")),
        quick_configs=quick_configs,
        projects=projects,
        path=sqlite_path,
        dirty=False,
    )
    document._ensure_active_project()
    return document


def append_run_point(path: str | Path, run_storage_id: int, payload: dict[str, Any]) -> None:
    with _connect(path) as conn:
        initialize_database(conn)
        with conn:
            next_index = conn.execute(
                "SELECT COALESCE(MAX(point_index) + 1, 0) FROM run_points WHERE run_id = ?",
                (int(run_storage_id),),
            ).fetchone()[0]
            _insert_run_point(conn, int(run_storage_id), int(next_index), payload)


def replace_run_points(
    path: str | Path,
    run_storage_id: int,
    payloads: list[dict[str, Any]],
) -> None:
    with _connect(path) as conn:
        initialize_database(conn)
        with conn:
            conn.execute("DELETE FROM run_points WHERE run_id = ?", (int(run_storage_id),))
            for index, payload in enumerate(payloads):
                _insert_run_point(conn, int(run_storage_id), index, payload)


def load_run_points(
    path: str | Path,
    run_storage_id: int,
    progress_callback: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    with _connect(path) as conn:
        initialize_database(conn)
        total = int(
            conn.execute(
                "SELECT COUNT(*) FROM run_points WHERE run_id = ?",
                (int(run_storage_id),),
            ).fetchone()[0]
        )
        if progress_callback is not None:
            progress_callback(0, total, "Loading data")
        payloads: list[dict[str, Any]] = []
        cursor = conn.execute(
            """
            SELECT payload_json
            FROM run_points
            WHERE run_id = ?
            ORDER BY point_index
            """,
            (int(run_storage_id),),
        )
        notify_every = max(1, total // 100) if total else 1
        for index, row in enumerate(cursor, start=1):
            payload = _json_loads(row["payload_json"], {})
            if isinstance(payload, dict):
                payloads.append(payload)
            if progress_callback is not None and (index == total or index % notify_every == 0):
                progress_callback(index, total, "Loading data")
        return payloads


def migrate_json_workspace_to_sqlite(
    json_path: str | Path,
    sqlite_path: str | Path | None = None,
) -> Path:
    from models.project import WorkspaceDocument

    source_path = Path(json_path)
    target_path = Path(sqlite_path) if sqlite_path is not None else default_sqlite_path_for_json(source_path)
    document = WorkspaceDocument.load_json(source_path)
    document.path = target_path
    document.name = target_path.stem
    save_workspace_document(document, target_path, include_run_data=True, replace=True)
    return target_path


def _save_workspace_meta(conn: sqlite3.Connection, document: WorkspaceDocument) -> None:
    values = {
        "format": "keithley-control-workspace",
        "version": "2",
        "name": str(document.name),
        "created_at": str(document.created_at),
        "updated_at": str(document.updated_at),
        "next_project_id": str(int(document.next_project_id)),
        "next_config_id": str(int(document.next_config_id)),
        "active_project_id": "" if document.active_project_id is None else str(int(document.active_project_id)),
    }
    conn.executemany(
        """
        INSERT INTO workspace_meta(key, value)
        VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        values.items(),
    )


def _save_quick_configs(conn: sqlite3.Connection, document: WorkspaceDocument) -> None:
    keep_ids = [int(config.id) for config in document.quick_configs]
    _delete_missing(conn, "quick_configs", keep_ids)
    conn.executemany(
        """
        INSERT INTO quick_configs(id, name, settings_json, quick_access)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            settings_json = excluded.settings_json,
            quick_access = excluded.quick_access
        """,
        [
            (
                int(config.id),
                str(config.name),
                _json_dumps(config.settings),
                1 if config.quick_access else 0,
            )
            for config in document.quick_configs
        ],
    )


def _save_projects_and_runs(conn: sqlite3.Connection, document: WorkspaceDocument) -> None:
    keep_project_ids = [int(project.id) for project in document.projects]
    _delete_missing(conn, "projects", keep_project_ids)

    for project in document.projects:
        conn.execute(
            """
            INSERT INTO projects(
                id, name, created_at, updated_at, next_run_id, active_run_id,
                default_settings_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                next_run_id = excluded.next_run_id,
                active_run_id = excluded.active_run_id,
                default_settings_json = excluded.default_settings_json
            """,
            (
                int(project.id),
                str(project.name),
                str(project.created_at),
                str(project.updated_at),
                int(project.next_run_id),
                project.active_run_id,
                _json_dumps(project.default_settings),
            ),
        )

        keep_storage_ids: list[int] = []
        for run in project.runs:
            storage_id = _upsert_run(conn, int(project.id), run)
            run.storage_id = storage_id
            keep_storage_ids.append(storage_id)
        if keep_storage_ids:
            placeholders = ",".join("?" for _ in keep_storage_ids)
            conn.execute(
                f"DELETE FROM runs WHERE project_id = ? AND id NOT IN ({placeholders})",
                (int(project.id), *keep_storage_ids),
            )
        else:
            conn.execute("DELETE FROM runs WHERE project_id = ?", (int(project.id),))


def _upsert_run(conn: sqlite3.Connection, project_id: int, run: RunRecord) -> int:
    graph = clone_json(DEFAULT_GRAPH_SETTINGS)
    if isinstance(run.graph, dict):
        graph.update(run.graph)
    storage_id = run.storage_id
    if storage_id is None:
        row = conn.execute(
            "SELECT id FROM runs WHERE project_id = ? AND run_number = ?",
            (project_id, int(run.id)),
        ).fetchone()
        storage_id = int(row["id"]) if row is not None else None

    if storage_id is None:
        cursor = conn.execute(
            """
            INSERT INTO runs(
                project_id, run_number, name, status, created_at, started_at,
                finished_at, settings_json, graph_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _run_row_values(project_id, run, graph),
        )
        return int(cursor.lastrowid)

    conn.execute(
        """
        UPDATE runs SET
            project_id = ?,
            run_number = ?,
            name = ?,
            status = ?,
            created_at = ?,
            started_at = ?,
            finished_at = ?,
            settings_json = ?,
            graph_json = ?
        WHERE id = ?
        """,
        (*_run_row_values(project_id, run, graph), int(storage_id)),
    )
    return int(storage_id)


def _run_row_values(project_id: int, run: RunRecord, graph: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(project_id),
        int(run.id),
        str(run.name),
        str(run.status),
        str(run.created_at),
        run.started_at,
        run.finished_at,
        _json_dumps(run.settings),
        _json_dumps(graph),
    )


def _save_all_run_points(conn: sqlite3.Connection, document: WorkspaceDocument) -> None:
    conn.execute("DELETE FROM run_points")
    for project in document.projects:
        for run in project.runs:
            if run.storage_id is None:
                run.storage_id = _upsert_run(conn, int(project.id), run)
            for index, payload in enumerate(run.data):
                _insert_run_point(conn, int(run.storage_id), index, payload)


def _insert_run_point(
    conn: sqlite3.Connection,
    run_storage_id: int,
    point_index: int,
    payload: dict[str, Any],
) -> None:
    smu1 = payload.get("smu1", {}) if isinstance(payload, dict) else {}
    smu2 = payload.get("smu2", {}) if isinstance(payload, dict) else {}
    smu1_values = smu1.get("values", {}) if isinstance(smu1, dict) else {}
    smu2_values = smu2.get("values", {}) if isinstance(smu2, dict) else {}
    conn.execute(
        """
        INSERT INTO run_points(
            run_id, point_index, time_s, series_name, primary_name, stepper_name,
            smu1_source_v, smu1_voltage, smu1_current, smu1_resistance,
            smu2_source_v, smu2_voltage, smu2_current, smu2_resistance,
            payload_json
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(run_storage_id),
            int(point_index),
            _float_or_none(payload.get("time_s")),
            _str_or_none(payload.get("series_name")),
            _str_or_none(payload.get("primary_name")),
            _str_or_none(payload.get("stepper_name")),
            _float_or_none(smu1.get("source_v") if isinstance(smu1, dict) else None),
            _float_or_none(smu1_values.get("Voltage") if isinstance(smu1_values, dict) else None),
            _float_or_none(smu1_values.get("Current") if isinstance(smu1_values, dict) else None),
            _float_or_none(smu1_values.get("Resistance") if isinstance(smu1_values, dict) else None),
            _float_or_none(smu2.get("source_v") if isinstance(smu2, dict) else None),
            _float_or_none(smu2_values.get("Voltage") if isinstance(smu2_values, dict) else None),
            _float_or_none(smu2_values.get("Current") if isinstance(smu2_values, dict) else None),
            _float_or_none(smu2_values.get("Resistance") if isinstance(smu2_values, dict) else None),
            _json_dumps(payload),
        ),
    )


def _delete_missing(conn: sqlite3.Connection, table: str, keep_ids: list[int]) -> None:
    if keep_ids:
        placeholders = ",".join("?" for _ in keep_ids)
        conn.execute(f"DELETE FROM {table} WHERE id NOT IN ({placeholders})", keep_ids)
    else:
        conn.execute(f"DELETE FROM {table}")


def _nullable_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
