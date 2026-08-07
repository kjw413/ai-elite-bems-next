from __future__ import annotations

import unittest
from io import BytesIO
from unittest.mock import patch

from openpyxl import Workbook

from app.services import savings_import_service


HEADERS = [
    "No.", "테마명", "에너지원", "절감유형", "연간(계획)", "연간(실적)",
    "1월(계획)", "1월(실적)", "2월(계획)", "2월(실적)",
    "3월(계획)", "3월(실적)", "4월(계획)", "4월(실적)",
    "5월(계획)", "5월(실적)", "6월(계획)", "6월(실적)",
    "7월(계획)", "7월(실적)", "8월(계획)", "8월(실적)",
    "9월(계획)", "9월(실적)", "10월(계획)", "10월(실적)",
    "11월(계획)", "11월(실적)", "12월(계획)", "12월(실적)",
]


def workbook_bytes(*rows: list[object], sheet_name: str = "남양주") -> BytesIO:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    sheet.append(HEADERS)
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


class SavingsImportServiceTests(unittest.TestCase):
    def test_parses_template_row_and_preserves_blank_actual(self) -> None:
        row: list[object] = [1, "고효율 모터 교체", "전력", "설비교체", None, None]
        row.extend([100, 90, 120, None])
        row.extend([None] * 20)

        parsed = savings_import_service.parse_template(workbook_bytes(row), 2026)

        self.assertEqual(len(parsed), 1)
        theme = parsed[0]
        self.assertEqual(theme["factory"], "남양주")
        self.assertEqual(theme["year"], 2026)
        self.assertEqual(theme["title"], "고효율 모터 교체")
        self.assertEqual(theme["energy_type"], "power")
        self.assertEqual(theme["category"], "설비교체")
        self.assertEqual(theme["status"], "ongoing")
        self.assertEqual(theme["records"][0], {"month": 1, "planned_qty": 100.0, "actual_qty": 90.0})
        self.assertEqual(theme["records"][1], {"month": 2, "planned_qty": 120.0, "actual_qty": None})
        self.assertEqual(theme["records"][11], {"month": 12, "planned_qty": 0.0, "actual_qty": None})

    def test_rejects_unknown_energy_label_with_cell_location(self) -> None:
        row: list[object] = [1, "스팀 트랩 교체", "스팀", "설비교체", None, None]
        row.extend([None] * 24)

        with self.assertRaises(savings_import_service.TemplateValidationError) as raised:
            savings_import_service.parse_template(workbook_bytes(row, sheet_name="김해"), 2026)

        self.assertIn("김해!C2", str(raised.exception))
        self.assertIn("전력/연료/용수", str(raised.exception))

    def test_rejects_duplicate_theme_in_same_factory(self) -> None:
        first: list[object] = [1, "인버터 적용", "전력", "운전개선", None, None] + [None] * 24
        second: list[object] = [2, " 인버터 적용 ", "전력", "운전개선", None, None] + [None] * 24

        with self.assertRaises(savings_import_service.TemplateValidationError) as raised:
            savings_import_service.parse_template(workbook_bytes(first, second), 2026)

        self.assertIn("남양주!B3", str(raised.exception))
        self.assertIn("중복", str(raised.exception))

    def test_rejects_non_numeric_month_value(self) -> None:
        row: list[object] = [1, "누설 개선", "연료", "누설저감", None, None, "확인중"] + [None] * 23

        with self.assertRaises(savings_import_service.TemplateValidationError) as raised:
            savings_import_service.parse_template(workbook_bytes(row, sheet_name="논산"), 2026)

        self.assertIn("논산!G2", str(raised.exception))
        self.assertIn("숫자", str(raised.exception))


    def test_preview_contains_every_theme_and_all_month_values(self) -> None:
        row: list[object] = [1, "고효율 모터 교체", "전력", "설비교체", None, None]
        row.extend([100, 90, 120, None])
        row.extend([None] * 20)
        themes = savings_import_service.parse_template(workbook_bytes(row), 2026)

        with patch.object(savings_import_service, "_existing_theme_ids", return_value={}):
            preview = savings_import_service.preview_import(themes)

        self.assertEqual(len(preview["items"]), 1)
        item = preview["items"][0]
        self.assertEqual(item["sourceRow"], 2)
        self.assertEqual(item["energyLabel"], "전력")
        self.assertEqual(item["plannedTotal"], 220.0)
        self.assertEqual(item["actualTotal"], 90.0)
        self.assertEqual(len(item["months"]), 12)
        self.assertEqual(
            item["months"][1],
            {"month": 2, "plannedQty": 120.0, "actualQty": None},
        )

    def test_rejects_corrupt_xlsx_as_template_error(self) -> None:
        with self.assertRaises(savings_import_service.TemplateValidationError) as raised:
            savings_import_service.parse_template(BytesIO(b"not-an-xlsx"), 2026)

        self.assertIn("손상", str(raised.exception))

    # ── 시행월 자동 도출 ─────────────────────────────────────────
    # 양식에 시행월 열이 없어 월별 값에서 도출한다. 이 값이 없으면 원단위 전후
    # 비교 검증이 영영 '판정 보류'로 남으므로(2026-08 실측: 25건 전부 보류)
    # 규칙을 테스트로 고정한다.

    def test_start_ym_is_first_month_with_plan_or_actual(self) -> None:
        records = [
            {"month": month, "planned_qty": 100.0 if month >= 3 else 0.0, "actual_qty": None}
            for month in range(1, 13)
        ]
        self.assertEqual(savings_import_service.derive_start_ym(records, 2026), "2026-03")

    def test_start_ym_counts_actual_only_month(self) -> None:
        """계획 없이 실적만 있는 달도 시행으로 본다 — 계획 없이 시행된 건이 실제로 있다."""
        records = [
            {"month": month, "planned_qty": 0.0, "actual_qty": 5.0 if month >= 5 else None}
            for month in range(1, 13)
        ]
        self.assertEqual(savings_import_service.derive_start_ym(records, 2026), "2026-05")

    def test_start_ym_treats_zero_actual_as_entered(self) -> None:
        """실적 0(측정했더니 0)과 미입력(None)은 다르다 — 0 은 시행된 달이다."""
        records = [
            {"month": month, "planned_qty": 0.0, "actual_qty": 0.0 if month == 7 else None}
            for month in range(1, 13)
        ]
        self.assertEqual(savings_import_service.derive_start_ym(records, 2026), "2026-07")

    def test_start_ym_is_none_when_nothing_entered(self) -> None:
        records = [
            {"month": month, "planned_qty": 0.0, "actual_qty": None} for month in range(1, 13)
        ]
        self.assertIsNone(savings_import_service.derive_start_ym(records, 2026))

    def test_parsed_theme_carries_derived_start_ym(self) -> None:
        row: list[object] = [1, "고효율 모터 교체", "전력", "설비교체", None, None]
        row.extend([None, None])          # 1월 — 비어 있음
        row.extend([120, None])           # 2월 — 계획 최초 기입
        row.extend([None] * 20)
        themes = savings_import_service.parse_template(workbook_bytes(row), 2026)
        self.assertEqual(themes[0]["start_ym"], "2026-02")

if __name__ == "__main__":
    unittest.main()
