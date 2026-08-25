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
from app.services.production_actual_service import (
    correct_gwangju_energy_frame,
    correct_gwangju_energy_rows,
    overlay_actual_production,
)


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

    def test_gwangju_correction_sums_duplicate_actual_and_recalculates_all_rates(self) -> None:
        energy = pd.DataFrame({
            "date": [date(2026, 8, 24)],
            "factory": ["광주"],
            "mix_prod_kg": [1_000.0],
            "freezing_power_kwh": [100.0],
            "air_compressor_kwh": [50.0],
            "total_power_kwh": [400.0],
            "fuel_nm3": [20.0],
            "water_ton": [10.0],
            "freezing_power_per_ton_kwh": [999.0],
            "air_compressor_per_ton_kwh": [999.0],
            "power_per_ton_kwh": [999.0],
            "fuel_per_ton_nm3": [999.0],
            "water_per_ton_ton": [999.0],
        })
        actual = pd.DataFrame({
            "date": [date(2026, 8, 24), date(2026, 8, 24)],
            "factory": ["광주", "광주"],
            "actual_prod_kg": [1_500.0, 500.0],
        })

        result = correct_gwangju_energy_frame(energy, actual=actual)

        self.assertEqual(result.loc[0, "mix_prod_kg"], 2_000.0)
        self.assertEqual(result.loc[0, "freezing_power_per_ton_kwh"], 50.0)
        self.assertEqual(result.loc[0, "air_compressor_per_ton_kwh"], 25.0)
        self.assertEqual(result.loc[0, "power_per_ton_kwh"], 200.0)
        self.assertEqual(result.loc[0, "fuel_per_ton_nm3"], 10.0)
        self.assertEqual(result.loc[0, "water_per_ton_ton"], 5.0)

    def test_gwangju_rows_api_accepts_dict_actual_rows(self) -> None:
        rows = [{
            "date": date(2026, 8, 24),
            "factory": "광주",
            "mix_prod_kg": 1_000.0,
            "total_power_kwh": 100.0,
            "power_per_ton_kwh": 999.0,
        }]
        actual = [
            {"date": date(2026, 8, 24), "factory": "광주", "actual_prod_kg": 1_000.0},
            {"date": date(2026, 8, 24), "factory": "광주", "actual_prod_kg": 1_000.0},
        ]

        result = correct_gwangju_energy_rows(rows, actual=actual)

        self.assertEqual(result[0]["mix_prod_kg"], 2_000.0)
        self.assertEqual(result[0]["power_per_ton_kwh"], 50.0)

    def test_gwangju_correction_leaves_non_gwangju_row_unchanged(self) -> None:
        energy = pd.DataFrame({
            "date": [date(2026, 8, 24)],
            "factory": ["김해"],
            "mix_prod_kg": [1_000.0],
            "total_power_kwh": [100.0],
            "power_per_ton_kwh": [123.45],
        })
        actual = pd.DataFrame({
            "date": [date(2026, 8, 24)],
            "factory": ["김해"],
            "actual_prod_kg": [2_000.0],
        })

        result = correct_gwangju_energy_frame(energy, actual=actual)

        pd.testing.assert_frame_equal(result, energy)

    def test_gwangju_zero_production_sets_all_rates_to_zero(self) -> None:
        energy = pd.DataFrame({
            "date": [date(2026, 8, 24)],
            "factory": ["광주"],
            "mix_prod_kg": [1_000.0],
            "freezing_power_kwh": [100.0],
            "air_compressor_kwh": [50.0],
            "total_power_kwh": [400.0],
            "fuel_nm3": [20.0],
            "water_ton": [10.0],
            "freezing_power_per_ton_kwh": [999.0],
            "air_compressor_per_ton_kwh": [999.0],
            "power_per_ton_kwh": [999.0],
            "fuel_per_ton_nm3": [999.0],
            "water_per_ton_ton": [999.0],
        })
        actual = pd.DataFrame({
            "date": [date(2026, 8, 24)],
            "factory": ["광주"],
            "actual_prod_kg": [0.0],
        })

        result = correct_gwangju_energy_frame(energy, actual=actual)

        self.assertEqual(result.loc[0, "mix_prod_kg"], 0.0)
        for unit_col in (
            "freezing_power_per_ton_kwh",
            "air_compressor_per_ton_kwh",
            "power_per_ton_kwh",
            "fuel_per_ton_nm3",
            "water_per_ton_ton",
        ):
            self.assertEqual(result.loc[0, unit_col], 0.0)


if __name__ == "__main__":
    unittest.main()
