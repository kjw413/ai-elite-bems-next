"use client";

import { Download } from "lucide-react";
import { CartesianGrid, ComposedChart, Line, ResponsiveContainer, Scatter, Tooltip, XAxis, YAxis } from "recharts";
import { downloadCsv } from "@/lib/bems-csv";
import { ToggleLegend, useSeriesToggle } from "@/components/toggle-legend";
import { PivotTable, type PivotRow } from "@/components/pivot-table";

type AnyData = Record<string, any>;

// legacy 대시보드 "7일간 생산량·사용량 방향 비교"의 React 이식.
// 각 값은 최근 7일 중앙값을 100으로 둔 지수로 정규화하고(단위가 다른 생산량과
// 사용량을 한 축에서 비교하기 위해 — 이중축 금지 원칙), 생산량과 사용량의
// 증감 방향이 반대로 움직인 날짜를 신호 마커로 강조한다.
const usageMetrics = [
  { key: "actual", label: "전력 사용량", unit: "MWh", color: "var(--chart-power)", icon: "⚡" },
  { key: "fuel", label: "연료 사용량", unit: "Nm³", color: "var(--chart-fuel)", icon: "🔥" },
  { key: "water", label: "용수 사용량", unit: "ton", color: "var(--chart-water)", icon: "💧" },
  { key: "wastewater", label: "폐수 사용량", unit: "ton", color: "var(--chart-wastewater)", icon: "🚿" },
] as const;

const PROD_COLOR = "var(--chart-production)";
const GOOD_COLOR = "var(--blue)";
const WARN_COLOR = "var(--red)";

function cleanNumber(value: unknown): number | null {
  const num = typeof value === "number" ? value : Number(value);
  return Number.isFinite(num) ? num : null;
}

// 7일 중앙값을 100으로 둔 지수 (legacy _median_index와 동일 규칙).
export function medianIndex(values: (number | null)[]): { index: (number | null)[]; base: number | null } {
  const nums = values.filter((v): v is number => v != null && Math.abs(v) > 1e-9).sort((a, b) => a - b);
  if (!nums.length) return { index: values.map(() => null), base: null };
  const mid = Math.floor(nums.length / 2);
  const base = nums.length % 2 ? nums[mid] : (nums[mid - 1] + nums[mid]) / 2;
  if (Math.abs(base) <= 1e-9) return { index: values.map(() => null), base: null };
  return { index: values.map(v => (v == null ? null : (v / base) * 100)), base };
}

// 7일 중앙값의 1% 이하 변동은 잔진동으로 보고 신호에서 제외 (legacy _trend_direction).
function direction(curr: number | null, prev: number | null, base: number | null): number {
  if (curr == null || prev == null || base == null) return 0;
  const diff = curr - prev;
  const threshold = Math.max(Math.abs(base) * 0.01, 1e-9);
  if (diff > threshold) return 1;
  if (diff < -threshold) return -1;
  return 0;
}

export function signalLabel(prodDir: number, usageDir: number): { label: string; tone: "good" | "warn" | null } {
  if (prodDir > 0 && usageDir < 0) return { label: "생산↑ 사용↓", tone: "good" };
  if (prodDir < 0 && usageDir > 0) return { label: "생산↓ 사용↑", tone: "warn" };
  if (prodDir > 0 && usageDir > 0) return { label: "동반 증가", tone: null };
  if (prodDir < 0 && usageDir < 0) return { label: "동반 감소", tone: null };
  if (prodDir === 0 && usageDir === 0) return { label: "변화 작음", tone: null };
  if (prodDir === 0) return { label: "생산 유지", tone: null };
  return { label: "사용 유지", tone: null };
}

const changeText = (value: number | null) => value == null ? "-" : `${value > 0 ? "+" : ""}${value.toFixed(1)}%`;
const numberText = (value: number | null, digits = 1) => value == null ? "-" : value.toLocaleString("ko-KR", { maximumFractionDigits: digits });
// 데이터 표 전치용 — 생산·사용량 지수는 합계가 아니라 기간 평균이 의미 있는 요약값이다.
const avgOf = (values: (number | null)[]) => {
  const nums = values.filter((value): value is number => value != null);
  return nums.length ? Math.round(nums.reduce((acc, value) => acc + value, 0) / nums.length) : null;
};

type QuadRow = {
  date: string;
  production: number | null;
  usage: number | null;
  prodIdx: number | null;
  usageIdx: number | null;
  prodChange: number | null;
  usageChange: number | null;
  signal: string;
  tone: "good" | "warn" | null;
  goodY: number | null;
  warnY: number | null;
};

function buildQuadRows(trend: AnyData[], usageKey: string): QuadRow[] {
  const prodValues = trend.map(row => cleanNumber(row.production));
  const usageValues = trend.map(row => cleanNumber(row[usageKey]));
  const prod = medianIndex(prodValues);
  const usage = medianIndex(usageValues);
  return trend.map((row, i) => {
    const prodDir = i === 0 ? 0 : direction(prodValues[i], prodValues[i - 1], prod.base);
    const usageDir = i === 0 ? 0 : direction(usageValues[i], usageValues[i - 1], usage.base);
    const { label, tone } = i === 0 ? { label: "기준", tone: null as null } : signalLabel(prodDir, usageDir);
    const prevProd = i === 0 ? null : prodValues[i - 1];
    const prevUsage = i === 0 ? null : usageValues[i - 1];
    const markerY = Math.max(prod.index[i] ?? 100, usage.index[i] ?? 100) + 7;
    return {
      date: String(row.date ?? ""),
      production: prodValues[i],
      usage: usageValues[i],
      prodIdx: prod.index[i],
      usageIdx: usage.index[i],
      prodChange: prevProd != null && prodValues[i] != null && Math.abs(prevProd) > 1e-9 ? ((prodValues[i]! - prevProd) / Math.abs(prevProd)) * 100 : null,
      usageChange: prevUsage != null && usageValues[i] != null && Math.abs(prevUsage) > 1e-9 ? ((usageValues[i]! - prevUsage) / Math.abs(prevUsage)) * 100 : null,
      signal: label,
      tone,
      goodY: tone === "good" ? markerY : null,
      warnY: tone === "warn" ? markerY : null,
    };
  });
}

function QuadTooltip({ active, payload, unit }: { active?: boolean; payload?: AnyData[]; unit: string }) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload as QuadRow;
  return <div className="quad-tooltip">
    <strong>{row.date}</strong>
    <span>생산량 지수 {numberText(row.prodIdx)} · 실제 {numberText(row.production)} ton · 전일 {changeText(row.prodChange)}</span>
    <span>사용량 지수 {numberText(row.usageIdx)} · 실제 {numberText(row.usage)} {unit} · 전일 {changeText(row.usageChange)}</span>
    {row.tone && <em className={row.tone}>{row.signal}</em>}
  </div>;
}

function Quadrant({ trend, metric }: { trend: AnyData[]; metric: (typeof usageMetrics)[number] }) {
  const rows = buildQuadRows(trend, metric.key);
  const warnDays = rows.filter(row => row.tone === "warn").length;
  const goodDays = rows.filter(row => row.tone === "good").length;
  const legend = useSeriesToggle();
  const pivotRows: PivotRow[] = [
    { key: "production", label: "생산량(ton)", values: rows.map(row => row.production), total: rows.reduce((acc, row) => acc + (row.production ?? 0), 0) },
    { key: "usage", label: `사용량(${metric.unit})`, values: rows.map(row => row.usage), total: rows.reduce((acc, row) => acc + (row.usage ?? 0), 0) },
    { key: "prodIdx", label: "생산 지수", values: rows.map(row => row.prodIdx), total: avgOf(rows.map(row => row.prodIdx)), format: value => value == null ? "-" : numberText(Number(value), 0) },
    { key: "usageIdx", label: "사용 지수", values: rows.map(row => row.usageIdx), total: avgOf(rows.map(row => row.usageIdx)), format: value => value == null ? "-" : numberText(Number(value), 0) },
    { key: "signal", label: "신호", values: rows.map(row => row.signal), total: null, format: (value, index) => {
      if (index === -1) return "-";
      const tone = rows[index]?.tone;
      return tone ? <i className={`quad-chip ${tone}`}>{String(value ?? "-")}</i> : String(value ?? "-");
    } },
  ];
  return <div className="quad-cell">
    <div className="quad-head" style={{ borderColor: metric.color }}>
      <span aria-hidden>{metric.icon}</span>
      <b>{metric.label}</b>
      <small>[{metric.unit}]</small>
      {goodDays > 0 && <i className="quad-chip good">생산↑ 사용↓ {goodDays}일</i>}
      {warnDays > 0 && <i className="quad-chip warn">생산↓ 사용↑ {warnDays}일</i>}
    </div>
    <div className="chart quad-chart">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={rows} margin={{ top: 18, right: 12, bottom: 0, left: -18 }}>
          <CartesianGrid vertical={false}/>
          <XAxis dataKey="date" tick={{ fontSize: 11 }}/>
          <YAxis tick={{ fontSize: 11 }} domain={["auto", "auto"]}/>
          <Tooltip content={<QuadTooltip unit={metric.unit}/>}/>
          {!legend.isHidden("prodIdx") && <Line type="linear" dataKey="prodIdx" name="생산량 지수" stroke={PROD_COLOR} strokeWidth={2} dot={{ r: 3, fill: PROD_COLOR, stroke: "var(--card)", strokeWidth: 2 }} activeDot={{ r: 5 }} connectNulls/>}
          {!legend.isHidden("usageIdx") && <Line type="linear" dataKey="usageIdx" name="사용량 지수" stroke={metric.color} strokeWidth={2} dot={{ r: 3, fill: metric.color, stroke: "var(--card)", strokeWidth: 2 }} activeDot={{ r: 5 }} connectNulls/>}
          {!legend.isHidden("goodY") && <Scatter dataKey="goodY" name="생산↑ 사용↓" fill={GOOD_COLOR} shape="diamond" legendType="diamond"/>}
          {!legend.isHidden("warnY") && <Scatter dataKey="warnY" name="생산↓ 사용↑" fill={WARN_COLOR} shape="diamond" legendType="diamond"/>}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
    <ToggleLegend items={[
      { key: "prodIdx", label: "생산량 지수", color: PROD_COLOR },
      { key: "usageIdx", label: "사용량 지수", color: metric.color },
      { key: "goodY", label: "생산↑ 사용↓", color: GOOD_COLOR },
      { key: "warnY", label: "생산↓ 사용↑", color: WARN_COLOR },
    ]} hidden={legend.hidden} onToggle={legend.toggle}/>
    <details className="quad-details">
      <summary>데이터 테이블</summary>
      <PivotTable periods={rows.map(row => row.date)} rows={pivotRows} totalLabel={`${rows.length}일 요약`}/>
    </details>
  </div>;
}

export function SevenDayCompare({ trend, factory, date }: { trend: AnyData[]; factory: string; date: string }) {
  if (!trend?.length) return null;
  const hasUsageBreakdown = trend.some(row => row.fuel != null || row.water != null || row.wastewater != null);
  if (!hasUsageBreakdown) return null;
  return <article className="card chart-card span-all">
    <header className="card-title">
      <h3>7일간 생산량 · 사용량 방향 비교</h3>
      <div className="card-title-side">
        <button type="button" className="csv-button" title="현재 공장·기준일 필터의 7일치 원시 데이터를 CSV로 내려받습니다"
          onClick={() => downloadCsv(`7day_direction_${factory}_${date}`, trend,
            ["date", "production", "actual", "fuel", "water", "wastewater"],
            { date: "일자", production: "생산량(ton)", actual: "전력(MWh)", fuel: "연료(Nm³)", water: "용수(ton)", wastewater: "폐수(ton)" })}>
          <Download size={13}/>CSV
        </button>
        <span>지수 = 7일 중앙값 100 기준</span>
      </div>
    </header>
    <p className="quad-caption">생산량과 각 에너지원 사용량의 증감 방향이 반대인 날을 강조합니다 — 생산↓ 사용↑(빨강)은 점검 대상, 생산↑ 사용↓(파랑)은 효율 개선 신호입니다.</p>
    <div className="quad-grid">
      {usageMetrics.map(metric => <Quadrant key={metric.key} trend={trend} metric={metric}/>)}
    </div>
  </article>;
}
