from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.domain.factories import weighted_stored_unit_rate
from app.services.production_actual_service import overlay_actual_production


class StoredUnitRateTests(unittest.TestCase):
    def test_weighted_rate_uses_stored_excel_values_not_usage(self) -> None:
        frame = pd.DataFrame({
            "mix_prod_kg": [1_000.0, 3_000.0, 100_000.0],
            "power_per_ton_kwh": [100.0, 300.0, 0.0],
            "total_power_kwh": [999_999.0, 1.0, 999_999.0],
        })
        self.assertEqual(weighted_stored_unit_rate(frame, "power_per_ton_kwh"), 250.0)

    def test_production_overlay_does_not_overwrite_stored_unit_rate(self) -> None:
        energy = pd.DataFrame({
            "date": [date(2026, 7, 30)],
            "factory": ["김해"],
            "mix_prod_kg": [1_000.0],
            "total_power_kwh": [500.0],
            "power_per_ton_kwh": [123.45],
        })
        actual = pd.DataFrame({
            "date": [date(2026, 7, 30)],
            "factory": ["김해"],
            "actual_prod_kg": [2_000.0],
        })
        result = overlay_actual_production(energy, actual=actual)
        self.assertEqual(result.loc[0, "mix_prod_kg"], 2_000.0)
        self.assertEqual(result.loc[0, "power_per_ton_kwh"], 123.45)


if __name__ == "__main__":
    unittest.main()
