from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import production_dw_sync_service as sync


DAY = date(2026, 10, 1)


def record(factory: str, item: str = "122842", quantity: float = 18675.0) -> tuple:
    return (DAY, item, "test product", factory, "냉장", "MY", 20000.0, quantity)


def snapshot(*records: tuple) -> dict:
    return {
        (r[0], r[3], r[1]): sync._comparable(r[2], r[4], r[5], r[6], r[7])
        for r in records
    }


def db_row() -> dict:
    return dict(
        id=123, date=DAY, item_code="122842", item_name="test product",
        factory="F10B", category1="냉장", category2="MY", planned_qty=20000.0,
        actual_qty=18675.0, created_at=datetime(2026, 10, 23, 13, 45, 40),
        updated_at=datetime(2026, 10, 23, 13, 45, 40),
    )


class ProductionSyncTests(unittest.TestCase):
    def test_reclassified_factory_row_is_selected_but_history_is_preserved(self):
        source = [record("F10A"), record("F10B", "other")]
        existing = snapshot(*source, record("F10B"), record("F40"), record("F10B", "removed"))
        existing[(date(2025, 1, 1), "F10B", "122842")] = existing[(DAY, "F10B", "122842")]
        self.assertEqual(sync._stale_factory_keys(source, existing), [(DAY, "F10B", "122842")])

    def test_item_legitimately_present_in_both_factories_is_preserved(self):
        source = [record("F10A"), record("F10B")]
        self.assertEqual(sync._stale_factory_keys(source, snapshot(*source)), [])

    def test_reclassification_with_changed_quantity_still_removes_old_factory_key(self):
        source = [record("F10A", quantity=19000), record("F10B", "other")]
        self.assertEqual(
            sync._stale_factory_keys(source, snapshot(record("F10B"))),
            [(DAY, "F10B", "122842")],
        )

    def test_invalid_source_is_rejected_before_db_access(self):
        columns = ["date", "item_code", "item_name", "factory", "category1", "category2", "planned_qty", "actual_qty"]
        valid = dict(zip(columns, record("F10A")))
        for frame in [pd.DataFrame(columns=columns), pd.DataFrame([valid, valid]), pd.DataFrame([{**valid, "date": None}])]:
            with self.subTest(frame=frame.shape), patch.object(sync.pd, "read_excel", return_value=frame):
                with self.assertRaises(ValueError):
                    sync._load_daily_sheet(Path("source.xlsx"))

    def test_source_rejects_namyangju_chilled_my_misclassified_as_factory_two(self):
        columns = ["date", "item_code", "item_name", "factory", "category1", "category2", "planned_qty", "actual_qty"]
        frame = pd.DataFrame([dict(zip(columns, record("F10B")))])
        with patch.object(sync.pd, "read_excel", return_value=frame):
            with self.assertRaisesRegex(ValueError, "남양주 공장 분류 오류"):
                sync._load_daily_sheet(Path("source.xlsx"))

    def test_source_accepts_correct_namyangju_split(self):
        columns = ["date", "item_code", "item_name", "factory", "category1", "category2", "planned_qty", "actual_qty"]
        first = dict(zip(columns, record("F10A")))
        second = {**first, "factory": "F10B", "item_code": "other", "category2": "FM"}
        with patch.object(sync.pd, "read_excel", return_value=pd.DataFrame([first, second])):
            self.assertEqual(len(sync._load_daily_sheet(Path("source.xlsx"))), 2)

    def test_deleted_rows_are_backed_up_with_ids_and_timestamps_before_commit(self):
        conn = MagicMock()
        cur = conn.cursor.return_value
        cur.fetchall.return_value = [db_row()]
        cur.rowcount = 1
        with tempfile.TemporaryDirectory() as folder, patch.object(sync, "BACKUP_DIR", Path(folder)), patch.object(sync, "get_connection", return_value=conn):
            result = sync._apply_sync_changes(
                [record("F10A")], [(DAY, "F10B", "122842")], snapshot(record("F10B")), Path("source.xlsx"),
            )
            self.assertEqual(result[:3], (1, 1, 1))
            backup = json.loads(Path(result[3]).read_text(encoding="utf-8"))
            self.assertEqual(backup["rows"][0]["id"], 123)
            self.assertEqual(backup["rows"][0]["created_at"], "2026-10-23 13:45:40")
            self.assertIn("FOR UPDATE", cur.execute.call_args.args[0])
            self.assertTrue(cur.executemany.call_args_list[0].args[0].startswith("DELETE"))
            conn.commit.assert_called_once()
            conn.rollback.assert_not_called()

    def test_backup_failure_aborts_without_deleting(self):
        conn = MagicMock()
        cur = conn.cursor.return_value
        cur.fetchall.return_value = [db_row()]
        with patch.object(sync, "get_connection", return_value=conn), patch.object(sync, "_backup_stale_rows", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sync._apply_sync_changes([], [(DAY, "F10B", "122842")], snapshot(record("F10B")), Path("source.xlsx"))
        cur.executemany.assert_not_called()
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()

    def test_upsert_failure_rolls_back_the_deletion_too(self):
        conn = MagicMock()
        cur = conn.cursor.return_value
        cur.fetchall.return_value = [db_row()]
        cur.rowcount = 1
        cur.executemany.side_effect = [None, RuntimeError("write failed")]
        with patch.object(sync, "get_connection", return_value=conn), patch.object(sync, "_backup_stale_rows", return_value=Path("backup.json")):
            with self.assertRaises(RuntimeError):
                sync._apply_sync_changes([record("F10A")], [(DAY, "F10B", "122842")], snapshot(record("F10B")), Path("source.xlsx"))
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()

    def test_concurrent_db_change_aborts_before_backup_or_delete(self):
        conn = MagicMock()
        cur = conn.cursor.return_value
        cur.fetchall.return_value = [{**db_row(), "actual_qty": 999.0}]
        with patch.object(sync, "get_connection", return_value=conn), patch.object(sync, "_backup_stale_rows") as backup:
            with self.assertRaises(RuntimeError):
                sync._apply_sync_changes([], [(DAY, "F10B", "122842")], snapshot(record("F10B")), Path("source.xlsx"))
        backup.assert_not_called()
        cur.executemany.assert_not_called()
        conn.rollback.assert_called_once()

    def test_no_changes_does_not_open_a_write_connection(self):
        with patch.object(sync, "get_connection") as connection:
            self.assertEqual(sync._apply_sync_changes([], [], {}, Path("source.xlsx")), (0, 0, 0, None))
        connection.assert_not_called()

    def test_policy_upgrade_rechecks_an_unchanged_source_once(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.xlsx"
            source.touch()
            state = {"last_mtime": source.stat().st_mtime}
            with patch.object(sync, "_read_state", return_value=state), patch.object(sync, "_load_daily_sheet", return_value=pd.DataFrame()) as loader, patch.object(sync, "_df_to_records", return_value=[record("F10A")]), patch.object(sync, "_existing_snapshot", return_value={}), patch.object(sync, "_apply_sync_changes", return_value=(1, 1, 0, None)), patch.object(sync, "_write_state") as save:
                self.assertEqual(sync.auto_sync_production_once(source)["status"], "synced")
                loader.assert_called_once()
                self.assertEqual(save.call_args.args[0]["sync_policy_version"], sync.SYNC_POLICY_VERSION)
            state["sync_policy_version"] = sync.SYNC_POLICY_VERSION
            with patch.object(sync, "_read_state", return_value=state), patch.object(sync, "_load_daily_sheet") as loader:
                self.assertEqual(sync.auto_sync_production_once(source)["status"], "unchanged")
                loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
