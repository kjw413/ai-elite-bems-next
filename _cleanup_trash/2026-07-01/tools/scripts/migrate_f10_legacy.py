# 이 스크립트는 production_daily 의 남양주 F10 통합 코드를
# F10A(남양주1)/F10B(남양주2) 로 자동 분리하는 일회성 마이그레이션입니다.
#
# 새 분류 룰 (production_dw_service.resolve_namyangju_factory):
#   - 냉장 + category2=MY  →  F10A (남양주1)
#   - 그 외 모든 조합       →  F10B (남양주2)
#
# 처리 단계:
#   1) DB 스냅샷 — F10/F10A/F10B/기타 별 행수
#   2) DB_생산실적.xlsx 백업 (.bak.YYYYMMDD)
#   3) Raw 폴더 → 새 룰로 재통합 → DB_생산실적.xlsx 재기록
#   4) 동기화 상태 JSON 삭제 (mtime skip 우회)
#   5) auto_sync_production_once(force=True) — F10A/F10B 로 UPSERT
#   6) DELETE FROM production_daily WHERE factory='F10' (legacy 정리)
#   7) 사후 검증 — F10 행수 0 확인, F10A/F10B 행수 보고
#
# 사용법:
#   .\.venv\Scripts\python.exe tools/migrate_f10_legacy.py
#   .\.venv\Scripts\python.exe tools/migrate_f10_legacy.py --dry-run
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.database.db_connection import get_connection  # noqa: E402
from app.services.production_dw_service import (  # noqa: E402
    DEFAULT_OUTPUT_PATH,
    DEFAULT_SRC_FOLDER,
    build_dataset,
)
from app.services.production_dw_sync_service import (  # noqa: E402
    SYNC_STATE_PATH,
    auto_sync_production_once,
)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def _factory_counts() -> dict[str, int]:
    """production_daily 의 factory 별 행수 스냅샷."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT factory, COUNT(*) FROM production_daily GROUP BY factory ORDER BY factory"
        )
        return {row[0]: int(row[1]) for row in cur.fetchall()}
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()


def _delete_legacy_f10() -> int:
    """factory='F10' 잔여 행 삭제 후 영향 행수 반환."""
    conn = get_connection(admin=True)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM production_daily WHERE factory = 'F10'")
        deleted = int(cur.rowcount)
        conn.commit()
        return deleted
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()


def _print_counts(label: str, counts: dict[str, int]) -> None:
    print(f"\n[{label}]")
    if not counts:
        print("  (production_daily 비어있음)")
        return
    total = sum(counts.values())
    for f, n in counts.items():
        print(f"  {f:<10} {n:>10,}")
    print(f"  {'TOTAL':<10} {total:>10,}")


def main() -> int:
    p = argparse.ArgumentParser(
        description="남양주 F10 통합 → F10A/F10B 자동 분리 마이그레이션"
    )
    p.add_argument("--src", type=str, default=str(DEFAULT_SRC_FOLDER),
                   help=f"Raw 폴더 (기본: {DEFAULT_SRC_FOLDER})")
    p.add_argument("--out", type=str, default=str(DEFAULT_OUTPUT_PATH),
                   help=f"통합 xlsx (기본: {DEFAULT_OUTPUT_PATH})")
    p.add_argument("--dry-run", action="store_true",
                   help="실제 변경 없이 영향만 미리 확인")
    p.add_argument("--skip-rebuild", action="store_true",
                   help="DB_생산실적.xlsx 재생성을 생략 (이미 새 룰로 재생성된 경우)")
    args = p.parse_args()

    _setup_logging()

    print("=" * 60)
    print(" F10 → F10A/F10B 마이그레이션")
    print(f" 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f" dry-run = {args.dry_run}")
    print("=" * 60)

    # ── (1) 사전 스냅샷 ──
    before = _factory_counts()
    _print_counts("BEFORE — DB 현 상태", before)

    if args.dry_run:
        print("\n[dry-run] 여기서 종료. 실제 변경 시 --dry-run 빼고 다시 실행하세요.")
        return 0

    # ── (2) DW xlsx 백업 ──
    out_path = Path(args.out)
    if out_path.exists():
        bak_path = out_path.with_name(
            out_path.stem + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}" + out_path.suffix
        )
        shutil.copy2(out_path, bak_path)
        print(f"\n✅ 백업 생성: {bak_path.name}")

    # ── (3) Raw 폴더 재통합 → 새 xlsx ──
    if not args.skip_rebuild:
        src_path = Path(args.src)
        if not src_path.exists():
            print(f"\n❌ Raw 폴더 없음: {src_path}")
            return 1
        print(f"\n[STEP 1/4] Raw 폴더 재통합 ({src_path}) ...")
        t0 = time.time()
        df, written = build_dataset(src_folder=src_path, output_path=out_path)
        dt = time.time() - t0
        print(f"  통합 완료 — {len(df):,}행 / {dt:.1f}s → {written}")

        if not df.empty:
            fac_counts = df["factory"].value_counts().to_dict()
            print(f"  factory 분포: {fac_counts}")
            f10_in_dw = int(fac_counts.get("F10", 0))
            if f10_in_dw > 0:
                print(f"  ⚠ 새 통합 xlsx 에 F10 코드가 {f10_in_dw}행 — 분리 룰이 적용 안 됐는지 확인 필요")
    else:
        print("\n[STEP 1/4] (skip-rebuild) DW xlsx 재생성 생략")

    # ── (4) 동기화 상태 JSON 삭제 ──
    print("\n[STEP 2/4] 동기화 상태 초기화")
    if SYNC_STATE_PATH.exists():
        SYNC_STATE_PATH.unlink()
        print(f"  삭제: {SYNC_STATE_PATH.name}")
    else:
        print("  (이미 없음 — skip)")

    # ── (5) DB sync (UPSERT F10A/F10B) ──
    print("\n[STEP 3/4] DB sync (force=True)")
    result = auto_sync_production_once(src_path=out_path, force=True)
    print(f"  status   : {result.get('status')}")
    print(f"  message  : {result.get('message')}")
    if result.get("status") not in ("synced", "unchanged"):
        print(f"  ❌ sync 실패 — 마이그레이션 중단")
        return 2

    # ── (6) legacy F10 행 삭제 ──
    print("\n[STEP 4/4] legacy F10 행 삭제")
    deleted = _delete_legacy_f10()
    print(f"  DELETE 영향 행수: {deleted:,}")

    # ── (7) 사후 검증 ──
    after = _factory_counts()
    _print_counts("AFTER — DB 새 상태", after)

    print("\n" + "=" * 60)
    if int(after.get("F10", 0)) == 0:
        print(" ✅ 마이그레이션 성공 — F10 잔여 0건")
    else:
        print(f" ⚠ F10 행이 여전히 {after['F10']}건 남음 — 점검 필요")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
