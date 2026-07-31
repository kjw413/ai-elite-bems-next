from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.daily_energy_sync_service import _coerce_date


class DailyEnergySyncDateTests(unittest.TestCase):
    def test_nat_from_trailing_excel_rows_is_kept_as_missing(self) -> None:
        result = _coerce_date(pd.NaT)

        self.assertTrue(pd.isna(result))


if __name__ == "__main__":
    unittest.main()
