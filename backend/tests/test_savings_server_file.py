from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import server


class SavingsServerFileTests(unittest.TestCase):
    def test_missing_fixed_file_reports_expected_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "절감테마_등록.xlsx"
            with patch.object(server, "SAVINGS_IMPORT_PATH", path):
                status = server._savings_import_file_status()
                self.assertFalse(status["exists"])
                self.assertEqual(status["path"], str(path))
                with self.assertRaises(server.HTTPException) as raised:
                    server._read_savings_server_file()
        self.assertEqual(raised.exception.status_code, 404)

    def test_fixed_file_read_returns_metadata_and_hash(self) -> None:
        content = b"xlsx-placeholder"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "절감테마_등록.xlsx"
            path.write_bytes(content)
            with patch.object(server, "SAVINGS_IMPORT_PATH", path):
                actual, status = server._read_savings_server_file()
        self.assertEqual(actual, content)
        self.assertTrue(status["exists"])
        self.assertEqual(status["sizeBytes"], len(content))
        self.assertIsNotNone(status["modifiedAt"])
        self.assertEqual(status["sha256"], hashlib.sha256(content).hexdigest())

    def test_preview_endpoint_has_no_browser_file_body(self) -> None:
        operation = server.app.openapi()["paths"]["/api/v1/savings/themes/import/preview"]["post"]
        self.assertNotIn("requestBody", operation)

    def test_apply_rejects_file_changed_after_preview(self) -> None:
        source_file = {"sha256": "a" * 64}
        with (
            patch.object(server, "require_admin"),
            patch.object(server, "_read_savings_server_file", return_value=(b"content", source_file)),
            patch.object(server, "_parse_savings_template") as parse,
            self.assertRaises(server.HTTPException) as raised,
        ):
            server.import_savings_template(object(), 2026, "b" * 64)  # type: ignore[arg-type]
        self.assertEqual(raised.exception.status_code, 409)
        parse.assert_not_called()


if __name__ == "__main__":
    unittest.main()