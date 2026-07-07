"""5월 남양주2 예측을 재실행해 prediction_log를 새 보정룰로 갱신.

근로자의 날(5/1)이 이제 holiday_set에 포함되므로 batch의 is_workday 필터에서 제외됨.
override_df를 사용해 모든 prediction_log 기존 일자를 강제 포함시킴.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database.db_connection import get_connection
from app.services.usage_prediction_v5_service import predict_v5_batch


def _load_existing_dates(factory: str, date_from: date, date_to: date) -> pd.DataFrame:
    """prediction_log에 이미 있는 (날짜, 생산량) 쌍을 로드."""
    sql = """
        SELECT DISTINCT pred_date AS 날짜, mix_prod_kg AS `믹스생산량[kg]`
        FROM prediction_log
        WHERE factory = %s AND pred_date BETWEEN %s AND %s
        ORDER BY pred_date
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, (factory, date_from.strftime("%Y-%m-%d"), date_to.strftime("%Y-%m-%d")))
        rows = cur.fetchall()
        return pd.DataFrame(rows, columns=["날짜", "믹스생산량[kg]"])
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()


def main() -> None:
    factory = "남양주2"
    date_from = date(2026, 5, 1)
    date_to = date(2026, 5, 15)
    override_df = _load_existing_dates(factory, date_from, date_to)
    override_df["날짜"] = pd.to_datetime(override_df["날짜"])
    print(f"Rerunning predictions for {factory}: {date_from} ~ {date_to}")
    print(f"override_df rows: {len(override_df)} ({list(pd.to_datetime(override_df['날짜']).dt.strftime('%Y-%m-%d'))})")
    results = predict_v5_batch(
        factory=factory,
        date_from=date_from,
        date_to=date_to,
        override_df=override_df,
        save_to_db=True,
    )
    print(f"Generated {len(results)} day-results.")
    for r in results:
        d = r.get("date")
        for tgt in ("연료", "용수", "전력"):
            tr = (r.get("results") or {}).get(tgt) or {}
            if "error" in tr:
                print(f"  {d} {tgt}: ERROR {tr['error']}")
                continue
            pred = tr.get("pred")
            hol = tr.get("holiday_correction")
            if hol:
                pre = tr.get("pred_before_holiday_adj")
                print(f"  {d} {tgt}: pre={pre:.1f} → post={pred:.1f} | rule={hol['rule']} ×{hol['factor']}")
            else:
                print(f"  {d} {tgt}: pred={pred:.1f} (no holiday correction)")


if __name__ == "__main__":
    main()
