# predict_v5_batch 재귀 lag 스모크 테스트(실DB+활성모델, 저장 안 함).
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.services.usage_prediction_v5_service as svc  # noqa: E402

out = []


def p50s(factory, d0, d1):
    res = svc.predict_v5_batch(factory, d0, d1, targets=["전력"], save_to_db=False)
    return [(r["date"], round(float(r["results"]["전력"]["pred"]), 0))
            for r in res if "전력" in r["results"] and "pred" in r["results"]["전력"]]


# 1) 과거 구간(실측 존재) — 재귀 미발동, 정상 동작 확인
past = p50s("남양주2", date(2026, 6, 8), date(2026, 6, 12))
out.append(f"[과거/실측 구간] n={len(past)}  (재귀 미발동, fresh-lag)")
out.append("  " + ", ".join(f"{d}:{v:.0f}" for d, v in past))

# 2) 미래 구간 — 재귀 ON vs OFF 비교
fut0, fut1 = date(2026, 6, 23), date(2026, 6, 27)
svc.RECURSIVE_FUTURE_LAG = True
on = p50s("남양주2", fut0, fut1)
svc.RECURSIVE_FUTURE_LAG = False
off = p50s("남양주2", fut0, fut1)
svc.RECURSIVE_FUTURE_LAG = True

out.append("")
out.append(f"[미래 구간] {fut0}~{fut1}  재귀 ON vs OFF(동결)")
out.append("  date         recursive   frozen   diff")
omap = dict(off)
for d, v in on:
    fv = omap.get(d, float("nan"))
    out.append(f"  {d}   {v:>9.0f} {fv:>8.0f} {v-fv:>+7.0f}")
diff_any = any(abs(v - omap.get(d, v)) > 1e-6 for d, v in on)
out.append(f"  >> 재귀로 예측이 바뀜? {diff_any} (True 면 재귀 경로 활성)")

rep = "\n".join(out)
(Path(__file__).parent / "_smoke_recursive.txt").write_text(rep, encoding="utf-8")
print(rep)
