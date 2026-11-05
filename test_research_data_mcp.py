import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import research_data_mcp as m


class SharedDataTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(Path(__file__).with_name("catalog_schema.sql").read_text(encoding="utf-8"))
        def execute(sql, args=()):
            cursor = self.db.execute(sql, args)
            return [dict(row) for row in cursor.fetchall()] if cursor.description else []
        self.patcher = patch.object(m, "execute", side_effect=execute)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.db.close()

    def test_catalog_is_not_reported_as_transferred(self):
        files = m.call_tool("list_files", {"dataset_id": "bcer-wa36588-pilot"})
        self.assertEqual(len(files), 3)
        self.assertTrue(all(row["transfer_status"] == "catalogued" and row["size_bytes"] is None for row in files))

    def test_same_key_in_two_projects_does_not_collide(self):
        for project in ("project-a", "project-b"):
            m.call_tool("write_result", {"project_id": project, "result_key": "same", "value": {"owner": project}})
        for project in ("project-a", "project-b"):
            self.assertEqual(m.call_tool("read_result", {"project_id": project, "result_key": "same"})["value"]["owner"], project)

    def test_update_only_changes_the_named_project(self):
        for project in ("a", "b"):
            m.call_tool("write_result", {"project_id": project, "result_key": "same", "value": 1})
        m.call_tool("write_result", {"project_id": "a", "result_key": "same", "value": 2})
        self.assertEqual(m.call_tool("read_result", {"project_id": "a", "result_key": "same"})["value"], 2)
        self.assertEqual(m.call_tool("read_result", {"project_id": "b", "result_key": "same"})["value"], 1)

    def test_oversize_value_is_rejected(self):
        with self.assertRaises(ValueError):
            m.call_tool("write_result", {"project_id": "a", "result_key": "large", "value": "x" * 65537})

    def test_source_identifier_is_parameterized(self):
        self.assertEqual(m.call_tool("list_files", {"dataset_id": "a'; DROP TABLE files; --"}), [])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0], 3)

    def test_missing_result(self):
        self.assertEqual(m.call_tool("read_result", {"project_id": "a", "result_key": "missing"}), {"found": False})

    def test_mcp_initialization_and_discovery_without_credentials(self):
        result = m.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(result["result"]["serverInfo"]["name"], "research_data")
        self.assertEqual(len(m.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]), 5)
        self.assertIsNone(m.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))


if __name__ == "__main__":
    unittest.main()
