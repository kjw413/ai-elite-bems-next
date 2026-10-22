from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import v5_post_correction as pc

# 2026-10-09(금) 한글날 → 10-09~10-11 연휴, 10-12(월)은 연휴 직후 첫 근무일.
HOLIDAYS = {date(2026, 10, 9)}
SATURDAY = date(2026, 10, 17)
POST_LONG_WEEKEND = date(2026, 10, 12)
WEDNESDAY = date(2026, 10, 14)


def _workdays_before(end: date, n: int) -> list[pd.Timestamp]:
    days = pd.bdate_range(end=pd.Timestamp(end) - pd.Timedelta(days=1), periods=n + 5)
    days = [d for d in days if d.date() not in HOLIDAYS]
    return days[-n:]


def _history(n: int, pred: float, actual: float, prod_kg: float = 50_000.0, end: date = WEDNESDAY) -> pd.DataFrame:
    return pd.DataFrame({
        "pred_date": _workdays_before(end, n),
        "pred_ref": [pred] * n,
        "actual": [actual] * n,
        "mix_prod_kg": [prod_kg] * n,
    })


class NonworkMultiplierTests(unittest.TestCase):
    def test_production_threshold_selects_key(self) -> None:
        factor_on, key_on = pc.resolve_nonwork_multiplier("김해", "연료", 1_000.0)
        factor_off, key_off = pc.resolve_nonwork_multiplier("김해", "연료", 999.0)
        self.assertEqual(key_on, "production")
        self.assertEqual(key_off, "no_production")
        self.assertEqual(factor_on, pc.NONWORK_MULTIPLIERS["김해"]["연료"]["production"])
        self.assertEqual(factor_off, pc.NONWORK_MULTIPLIERS["김해"]["연료"]["no_production"])

    def test_missing_or_invalid_production_counts_as_no_production(self) -> None:
        for value in (None, float("nan"), "abc"):
            _, key = pc.resolve_nonwork_multiplier("광주", "전력", value)
            self.assertEqual(key, "no_production")

    def test_unknown_factory_uses_target_default(self) -> None:
        factor, _ = pc.resolve_nonwork_multiplier("경산", "용수", 5_000.0)
        self.assertEqual(factor, pc.NONWORK_DEFAULT_MULTIPLIERS["용수"]["production"])

    def test_unknown_target_has_no_multiplier(self) -> None:
        factor, _ = pc.resolve_nonwork_multiplier("광주", "폐수", 5_000.0)
        self.assertIsNone(factor)

    def test_power_is_corrected_on_nonwork_day(self) -> None:
        pred, info = pc.apply_nonwork_correction(1000.0, "광주", "전력", SATURDAY, HOLIDAYS, 0.0)
        expected = pc.NONWORK_MULTIPLIERS["광주"]["전력"]["no_production"]
        self.assertAlmostEqual(pred, 1000.0 * expected)
        self.assertEqual(info["rule"], "nonwork")
        self.assertEqual(info["production_key"], "no_production")

    def test_weekday_public_holiday_is_corrected(self) -> None:
        pred, info = pc.apply_nonwork_correction(1000.0, "논산", "용수", date(2026, 10, 9), HOLIDAYS, 50_000.0)
        self.assertIsNotNone(info)
        self.assertAlmostEqual(pred, 1000.0 * pc.NONWORK_MULTIPLIERS["논산"]["용수"]["production"])

    def test_workdays_including_post_long_weekend_are_unchanged(self) -> None:
        for day in (WEDNESDAY, POST_LONG_WEEKEND):
            for target in ("전력", "연료", "용수"):
                pred, info = pc.apply_nonwork_correction(1000.0, "남양주2", target, day, HOLIDAYS, 0.0)
                self.assertEqual(pred, 1000.0)
                self.assertIsNone(info)

    def test_custom_table_overrides_constants(self) -> None:
        table = {"광주": {"연료": {"production": 0.5, "no_production": 0.25}}}
        pred, _ = pc.apply_nonwork_correction(400.0, "광주", "연료", SATURDAY, HOLIDAYS, 0.0, table)
        self.assertAlmostEqual(pred, 100.0)

    def test_fit_uses_median_ratio_and_skips_nonpositive_rows(self) -> None:
        rows = pd.DataFrame({
            "factory": ["광주"] * 5,
            "target": ["연료"] * 5,
            "raw_pred": [100.0, 100.0, 100.0, 100.0, 0.0],
            "actual": [40.0, 50.0, 60.0, 0.0, 30.0],
            "mix_prod_kg": [0.0, 0.0, 0.0, 0.0, 0.0],
        })
        fitted = pc.fit_nonwork_multipliers(rows)
        self.assertEqual(fitted, {"광주": {"연료": {"no_production": 0.5}}})

    def test_fit_requires_columns(self) -> None:
        with self.assertRaises(ValueError):
            pc.fit_nonwork_multipliers(pd.DataFrame({"factory": ["광주"]}))


class BiasFactorTests(unittest.TestCase):
    def test_persistent_overprediction_scales_down(self) -> None:
        factor, info = pc.compute_bias_factor(_history(20, pred=110.0, actual=100.0), WEDNESDAY)
        self.assertAlmostEqual(factor, 100.0 / 110.0)
        self.assertEqual(info["n_points"], 20)

    def test_too_few_points_returns_identity(self) -> None:
        factor, info = pc.compute_bias_factor(_history(9, pred=110.0, actual=100.0), WEDNESDAY)
        self.assertEqual(factor, 1.0)
        self.assertIsNone(info)

    def test_empty_or_missing_history_returns_identity(self) -> None:
        self.assertEqual(pc.compute_bias_factor(None, WEDNESDAY), (1.0, None))
        self.assertEqual(pc.compute_bias_factor(pd.DataFrame(), WEDNESDAY), (1.0, None))

    def test_rows_on_or_after_prediction_date_are_ignored(self) -> None:
        hist = _history(20, pred=100.0, actual=100.0)
        future = pd.DataFrame({
            "pred_date": [pd.Timestamp(WEDNESDAY), pd.Timestamp(WEDNESDAY) + pd.Timedelta(days=1)],
            "pred_ref": [100.0, 100.0],
            "actual": [500.0, 500.0],
            "mix_prod_kg": [50_000.0, 50_000.0],
        })
        factor, _ = pc.compute_bias_factor(pd.concat([hist, future]), WEDNESDAY)
        self.assertEqual(factor, 1.0)

    def test_uses_only_last_window_points(self) -> None:
        old = _history(40, pred=100.0, actual=200.0)
        recent = _history(20, pred=100.0, actual=105.0)
        hist = pd.concat([old[~old["pred_date"].isin(recent["pred_date"])], recent])
        factor, info = pc.compute_bias_factor(hist, WEDNESDAY)
        self.assertAlmostEqual(factor, 1.05)
        self.assertEqual(info["n_points"], 20)

    def test_special_low_production_days_are_excluded(self) -> None:
        hist = _history(25, pred=100.0, actual=90.0)
        hist.loc[hist.index[-5:], ["actual", "mix_prod_kg"]] = [10.0, 500.0]
        factor, info = pc.compute_bias_factor(hist, WEDNESDAY)
        self.assertAlmostEqual(factor, 0.9)
        self.assertEqual(info["n_points"], 20)

    def test_missing_actuals_are_skipped(self) -> None:
        hist = _history(20, pred=100.0, actual=95.0)
        hist.loc[hist.index[-3:], "actual"] = float("nan")
        factor, info = pc.compute_bias_factor(hist, WEDNESDAY)
        self.assertAlmostEqual(factor, 0.95)
        self.assertEqual(info["n_points"], 17)

    def test_factor_is_capped(self) -> None:
        high, _ = pc.compute_bias_factor(_history(20, pred=100.0, actual=300.0), WEDNESDAY)
        low, _ = pc.compute_bias_factor(_history(20, pred=300.0, actual=100.0), WEDNESDAY)
        self.assertEqual(high, pc.DEFAULT_BIAS_CONFIG.factor_max)
        self.assertEqual(low, pc.DEFAULT_BIAS_CONFIG.factor_min)

    def test_min_abs_bias_gate(self) -> None:
        config = pc.BiasCorrectionConfig(min_abs_bias=0.05)
        factor, info = pc.compute_bias_factor(_history(20, pred=100.0, actual=103.0), WEDNESDAY, config)
        self.assertEqual((factor, info), (1.0, None))


class ServicePostCorrectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.services import usage_prediction_v5_service as service

        cls.service = service

    def _quantile_result(self, pred: float = 1000.0, actual: float | None = None) -> dict:
        return {
            "pred": pred,
            "pred_p05": pred * 0.9,
            "pred_p50": pred,
            "pred_p95": pred * 1.1,
            "actual": actual,
            "model_kind": "quantile",
        }

    def _apply(self, result: dict, pred_date: date, **overrides) -> dict:
        kwargs = dict(
            factory="광주",
            target_name="연료",
            pred_date=pred_date,
            holiday_set=HOLIDAYS,
            mix_prod_kg=50_000.0,
            wip_inputs={},
            drift_on=False,
            nonwork_on=True,
            bias_on=True,
            bias_history=None,
        )
        kwargs.update(overrides)
        return self.service._apply_post_corrections(result, **kwargs)

    def test_old_holiday_rule_is_removed(self) -> None:
        self.assertFalse(hasattr(self.service, "HOLIDAY_ADJ_CORRECTIONS"))
        self.assertFalse(hasattr(self.service, "_apply_holiday_adjacent_correction"))

    def test_post_long_weekend_workday_is_not_multiplied(self) -> None:
        result = self._apply(self._quantile_result(), POST_LONG_WEEKEND, target_name="용수")
        self.assertEqual(result["pred"], 1000.0)
        self.assertNotIn("nonwork_correction", result)

    def test_nonwork_day_scales_prediction_and_band(self) -> None:
        result = self._apply(self._quantile_result(actual=500.0), SATURDAY, target_name="전력", mix_prod_kg=0.0)
        factor = pc.NONWORK_MULTIPLIERS["광주"]["전력"]["no_production"]
        self.assertAlmostEqual(result["pred"], 1000.0 * factor)
        self.assertAlmostEqual(result["pred_p95"], 1100.0 * factor)
        self.assertEqual(result["pred_before_nonwork_adj"], 1000.0)
        self.assertAlmostEqual(result["pred_before_bias"], 1000.0 * factor)
        self.assertNotIn("bias_correction", result)

    def test_workday_bias_is_applied_and_band_reclassified(self) -> None:
        hist = _history(20, pred=100.0, actual=90.0)
        result = self._apply(self._quantile_result(actual=880.0), WEDNESDAY, bias_history=hist)
        self.assertAlmostEqual(result["pred"], 900.0)
        self.assertAlmostEqual(result["pred_p05"], 810.0)
        self.assertEqual(result["pred_before_bias"], 1000.0)
        self.assertEqual(result["bias_correction"]["n_points"], 20)
        self.assertEqual(result["band_status"], "inside")

    def test_bias_disabled_keeps_prediction(self) -> None:
        hist = _history(20, pred=100.0, actual=90.0)
        result = self._apply(self._quantile_result(), WEDNESDAY, bias_history=hist, bias_on=False)
        self.assertEqual(result["pred"], 1000.0)
        self.assertEqual(result["pred_before_bias"], 1000.0)

    def test_upsert_bias_history_replaces_same_date(self) -> None:
        history: dict[str, pd.DataFrame] = {}
        self.service._upsert_bias_history(history, "연료", WEDNESDAY, 100.0, None, 1.0)
        self.service._upsert_bias_history(history, "연료", WEDNESDAY, 120.0, 110.0, 2.0)
        self.service._upsert_bias_history(history, "연료", POST_LONG_WEEKEND, 90.0, 95.0, 3.0)
        frame = history["연료"]
        self.assertEqual(list(frame["pred_date"]), [pd.Timestamp(POST_LONG_WEEKEND), pd.Timestamp(WEDNESDAY)])
        self.assertEqual(frame.iloc[1]["pred_ref"], 120.0)
        self.assertEqual(frame.iloc[1]["actual"], 110.0)

    def _save_with_bias_column(self, exists: bool) -> tuple[str, tuple]:
        conn = MagicMock()
        cursor = conn.cursor.return_value
        row = {
            "factory": "광주", "pred_date": "2026-10-14", "target": "연료",
            "pred_value": 90.0, "pred_before_bias": 100.0, "pred_p05": 80.0, "pred_p95": 99.0,
            "actual_value": 95.0, "mix_prod_kg": 1.0, "model_path": "m.pkl",
        }
        with patch.object(self.service, "is_admin", return_value=True), \
                patch.object(self.service, "get_connection", return_value=conn), \
                patch.object(self.service, "_prediction_log_has_bias_column", return_value=exists):
            self.service._save_prediction_log([row])
        query, params_list = cursor.executemany.call_args.args
        return query, params_list[0]

    def test_save_writes_pred_before_bias_when_column_exists(self) -> None:
        query, params = self._save_with_bias_column(True)
        self.assertIn("pred_before_bias", query)
        self.assertEqual(query.count("%s"), len(params))
        self.assertEqual(params[3:5], (90.0, 100.0))

    def test_save_omits_pred_before_bias_without_column(self) -> None:
        query, params = self._save_with_bias_column(False)
        self.assertNotIn("pred_before_bias", query)
        self.assertEqual(query.count("%s"), len(params))
        self.assertEqual(params[3:5], (90.0, 80.0))

    def test_retrain_defaults_keep_nonwork_and_bias_corrections(self) -> None:
        from app.services.v5_common import RETRAIN_POST_PROCESSING_DEFAULTS

        self.assertTrue(RETRAIN_POST_PROCESSING_DEFAULTS["nonwork_correction"])
        self.assertTrue(RETRAIN_POST_PROCESSING_DEFAULTS["bias_correction"])
        self.assertNotIn("holiday_adjacent_correction", RETRAIN_POST_PROCESSING_DEFAULTS)


class BatchPredictionFlowTests(unittest.TestCase):
    """DB·모델을 대체해 predict_v5_batch 의 보정 흐름(이력 조회·배치 내 반영·저장)을 확인한다."""

    def test_batch_threads_in_batch_predictions_into_bias_history(self) -> None:
        from app.services import usage_prediction_v5_service as service

        date_from, date_to = date(2026, 9, 1), date(2026, 10, 16)
        hist_days = pd.bdate_range(end="2026-08-31", periods=30)
        energy = pd.DataFrame({
            "date": hist_days,
            "mix_prod_kg": 50_000.0,
            "total_power_kwh": 1000.0,
            "fuel_nm3": 1000.0,
            "water_ton": 1000.0,
        })
        # DB 이력은 편향 없음(실측=예측) → 배치 초반엔 보정이 거의 없다.
        db_history = {"연료": pd.DataFrame({
            "pred_date": hist_days[-20:],
            "pred_ref": 1000.0,
            "actual": 1000.0,
            "mix_prod_kg": 50_000.0,
        })}
        fetch_calls: list[tuple] = []

        def fake_fetch_bias(factory, d_from, d_to, targets, holiday_set):
            fetch_calls.append((factory, d_from, d_to, tuple(targets)))
            return {k: v.copy() for k, v in db_history.items()}

        # 배치 구간의 실측은 모델 출력보다 20% 낮다.
        def fake_infer(model_dict, factory, tgt, pred_d, df_source):
            return {"pred": 1000.0, "pred_p05": 900.0, "pred_p50": 1000.0, "pred_p95": 1100.0,
                    "actual": 800.0, "model_kind": "quantile"}

        saved: list[list[dict]] = []
        with patch.object(service, "_WEATHER_SYNC_AVAILABLE", False), \
                patch.object(service, "_cached_holidays", return_value=HOLIDAYS), \
                patch.object(service, "get_active_model", return_value={"광주": {}}), \
                patch.object(service, "get_active_model_path", return_value=Path("m.pkl")), \
                patch.object(service, "_drift_correction_enabled", return_value=False), \
                patch.object(service, "_nonwork_correction_enabled", return_value=True), \
                patch.object(service, "_bias_correction_enabled", return_value=True), \
                patch.object(service, "_prediction_log_has_bias_column", return_value=True), \
                patch.object(service, "_collect_required_wip_item_codes", return_value=[]), \
                patch.object(service, "_fetch_energy_history", return_value=energy), \
                patch.object(service, "_cached_weather_station", return_value=pd.DataFrame()), \
                patch.object(service, "get_mix_prod_prefill", return_value=50_000.0), \
                patch.object(service, "_resolve_weather_for_date", return_value={}), \
                patch.object(service, "_fetch_bias_history", side_effect=fake_fetch_bias), \
                patch.object(service, "_infer_target", side_effect=fake_infer), \
                patch.object(service, "_save_prediction_log", side_effect=lambda rows: saved.append(rows)):
            results = service.predict_v5_batch("광주", date_from, date_to, targets=["연료"])

        # 이력은 배치 시작 전 120일 ~ 마지막 근무일(10-16) 전날까지 한 번만 조회한다.
        self.assertEqual(fetch_calls, [("광주", date(2026, 5, 4), date(2026, 10, 15), ("연료",))])
        first = results[0]["results"]["연료"]
        last = results[-1]["results"]["연료"]
        self.assertNotIn("bias_correction", first)  # DB 이력 비율 1.0
        self.assertAlmostEqual(last["bias_correction"]["factor"], 0.8)  # 배치 내 20일이 창을 채움
        self.assertAlmostEqual(last["pred"], 800.0)
        self.assertEqual(last["pred_before_bias"], 1000.0)
        self.assertEqual(len(saved), 1)
        self.assertTrue(all(row["pred_before_bias"] == 1000.0 for row in saved[0]))


class FitToolReplayTests(unittest.TestCase):
    def test_replay_applies_nonwork_and_bias_without_post_weekend_multiplier(self) -> None:
        from tools.fit_v5_post_correction import holiday_set_from_rows, replay_new_corrections

        days = pd.date_range("2026-08-24", "2026-10-16", freq="D")
        rows = pd.DataFrame({
            "date": days,
            "factory": "광주",
            "target": "연료",
            "raw_pred": 110.0,
            "actual": 100.0,
            "current_pred": 110.0,
            "holiday_factor": 1.0,
            "mix_prod_kg": 50_000.0,
        })
        rows["is_workday"] = (rows["date"].dt.dayofweek < 5) & (rows["date"].dt.date != date(2026, 10, 9))
        rows.loc[~rows["is_workday"], "mix_prod_kg"] = 0.0
        holidays = holiday_set_from_rows(rows)
        self.assertEqual(holidays, HOLIDAYS)

        out = replay_new_corrections(rows, holidays).set_index("date")
        sat = out.loc[pd.Timestamp(SATURDAY) - pd.Timedelta(days=7)]
        self.assertAlmostEqual(sat["new_pred"], 110.0 * pc.NONWORK_MULTIPLIERS["광주"]["연료"]["no_production"])
        # 연휴 직후 첫 근무일은 비근무일 배수 대상이 아니고 편향 보정만 받는다.
        post = out.loc[pd.Timestamp(POST_LONG_WEEKEND)]
        self.assertAlmostEqual(post["new_pred"], 100.0)
        first = out[out["is_workday"]].iloc[0]
        self.assertEqual(first["new_pred"], 110.0)


if __name__ == "__main__":
    unittest.main()
