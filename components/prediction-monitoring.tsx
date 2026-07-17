"use client";

import { useEffect, useRef, useState } from "react";
import { RefreshCw, Stethoscope } from "lucide-react";
import { apiRequest, isAbortError, query } from "@/lib/bems-api";

type AnyRow = Record<string, any>;
type MonitoringResult = {
  overall: { status?: string; label?: string; message?: string; alert_count?: number; warning_count?: number; normal_count?: number };
  rows: AnyRow[];
};

// legacy 예측 이력 탭 '모델 성능 감지' 패널의 이식 — 최근 bias·패턴 일치·offset을
// prediction_monitoring_service 판정 그대로 보여준다.
const bannerClass: Record<string, string> = {
  offset_alert: "error", degraded: "error",
  watch: "warning", offset_warning: "warning",
  normal: "success",
};

const detailColumns: { key: string; label: string }[] = [
  { key: "factory", label: "공장" },
  { key: "target", label: "항목" },
  { key: "status_label", label: "상태" },
  { key: "latest_bias_pct", label: "Bias(%)" },
  { key: "latest_mape", label: "최근 MAPE(%)" },
  { key: "baseline_mape", label: "기준 MAPE(%)" },
  { key: "direction_accuracy", label: "방향 일치율(%)" },
  { key: "one_sided_rate", label: "한방향 잔차(%)" },
  { key: "estimated_started_at", label: "추정 시작일" },
  { key: "recommendation", label: "권장 조치" },
];

const cell = (value: unknown) => {
  if (value == null || value === "") return "-";
  if (typeof value === "number") return value.toLocaleString("ko-KR", { maximumFractionDigits: 2 });
  return String(value);
};

export function PredictionMonitoring({ factory }: { factory: string }) {
  const [result, setResult] = useState<MonitoringResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setLoading(true);
    setError("");
    apiRequest<MonitoringResult>(`/predictions/monitoring?${query({ factory })}`, { signal: abort.signal })
      .then(response => { if (!abort.signal.aborted) setResult(response); })
      .catch(requestError => {
        if (isAbortError(requestError)) return;
        setError(requestError instanceof Error ? requestError.message : "모니터링 결과를 불러오지 못했습니다.");
      })
      .finally(() => { if (controller.current === abort) setLoading(false); });
    return () => abort.abort();
  }, [factory]);

  const overall = result?.overall;
  const rows = result?.rows ?? [];
  return <article className="card chart-card span-all">
    <header className="card-title">
      <h3>모델 성능 감지</h3>
      <div className="card-title-side"><Stethoscope size={16}/><span>최근 bias · 패턴 일치 · offset</span></div>
    </header>
    {loading && <div className="loading inline-loading"><RefreshCw className="spin"/>모니터링 판정 중입니다.</div>}
    {!loading && error && <div className="form-message error">{error}</div>}
    {!loading && !error && overall && <>
      <div className={`form-message monitor-banner ${bannerClass[overall.status ?? ""] ?? "info"}`}>{overall.message ?? overall.label ?? "-"}</div>
      <div className="monitor-kpis">
        <div><span>전체 상태</span><b>{overall.label ?? "-"}</b></div>
        <div><span>감지</span><b>{overall.alert_count ?? 0}건</b></div>
        <div><span>주의</span><b>{overall.warning_count ?? 0}건</b></div>
        <div><span>정상</span><b>{overall.normal_count ?? 0}건</b></div>
      </div>
      {rows.length > 0
        ? <div className="table-wrap"><table>
            <thead><tr>{detailColumns.map(column => <th key={column.key}>{column.label}</th>)}</tr></thead>
            <tbody>{rows.map((row, index) => <tr key={index}>{detailColumns.map(column => <td key={column.key}>{cell(row[column.key])}</td>)}</tr>)}</tbody>
          </table></div>
        : <p className="panel-copy">실측값이 있는 예측 이력이 쌓이면 자동으로 판정됩니다.</p>}
      <p className="quad-caption">Offset은 증감 방향이 대체로 맞지만 예측-실측 차이가 한쪽으로 지속 누적될 때 감지됩니다.</p>
    </>}
  </article>;
}
