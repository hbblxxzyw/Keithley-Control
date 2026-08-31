from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from models.project import WorkspaceDocument


def sample_payload(index: int = 0) -> dict:
    return {
        "time_s": float(index),
        "series_name": "Measurement",
        "primary_name": "SMU 1",
        "stepper_name": "SMU 2",
        "smu1": {
            "source_v": 0.1 * index,
            "values": {"Voltage": 0.1 * index, "Current": 1e-6 * index},
        },
        "smu2": {
            "source_v": 0.2 * index,
            "values": {"Voltage": 0.2 * index, "Current": 2e-6 * index},
        },
    }


class WorkspaceStoreTests(unittest.TestCase):
    def test_sqlite_workspace_persists_metadata_runs_and_points(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.keithley-workspace.sqlite"
            workspace = WorkspaceDocument.new({"resource_address": "dummy"})
            workspace.save(path)

            project_1 = workspace.active_project()
            run_1 = project_1.add_run({"run": 1})
            workspace.save()
            run_1.data.append(sample_payload(1))
            workspace.append_run_point(project_1.id, run_1.id, sample_payload(1))

            project_2 = workspace.add_project("Project 2", {"project": 2})
            run_2 = project_2.add_run({"run": 1, "project": 2})
            workspace.save()
            run_2.data.append(sample_payload(2))
            workspace.append_run_point(project_2.id, run_2.id, sample_payload(2))

            project_1.delete_run(run_1.id)
            next_run = project_1.add_run({"run": 2})
            self.assertEqual(next_run.id, 2)
            workspace.add_quick_config("Shared", {"quick": True})
            workspace.save()

            reopened = WorkspaceDocument.load(path)
            self.assertEqual(len(reopened.projects), 2)
            self.assertEqual(len(reopened.quick_configs), 1)
            self.assertEqual(reopened.quick_configs[0].name, "Shared")
            self.assertTrue(reopened.quick_configs[0].quick_access)
            self.assertEqual(reopened.projects[0].runs[0].id, 2)
            self.assertEqual(reopened.projects[1].runs[0].id, 1)

            loaded_project_2 = reopened.get_project(project_2.id)
            self.assertIsNotNone(loaded_project_2)
            loaded_run_2 = loaded_project_2.get_run(run_2.id) if loaded_project_2 else None
            self.assertIsNotNone(loaded_run_2)
            points = reopened.load_run_points(loaded_run_2) if loaded_run_2 else []
            self.assertEqual(len(points), 1)
            self.assertEqual(points[0]["smu2"]["values"]["Current"], 4e-6)

    def test_migrates_legacy_workspace_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "legacy.keithley-workspace.json"
            workspace = WorkspaceDocument.new({"base": True})
            project = workspace.active_project()
            run = project.add_run({"settings": 1})
            run.data.append(sample_payload(3))
            workspace.add_quick_config("Legacy Config", {"cfg": 1})
            json_path.write_text(json.dumps(workspace.to_dict()), encoding="utf-8")

            migrated = WorkspaceDocument.load(json_path)

            self.assertEqual(migrated.path, Path(tmp) / "legacy.keithley-workspace.sqlite")
            self.assertTrue(migrated.path.exists())
            self.assertEqual(len(migrated.quick_configs), 1)
            migrated_run = migrated.active_project().active_run()
            self.assertIsNotNone(migrated_run)
            points = migrated.load_run_points(migrated_run) if migrated_run else []
            self.assertEqual(len(points), 1)
            self.assertEqual(points[0]["time_s"], 3.0)

    def test_migrates_legacy_project_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "legacy_project.json"
            payload = {
                "format": "keithley-control-project",
                "version": 1,
                "project": {"name": "Old Project", "next_run_id": 2},
                "active_run_id": 1,
                "quick_configs": [{"id": 1, "name": "Old Quick", "settings": {"a": 1}}],
                "runs": [
                    {
                        "id": 1,
                        "name": "",
                        "settings": {"run": True},
                        "graph": {},
                        "data": [sample_payload(4)],
                    }
                ],
            }
            json_path.write_text(json.dumps(payload), encoding="utf-8")

            migrated = WorkspaceDocument.load(json_path)

            self.assertEqual(len(migrated.projects), 1)
            self.assertEqual(migrated.active_project().name, "Old Project")
            self.assertEqual(len(migrated.quick_configs), 1)
            self.assertTrue(migrated.quick_configs[0].quick_access)
            run = migrated.active_project().active_run()
            points = migrated.load_run_points(run) if run else []
            self.assertEqual(points[0]["time_s"], 4.0)

    def test_quick_config_quick_access_limit_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quick-access.keithley-workspace.sqlite"
            workspace = WorkspaceDocument.new({})
            configs = [
                workspace.add_quick_config(f"Config {index}", {"index": index})
                for index in range(1, 5)
            ]

            self.assertEqual(
                [config.quick_access for config in configs],
                [True, True, True, False],
            )
            self.assertFalse(
                workspace.set_quick_config_quick_access(configs[3].id, True)
            )
            self.assertTrue(
                workspace.set_quick_config_quick_access(configs[0].id, False)
            )
            self.assertTrue(
                workspace.set_quick_config_quick_access(configs[3].id, True)
            )
            workspace.save(path)

            reopened = WorkspaceDocument.load(path)
            self.assertEqual(
                [config.quick_access for config in reopened.quick_configs],
                [False, True, True, True],
            )

    def test_imports_legacy_gui_config_as_workspace_quick_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "keithley_config.json"
            payload = {
                "format": "keithley-gui-config",
                "version": 1,
                "settings": {"resource_address": "USB0::1"},
            }
            json_path.write_text(json.dumps(payload), encoding="utf-8")

            migrated = WorkspaceDocument.load(json_path)

            self.assertEqual(len(migrated.projects), 1)
            self.assertEqual(len(migrated.quick_configs), 1)
            self.assertEqual(
                migrated.quick_configs[0].settings["resource_address"],
                "USB0::1",
            )


if __name__ == "__main__":
    unittest.main()
