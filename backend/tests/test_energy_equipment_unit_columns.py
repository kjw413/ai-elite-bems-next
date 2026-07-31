from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import db_connection
from app.services import daily_energy_sync_service, upload_service, v5_common
from app.utils import excel_parser


class EquipmentUnitColumnContractTests(unittest.TestCase):
    COLUMNS = (
        "freezing_power_per_ton_kwh",
        "air_compressor_per_ton_kwh",
    )

    def test_columns_are_registered_in_every_ingestion_contract(self) -> None:
        for label, columns in (
            ("EXPECTED_COLUMNS", excel_parser.EXPECTED_COLUMNS),
            ("sync _INSERT_COLUMNS", daily_energy_sync_service._INSERT_COLUMNS),
            ("upload INSERT_COLUMNS", upload_service.INSERT_COLUMNS),
            (
                "ENERGY_UPLOAD_TO_MODEL_COLUMNS",
                list(v5_common.ENERGY_UPLOAD_TO_MODEL_COLUMNS),
            ),
        ):
            with self.subTest(list=label):
                actual = [column for column in columns if column in self.COLUMNS]
                self.assertEqual(actual, list(self.COLUMNS))

        self.assertEqual(
            excel_parser.KOR_SUBSTR_MAP["냉동원단위"],
            "freezing_power_per_ton_kwh",
        )
        self.assertEqual(
            excel_parser.KOR_SUBSTR_MAP["공압기원단위"],
            "air_compressor_per_ton_kwh",
        )
        self.assertEqual(
            v5_common.ENERGY_UPLOAD_TO_MODEL_COLUMNS["freezing_power_per_ton_kwh"],
            "냉동원단위[kWh/mix-ton]",
        )
        self.assertEqual(
            v5_common.ENERGY_UPLOAD_TO_MODEL_COLUMNS["air_compressor_per_ton_kwh"],
            "공압기원단위[kWh/mix-ton]",
        )

    def test_insert_column_order_matches_parser_numeric_columns(self) -> None:
        self.assertEqual(excel_parser.NUMERIC_COLUMNS, excel_parser.EXPECTED_COLUMNS[1:])
        self.assertEqual(
            daily_energy_sync_service._INSERT_COLUMNS[2:],
            excel_parser.NUMERIC_COLUMNS,
        )
        self.assertEqual(
            upload_service.INSERT_COLUMNS[2:],
            excel_parser.NUMERIC_COLUMNS,
        )

    def test_migration_chain_and_schema_place_columns_after_mix_production(self) -> None:
        migrations = [
            (column, fragment)
            for table, column, fragment in db_connection._PENDING_COLUMN_MIGRATIONS
            if table == "energy_daily" and column in self.COLUMNS
        ]
        self.assertEqual(
            [column for column, _fragment in migrations],
            list(self.COLUMNS),
        )
        anchors = ("mix_prod_kg", "freezing_power_per_ton_kwh")
        for (column, fragment), anchor in zip(migrations, anchors):
            with self.subTest(column=column):
                self.assertIn(f"AFTER {anchor}", fragment)

        schema = (BACKEND_ROOT / "app" / "database" / "schema.sql").read_text(
            encoding="utf-8",
        )
        create = schema.split("CREATE TABLE IF NOT EXISTS energy_daily")[1].split(
            ") ENGINE",
        )[0]
        defined = [
            match.group(1)
            for match in (
                re.match(
                    r"([a-z_0-9]+)\s+(?:DOUBLE|INT|DATE|DATETIME|VARCHAR|TEXT)",
                    line.strip(),
                )
                for line in create.splitlines()
                if line.strip() and not line.strip().startswith("--")
            )
            if match
        ]
        start = defined.index("mix_prod_kg") + 1
        self.assertEqual(
            defined[start:start + 3],
            [*self.COLUMNS, "power_per_ton_kwh"],
        )


if __name__ == "__main__":
    unittest.main()
