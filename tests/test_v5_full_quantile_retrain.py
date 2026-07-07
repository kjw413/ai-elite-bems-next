from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import v5_band_calibration as bandcal
from app.services import v5_common
from app.services import v5_retrain_lock as lock
from app.services import v5_retrain_worker as worker
from app.services.v5_common import resolve_cqr_margins
from app.services.v5_quantile_training import (
    DEFAULT_M_TYPES,
    MODELS_PER_QUANTILE,
    QUANTILES,
    predict_quantile_probe,
    validate_quantile_spec_structure,
)


def _check(condition: bool, message: str, failures: list[str]) -> None:
    print(("  OK " if condition else "  NG ") + message)
    if not condition:
        failures.append(message)


# =============================================================================
# 합성 분위수 spec (실제 학습 없이 구조/예측 검증용)
# =============================================================================
class _FakeModel:
    """log1p 공간에서 상수를 반환하는 가짜 모델(predict_quantile_probe 가 expm1 적용)."""

    def __init__(self, value: float) -> None:
        self.value = float(value)

    def predict(self, X) -> np.ndarray:
        return np.array([np.log1p(self.value)] * len(X))


def _make_synthetic_spec() -> tuple[dict, pd.DataFrame]:
    m_types = list(DEFAULT_M_TYPES)
    features = {mt: ["f1", "f2"] for mt in m_types}
    level_for_q = {0.05: 10.0, 0.50: 20.0, 0.95: 30.0}
    models_by_q = {q: [_FakeModel(level_for_q[q]) for _ in range(MODELS_PER_QUANTILE)] for q in QUANTILES}
    weights_by_q = {q: np.ones(MODELS_PER_QUANTILE) / MODELS_PER_QUANTILE for q in QUANTILES}
    spec = {
        "models_by_q": models_by_q,
        "weights_by_q": weights_by_q,
        "quantiles": list(QUANTILES),
        "m_types": m_types,
        "features": features,
        "target_col": "전력량[kWh]",
        "cqr_q_hat": 1.0,
    }
    probe_X = pd.DataFrame({"f1": [1.0], "f2": [2.0]})
    return spec, probe_X


def _test_rolling_split(failures: list[str]) -> None:
    print("[rolling split]")
    dates = pd.date_range("2026-01-01", periods=10, freq="D")
    d = pd.DataFrame({"date": dates})

    res = worker._rolling_split_masks(d, test_bdays=3, valid_bdays=4, min_train_rows=2)
    _check(res is not None, "split returned for sufficient data", failures)
    if res is not None:
        mtr, mva, mte, split = res
        _check(int(mtr.sum()) == 3, "train mask count = n - test - valid (3)", failures)
        _check(int(mva.sum()) == 4, "valid mask count = valid_bdays (4)", failures)
        _check(int(mte.sum()) == 3, "test mask count = test_bdays (3)", failures)
        # 겹침 없음 + 전체 커버
        overlap = (mtr & mva) | (mva & mte) | (mtr & mte)
        _check(not overlap.any(), "masks are mutually exclusive", failures)
        _check(int(mtr.sum() + mva.sum() + mte.sum()) == len(d), "masks cover all rows", failures)
        _check(split["train_end"] == "2026-01-03", "train_end boundary date", failures)
        _check(split["valid_start"] == "2026-01-04", "valid_start boundary date", failures)
        _check(split["valid_end"] == "2026-01-07", "valid_end boundary date", failures)
        _check(split["test_start"] == "2026-01-08", "test_start boundary date", failures)
        _check(split["test_end"] == "2026-01-10", "test_end boundary date", failures)

    short = worker._rolling_split_masks(d.head(8), test_bdays=3, valid_bdays=4, min_train_rows=2)
    _check(short is None, "data shortage returns None", failures)


def _test_spec_structure(failures: list[str]) -> None:
    print("[quantile spec structure]")
    spec, _ = _make_synthetic_spec()
    _check(validate_quantile_spec_structure(spec) == [], "well-formed spec passes structure check", failures)

    # 모델 1개 누락 → TOTAL_MODELS_PER_TARGET 미만 + 분위수당 MODELS_PER_QUANTILE 미만
    bad = dict(spec)
    bad_models = {q: list(v) for q, v in spec["models_by_q"].items()}
    bad_models[0.05] = bad_models[0.05][:-1]
    bad["models_by_q"] = bad_models
    _check(len(validate_quantile_spec_structure(bad)) > 0, "missing model is rejected", failures)

    # 가중치 길이 불일치
    bad2 = dict(spec)
    bad2_w = {q: np.array(v, dtype=float) for q, v in spec["weights_by_q"].items()}
    bad2_w[0.50] = np.ones(5) / 5.0
    bad2["weights_by_q"] = bad2_w
    _check(len(validate_quantile_spec_structure(bad2)) > 0, "weight/model count mismatch is rejected", failures)

    # 비유한 q_hat
    bad3 = dict(spec)
    bad3["cqr_q_hat"] = float("inf")
    _check(len(validate_quantile_spec_structure(bad3)) > 0, "non-finite q_hat is rejected", failures)

    # 분위수 키 누락
    bad4 = dict(spec)
    bad4_models = {q: list(v) for q, v in spec["models_by_q"].items()}
    bad4_models.pop(0.95)
    bad4["models_by_q"] = bad4_models
    _check(len(validate_quantile_spec_structure(bad4)) > 0, "missing quantile is rejected", failures)


def _test_probe_prediction(failures: list[str]) -> None:
    print("[probe prediction]")
    spec, probe_X = _make_synthetic_spec()
    p05, p50, p95 = predict_quantile_probe(spec, probe_X)
    _check(p05 <= p50 <= p95, "probe prediction is monotone (P05<=P50<=P95)", failures)
    _check(all(np.isfinite([p05, p50, p95])), "probe predictions are finite", failures)
    # q_hat=1 → p05=10-1=9, p95=30+1=31
    _check(abs(p05 - 9.0) < 1e-6, "P05 = level - q_hat", failures)
    _check(abs(p50 - 20.0) < 1e-6, "P50 = median level", failures)
    _check(abs(p95 - 31.0) < 1e-6, "P95 = level + q_hat", failures)


def _test_cqr_margins(failures: list[str]) -> None:
    print("[asymmetric CQR margins (§10.4)]")
    # 대칭 fallback: cqr_q_hat 만 있으면 양쪽 동일.
    lo, hi = resolve_cqr_margins({"cqr_q_hat": 2.0})
    _check(abs(lo - 2.0) < 1e-9 and abs(hi - 2.0) < 1e-9, "symmetric spec falls back to cqr_q_hat both sides", failures)

    # 비대칭 우선: lower/upper 가 있으면 그 값을 사용.
    lo, hi = resolve_cqr_margins({"cqr_q_hat": 2.0, "cqr_q_hat_lower": 1.0, "cqr_q_hat_upper": 5.0})
    _check(abs(lo - 1.0) < 1e-9 and abs(hi - 5.0) < 1e-9, "asymmetric keys take precedence", failures)

    # 한쪽만 있으면 나머지는 대칭 fallback.
    lo, hi = resolve_cqr_margins({"cqr_q_hat": 2.0, "cqr_q_hat_upper": 5.0})
    _check(abs(lo - 2.0) < 1e-9 and abs(hi - 5.0) < 1e-9, "missing lower falls back to cqr_q_hat", failures)

    # 음수/비유한/누락은 0 으로 방어.
    lo, hi = resolve_cqr_margins({"cqr_q_hat_lower": -3.0, "cqr_q_hat_upper": float("inf")})
    _check(lo == 0.0 and hi == 0.0, "negative/non-finite margins clamp to 0", failures)
    lo, hi = resolve_cqr_margins({})
    _check(lo == 0.0 and hi == 0.0, "empty spec yields zero margins", failures)

    # probe 가 비대칭 보정을 반영하는지: level 30 상단 + upper 5 = 35, 하단 10 - lower 1 = 9.
    spec, probe_X = _make_synthetic_spec()
    spec["cqr_q_hat_lower"] = 1.0
    spec["cqr_q_hat_upper"] = 5.0
    p05, p50, p95 = predict_quantile_probe(spec, probe_X)
    _check(abs(p05 - 9.0) < 1e-6, "probe P05 uses q_hat_lower", failures)
    _check(abs(p95 - 35.0) < 1e-6, "probe P95 uses q_hat_upper", failures)


def _test_band_calibration(failures: list[str]) -> None:
    print("[band calibration layer (§10.5)]")
    # 항목 없음 → 항등(원값 그대로, info=None).
    p05, p95, info = bandcal.apply_band_calibration("김해", "연료", 10.0, 20.0, 30.0, calibration={"calibration": {}})
    _check(p05 == 10.0 and p95 == 30.0 and info is None, "no entry → identity band", failures)

    # 상단 가산보정만 적용: p95 = 30 + 4 = 34, p05 불변.
    calib = {"calibration": {"김해": {"연료": {"upper_margin": 4.0}}}}
    p05, p95, info = bandcal.apply_band_calibration("김해", "연료", 10.0, 20.0, 30.0, calibration=calib)
    _check(abs(p95 - 34.0) < 1e-9, "upper_margin widens P95", failures)
    _check(p05 == 10.0, "upper-only calibration leaves P05", failures)
    _check(info is not None, "calibration returns info dict", failures)

    # 상단 factor 1.5: 반폭 10 → 15 → p95 = 20 + 15 = 35.
    calib2 = {"calibration": {"김해": {"연료": {"upper_factor": 1.5}}}}
    _, p95, _ = bandcal.apply_band_calibration("김해", "연료", 10.0, 20.0, 30.0, calibration=calib2)
    _check(abs(p95 - 35.0) < 1e-9, "upper_factor scales half-width around P50", failures)

    # 뒤집힘 방지: 음수 하단 factor 로 p05 가 p50 을 넘지 않도록 클램프.
    calib3 = {"calibration": {"김해": {"연료": {"lower_factor": -5.0}}}}
    p05, _, _ = bandcal.apply_band_calibration("김해", "연료", 10.0, 20.0, 30.0, calibration=calib3)
    _check(p05 <= 20.0, "calibrated P05 never crosses P50", failures)

    # 이력 기반 산출: 초과율 높은 항목만 상향 margin 을 만든다.
    rows = []
    # 김해 연료: p95=100 고정, actual 이 자주 초과(120 이 절반) → 상향 margin 필요.
    for i in range(40):
        actual = 130.0 if i % 2 == 0 else 90.0
        rows.append({"factory": "김해", "target": "연료", "actual_value": actual, "pred_p95": 100.0})
    # 광주 용수: 초과 거의 없음 → margin 미생성.
    for i in range(40):
        rows.append({"factory": "광주", "target": "용수", "actual_value": 50.0, "pred_p95": 100.0})
    table = bandcal.compute_calibration_from_rows(rows, target_rate=0.05, min_samples=20)
    _check("김해" in table and "연료" in table.get("김해", {}), "high-exceed item gets calibration entry", failures)
    if "김해" in table and "연료" in table["김해"]:
        _check(table["김해"]["연료"]["upper_margin"] > 0, "high-exceed upper_margin is positive", failures)
    _check("광주" not in table, "low-exceed item gets no calibration (no shrink)", failures)

    # 표본 부족 → 생성 안 함.
    few = bandcal.compute_calibration_from_rows(rows[:5], target_rate=0.05, min_samples=20)
    _check(few == {}, "insufficient samples produce no calibration", failures)


def _test_candidate_validation(failures: list[str]) -> None:
    print("[candidate validation gate]")
    spec, probe_X = _make_synthetic_spec()
    factories = ["남양주1"]
    targets = ["전력"]
    probes = {("남양주1", "전력"): {"probe_X": probe_X, "probe_y": 20.0}}

    good_model = {"남양주1": {"전력": spec}}
    try:
        worker._validate_quantile_candidate(good_model, probes, factories, targets)
        _check(True, "valid candidate passes _validate_quantile_candidate", failures)
    except Exception as exc:  # pragma: no cover
        _check(False, f"valid candidate unexpectedly raised: {exc}", failures)

    # 대상 spec 누락 → 예외
    raised = False
    try:
        worker._validate_quantile_candidate({"남양주1": {}}, probes, factories, targets)
    except Exception:
        raised = True
    _check(raised, "missing target spec fails candidate validation", failures)


def _test_retention_cleanup(failures: list[str]) -> None:
    print("[artifact retention cleanup]")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        stamps = ["20260101_000000", "20260102_000000", "20260103_000000", "20260104_000000"]
        paths = []
        for i, s in enumerate(stamps):
            p = base / f"v5.3_{s}.pkl"
            p.write_bytes(b"x")
            v5_common.perf_report_path_for(p).write_text("{}", encoding="utf-8")
            # mtime 오름차순(나중 stamp = 더 최신)
            os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
            paths.append(p)
        # 고정 프리셋 파일(버전 아님)은 정리 대상에서 제외돼야 함
        preset = base / "v5.3.pkl"
        preset.write_bytes(b"x")

        active = paths[-1]  # 가장 최신 = 활성
        deleted = v5_common.cleanup_model_artifacts("v5.3", keep=2, base_dir=base, active_path=active)

        _check(active.exists(), "active model is always kept", failures)
        _check(paths[-2].exists(), "most recent previous model is kept (keep=2)", failures)
        _check(not paths[0].exists(), "oldest model is deleted", failures)
        _check(not paths[1].exists(), "second-oldest model is deleted", failures)
        _check(preset.exists(), "fixed preset (v5.3.pkl) is never touched", failures)
        _check(
            not v5_common.perf_report_path_for(paths[0]).exists(),
            "deleted model's perf sidecar is also removed",
            failures,
        )
        _check(len(deleted) == 4, "two pkl + two sidecars deleted", failures)


def _test_artifact_post_processing(failures: list[str]) -> None:
    print("[artifact post_processing metadata]")
    _check(
        v5_common.RETRAIN_POST_PROCESSING_DEFAULTS.get("holiday_adjacent_correction") is False,
        "retrain disables holiday adjacent correction by default",
        failures,
    )
    _check(
        v5_common.RETRAIN_POST_PROCESSING_DEFAULTS.get("drift_correction") is True,
        "retrain keeps drift (223579) correction by default",
        failures,
    )
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "v5.3_20260101_000000.pkl"
        p.write_bytes(b"hello-model")
        art = v5_common.build_model_artifact_metadata(
            p,
            model_key="v5.3",
            training_profile="quantile_wip",
            post_processing=dict(v5_common.RETRAIN_POST_PROCESSING_DEFAULTS),
            schema_version=2,
        )
        _check(art.get("schema_version") == 2, "artifact schema_version recorded", failures)
        _check(
            art.get("post_processing", {}).get("holiday_adjacent_correction") is False,
            "artifact carries post_processing flags",
            failures,
        )


def _test_lock_run_id(failures: list[str]) -> None:
    print("[lock run_id ownership]")
    original_lock_path = lock.LOCK_PATH
    with tempfile.TemporaryDirectory() as tmp:
        lock.LOCK_PATH = Path(tmp) / "v5_training.lock"
        try:
            _check(
                lock.acquire_training_lock(trigger_mode="manual", run_id="RUN_A"),
                "acquire lock with run_id",
                failures,
            )
            data = lock.read_training_lock()
            _check(data.get("run_id") == "RUN_A", "lock records run_id", failures)

            lock.mark_worker_started(os.getpid(), run_id="RUN_A", trigger_mode="manual")
            data = lock.read_training_lock()
            _check(data.get("worker_pid") == os.getpid(), "worker pid recorded under same run_id", failures)

            # 다른 run_id 의 heartbeat 는 잠금을 덮어쓰지 못함
            lock.touch_training_lock(worker_pid=os.getpid(), run_id="RUN_B", state="running")
            data = lock.read_training_lock()
            _check(data.get("run_id") == "RUN_A", "different run_id cannot take over lock", failures)

            # 다른 run_id 로는 해제 불가
            _check(
                not lock.release_training_lock(owner_pid=os.getpid(), owner_run_id="RUN_B"),
                "wrong run_id cannot release lock",
                failures,
            )
            _check(lock.LOCK_PATH.exists(), "lock remains after wrong run_id release", failures)

            # 올바른 run_id + pid 로 해제
            _check(
                lock.release_training_lock(owner_pid=os.getpid(), owner_run_id="RUN_A"),
                "correct run_id + pid releases lock",
                failures,
            )
            _check(not lock.LOCK_PATH.exists(), "lock removed after correct release", failures)
        finally:
            try:
                lock.LOCK_PATH.unlink(missing_ok=True)  # type: ignore[call-arg]
            except Exception:
                pass
            lock.LOCK_PATH = original_lock_path


def main() -> int:
    failures: list[str] = []
    _test_rolling_split(failures)
    _test_spec_structure(failures)
    _test_probe_prediction(failures)
    _test_cqr_margins(failures)
    _test_band_calibration(failures)
    _test_candidate_validation(failures)
    _test_retention_cleanup(failures)
    _test_artifact_post_processing(failures)
    _test_lock_run_id(failures)

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll v5 full-quantile retrain checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
