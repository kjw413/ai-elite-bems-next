"use client";

import { useEffect, useState } from "react";
import { RefreshCw, TrendingUp } from "lucide-react";
import { Area, CartesianGrid, ComposedChart, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { apiRequest, isAbortError, query } from "@/lib/bems-api";
import { downloadCsv } from "@/lib/bems-csv";
import { PivotTable, type PivotRow } from "@/components/pivot-table";
import { DataToggle } from "@/components/data-toggle";

type GapPoint = {
  date: string; fullDate: string;
  predicted: number | null; actual: number | null;
  lower: number | null; upper: number | null; band: [number, number] | null;
  gap: number | null; gapPct: number | null; status: string;
};
type GapSummary = {
  days: number; measuredDays: number; outsideDays: number;
  meanGapPct: number | null; meanAbsGapPct: number | null;
};

const TARGETS = ["전력", "연료", "용수"] as const;
const UNITS: Record<string, string> = { 전력: "MWh", 연료: "천 Nm³", 용수: "천 ton" };
const numberText = (value: unknown) => typeof value === "number" ? value.toLocaleString("ko-KR", { maximumFractionDigits: 2 }) : "-";
const tooltipStyle = { contentStyle: { borderRadius: 10, border: "1px solid var(--line)", background: "var(--card)", fontSize: 12 }, labelStyle: { color: "var(--text)" } };

function shiftIso(iso: string, days: number) {
  const parsed = Date.parse(`${iso.slice(0, 10)}T00:00:00`);
  if (Number.isNaN(parsed)) return iso;
  const shifted = new Date(parsed + days * 86_400_000);
  return `${shifted.getFullYear()}-${String(shifted.getMonth() + 1).padStart(2, "0")}-${String(shifted.getDate()).padStart(2, "0")}`;
}

// 예측이 실측과 얼마나, 어느 방향으로 벌어졌는지 기간을 잡아 본다.
// 단일 시점 판정만으로는 '매일 조금씩 계속 높은' 편향을 볼 수 없어, 위(수준)와
// 아래(편향 방향)를 축을 맞춰 2단으로 그린다.
export function PredictionGap({ factory, date }: { factory: string; date: string }) {
  const [target, setTarget] = useState<(typeof TARGETS)[number]>("전력");
  const [from, setFrom] = useState(() => shiftIso(date, -29));
  const [to, setTo] = useState(date);
  const [series, setSeries] = useState<GapPoint[]>([]);
  const [summary, setSummary] = useState<GapSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  // 상단 기준일이 바뀌면 조회창도 그 기준으로 다시 잡는다.
  useEffect(() => { setFrom(shiftIso(date, -29)); setTo(date); }, [date]);

  useEffect(() => {
    if (!from || !to) return;
    const abort = new AbortController();
    setLoading(true);
    setError("");
    apiRequest<{ series: GapPoint[]; summary: GapSummary }>(
      `/predictions/gap?${query({ factory, target, date_from: from, date_to: to })}`,
      { signal: abort.signal },
    )
      .then(response => {
        if (abort.signal.aborted) return;
        setSeries(response.series ?? []);
        setSummary(response.summary ?? null);
      })
      .catch(requestError => {
        if (isAbortError(requestError)) return;
        setError(requestError instanceof Error ? requestError.message : "괴리 추이를 불러오지 못했습니다.");
        setSeries([]); setSummary(null);
      })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [factory, target, from, to]);

  const unit = UNITS[target] ?? "";
  const pivotRows: PivotRow[] = [
    { key: "predicted", label: `예측 P50(${unit})`, values: series.map(point => point.predicted) },
    { key: "actual", label: `실측(${unit})`, values: series.map(point => point.actual) },
    { key: "gap", label: `괴리(${unit})`, values: series.map(point => point.gap),
      className: value => value == null ? undefined : Number(value) >= 0 ? "bad" : "good" },
    { key: "gapPct", label: "괴리율(%)", values: series.map(point => point.gapPct),
      total: summary?.meanGapPct ?? null,
      format: value => value == null ? "-" : `${Number(value) > 0 ? "+" : ""}${Number(value).toFixed(1)}`,
      className: value => value == null ? undefined : Number(value) >= 0 ? "bad" : "good" },
  ];

  return <article className="card chart-card span-all">
    <header className="card-title"><h3>예측 대비 실측 괴리 추이</h3><div className="card-title-side">
      {series.length > 0 && <button type="button" className="csv-button"
        onClick={() => downloadCsv(`prediction_gap_${factory}_${target}_${from}_${to}`, series,
          ["fullDate", "predicted", "actual", "gap", "gapPct", "status"],
          { fullDate: "일자", predicted: `예측(${unit})`, actual: `실측(${unit})`, gap: `괴리(${unit})`, gapPct: "괴리율(%)", status: "판정" })}>
        <TrendingUp size={13}/>CSV</button>}
      <span>{unit}</span>
    </div></header>
    <div className="mode-row">
      <div className="segmented" role="group" aria-label="괴리 조회 지표">
        {TARGETS.map(item => <button type="button" key={item} className={target === item ? "active" : ""} aria-pressed={target === item} onClick={() => setTarget(item)}>{item}</button>)}
      </div>
      <div className="range-fields">
        <label><span>시작일</span><input type="date" value={from} max={to} onChange={event => setFrom(event.target.value)}/></label>
        <label><span>종료일</span><input type="date" value={to} min={from} onChange={event => setTo(event.target.value)}/></label>
      </div>
    </div>
    {error && <div className="form-message error">{error}</div>}
    {loading && <div className="loading inline-loading"><RefreshCw className="spin"/>괴리 추이를 불러오는 중입니다.</div>}
    {!loading && !error && series.length === 0 && <p className="panel-copy">선택 기간에 예측 이력이 없습니다.</p>}
    {!loading && !error && series.length > 0 && <>
      {summary && <div className="kpi-grid compact">
        <article className="kpi card"><div className="kpi-icon"><TrendingUp size={20}/></div><div>
          <p>평균 편향</p><strong>{summary.meanGapPct == null ? "-" : `${summary.meanGapPct > 0 ? "+" : ""}${summary.meanGapPct}`} <small>%</small></strong>
          <span className="kpi-note">실측이 예측보다 {Number(summary.meanGapPct) >= 0 ? "높은" : "낮은"} 쪽으로 치우친 정도</span>
        </div></article>
        <article className="kpi card"><div className="kpi-icon"><TrendingUp size={20}/></div><div>
          <p>평균 오차</p><strong>{summary.meanAbsGapPct ?? "-"} <small>%</small></strong>
          <span className="kpi-note">방향과 무관한 예측 정확도</span>
        </div></article>
        <article className="kpi card"><div className="kpi-icon"><TrendingUp size={20}/></div><div>
          <p>정상범주 이탈</p><strong>{summary.outsideDays} <small>/ {summary.measuredDays}일</small></strong>
          <span className="kpi-note">실측이 P05~P95를 벗어난 날</span>
        </div></article>
      </div>}
      <div className="gap-charts">
        <div>
          <p className="quad-caption">① 예측 vs 실측 — 수준 비교</p>
          <div className="chart gap-chart"><ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={series}>
              <defs><linearGradient id="gapBand" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stopColor="var(--chart-band)" stopOpacity={0.22}/>
                <stop offset="1" stopColor="var(--chart-band)" stopOpacity={0.02}/>
              </linearGradient></defs>
              <CartesianGrid vertical={false}/>
              <XAxis dataKey="date" tick={{ fontSize: 11 }} interval="preserveStartEnd" minTickGap={18}/>
              <YAxis tick={{ fontSize: 11 }} domain={["auto", "auto"]}/>
              <Tooltip {...tooltipStyle} formatter={(value: unknown) => numberText(value)}/>
              <Area type="linear" dataKey="band" name="정상범주(P05~P95)" stroke="none" fill="url(#gapBand)" connectNulls={false}/>
              <Line type="linear" dataKey="predicted" name="예측 P50" stroke="var(--chart-predicted)" strokeWidth={2} strokeDasharray="5 4" dot={false} connectNulls/>
              <Line type="linear" dataKey="actual" name="실측" stroke="var(--chart-actual)" strokeWidth={2} dot={false} connectNulls/>
            </ComposedChart>
          </ResponsiveContainer></div>
        </div>
        <div>
          <p className="quad-caption">② 괴리율(%) — 0선 위는 실측이 예측보다 높음</p>
          <div className="chart gap-chart"><ResponsiveContainer width="100%" height="100%">
            <LineChart data={series}>
              <CartesianGrid vertical={false}/>
              <XAxis dataKey="date" tick={{ fontSize: 11 }} interval="preserveStartEnd" minTickGap={18}/>
              <YAxis tick={{ fontSize: 11 }} domain={["auto", "auto"]}/>
              <Tooltip {...tooltipStyle} formatter={(value: unknown) => typeof value === "number" ? `${value > 0 ? "+" : ""}${value}%` : "-"}/>
              <ReferenceLine y={0} stroke="var(--muted)" strokeWidth={1.5}/>
              <Line type="linear" dataKey="gapPct" name="괴리율(%)" stroke="var(--chart-amber)" strokeWidth={2} dot={{ r: 2 }} connectNulls/>
            </LineChart>
          </ResponsiveContainer></div>
        </div>
      </div>
      <DataToggle><PivotTable periods={series.map(point => point.date)} periodLabel="일자" rows={pivotRows} totalLabel="기간 평균"/></DataToggle>
    </>}
  </article>;
}
