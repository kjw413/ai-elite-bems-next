"use client";

import { useEffect, useMemo, useState, type ComponentType } from "react";
import { Activity, CircleDollarSign, Factory, Flame, Gauge, RefreshCw, Zap } from "lucide-react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { apiGet, query } from "@/lib/bems-api";
import { DataToggle } from "@/components/data-toggle";
import { PivotTable } from "@/components/pivot-table";
import { ToggleLegend, useSeriesToggle, type LegendItem } from "@/components/toggle-legend";

type CostMetric = "power" | "fuel";
// 비용 화면의 조회 모드 — 사용량·원단위 화면(energyModes)과 같은 어휘를 쓴다.
// 기간별이 없는 이유: 비용은 월 단위로 마감·정산되어 임의 구간 합계에 실무 의미가 없다.
type CostMode = "month" | "year";
type NullableNumber = number | null;

type CostBridge = {
  previous: number;
  current: number;
  productionEffect: number;
  efficiencyEffect: number;
  priceEffect: number;
  tonPrev: number;
  tonCurr: number;
  intensityPrev: number;
  intensityCurr: number;
  pricePrev: number;
  priceCurr: number;
};

type CostPeriod = {
  cost: number;
  previousCost: number;
  costChange: NullableNumber;
  price: NullableNumber;
  previousPrice: NullableNumber;
  priceChange: NullableNumber;
  costPerTon: NullableNumber;
  previousCostPerTon: NullableNumber;
  coverage: NullableNumber;
  previousCoverage: NullableNumber;
  priceEffect: NullableNumber;
  bridge: CostBridge | null;
  bridgeNote: string | null;
  costChangeComparable: boolean;
  costChangeNote: string | null;
};

// 주차 집계 (월간 모드) — 월~일 7일을 채운 주만 담긴다.
// 비용은 합산, 단가는 Σ비용÷Σ사용량 가중평균이다.
type WeeklyCost = {
  week: string;
  span: string;
  days: number;
  cost: number;
  usage: number;
  price: NullableNumber;
  coverage: NullableNumber;
};

type MonthlyCost = {
  month: string;
  cost: NullableNumber;
  previousCost: NullableNumber;
  costChange: NullableNumber;
  price: NullableNumber;
  previousPrice: NullableNumber;
  priceChange: NullableNumber;
  costPerTon: NullableNumber;
  previousCostPerTon: NullableNumber;
  coverage: NullableNumber;
};

type CostMatrixRow = {
  factory: string;
  usage: number;
  cost: number;
  price: NullableNumber;
  previousPrice: NullableNumber;
  priceChange: NullableNumber;
  costPerTon: NullableNumber;
  coverage: NullableNumber;
};

type EnergyCostData = {
  baseDate: string;
  metric: CostMetric;
  label: string;
  priceUnit: string | null;
  usageUnit: string | null;
  year: number;
  dataStart: string;
  scopeNote: string;
  ytd: CostPeriod;
  mtd: CostPeriod;
  monthly: MonthlyCost[];
  composition: { metric: string; label: string; cost: number; change: NullableNumber; share: number }[];
  matrix: CostMatrixRow[];
  dailyPrice: { date: string; price: number; usage: number }[];
  weekly: WeeklyCost[];
  weeklyExcluded: string[];
  coverage: { expectedDays: number; presentDays: number; missingDays: number };
};

const metricDefs: { id: CostMetric; label: string }[] = [
  { id: "power", label: "전력" },
  { id: "fuel", label: "연료" },
];

const emptyPeriod = (): CostPeriod => ({
  cost: 0,
  previousCost: 0,
  costChange: null,
  price: null,
  previousPrice: null,
  priceChange: null,
  costPerTon: null,
  previousCostPerTon: null,
  coverage: null,
  previousCoverage: null,
  priceEffect: null,
  bridge: null,
  bridgeNote: null,
  costChangeComparable: true,
  costChangeNote: null,
});

const emptyData = (metric: CostMetric): EnergyCostData => ({
  baseDate: "",
  metric,
  label: metric === "power" ? "전력" : "연료",
  priceUnit: metric === "power" ? "원/kWh" : "원/Nm³",
  usageUnit: metric === "power" ? "kWh" : "Nm³",
  year: 0,
  dataStart: "",
  scopeNote: "",
  ytd: emptyPeriod(),
  mtd: emptyPeriod(),
  monthly: [],
  composition: [],
  matrix: [],
  dailyPrice: [],
  weekly: [],
  weeklyExcluded: [],
  coverage: { expectedDays: 0, presentDays: 0, missingDays: 0 },
});

const fmt = (value: unknown, digits = 1) =>
  typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("ko-KR", { maximumFractionDigits: digits })
    : "-";


const tooltipStyle = {
  contentStyle: {
    borderRadius: 10,
    border: "1px solid var(--line)",
    background: "var(--card)",
    boxShadow: "0 6px 18px #12201814",
    fontSize: 12,
  },
  labelStyle: { color: "var(--text)" },
};

function CostKpi({
  label,
  value,
  unit,
  change,
  note,
  icon: Icon,
}: {
  label: string;
  value: NullableNumber;
  unit: string;
  change?: NullableNumber;
  note?: string;
  icon: ComponentType<{ size?: number }>;
}) {
  return <article className="kpi card">
    <div className="kpi-icon"><Icon size={20}/></div>
    <div>
      <p>{label}</p>
      <strong>{fmt(value, unit.startsWith("원/") ? 2 : 1)} <small>{unit}</small></strong>
      {change != null && <span className={change <= 0 ? "good" : "bad"}>{change > 0 ? "+" : ""}{fmt(change)}% 전년비</span>}
      {note && <span className="kpi-note">{note}</span>}
    </div>
  </article>;
}

function CostBridgeView({ bridge }: { bridge: CostBridge }) {
  const steps = [
    { key: "production", label: "생산량 효과", value: bridge.productionEffect },
    { key: "efficiency", label: "효율 효과", value: bridge.efficiencyEffect },
    { key: "price", label: "단가 효과", value: bridge.priceEffect },
  ];
  const scale = Math.max(...steps.map(step => Math.abs(step.value)), 0.0001);
  return <>
    <div className="bridge cost-bridge">
      <div className="bridge-end"><span>전년 동기</span><b>{fmt(bridge.previous)}</b><small>백만원</small></div>
      <div className="bridge-steps">
        {steps.map(step => <div className="bridge-step" key={step.key}>
          <span>{step.label}</span>
          <i><em className={step.value >= 0 ? "up" : "down"} style={{ width: `${Math.abs(step.value) / scale * 100}%` }}/></i>
          <b className={step.value >= 0 ? "bad" : "good"}>{step.value >= 0 ? "+" : ""}{fmt(step.value)}</b>
        </div>)}
      </div>
      <div className="bridge-end"><span>금년</span><b>{fmt(bridge.current)}</b><small>백만원</small></div>
    </div>
    <div className="cost-bridge-basis">
      <span>생산량 {fmt(bridge.tonPrev)} → {fmt(bridge.tonCurr)} ton</span>
      <span>원단위 {fmt(bridge.intensityPrev, 2)} → {fmt(bridge.intensityCurr, 2)}</span>
      <span>단가 {fmt(bridge.pricePrev, 2)} → {fmt(bridge.priceCurr, 2)} 원</span>
    </div>
  </>;
}

export function EnergyCost({ factory, requestedDate }: { factory: string; requestedDate: string }) {
  const [mode, setMode] = useState<CostMode>("month");

  return <div className="energy-cost-screen">
    <div className="mode-row cost-toolbar">
      <div className="segmented" role="group" aria-label="비용 조회 모드">
        <button type="button" className={mode === "month" ? "active" : ""} aria-pressed={mode === "month"} onClick={() => setMode("month")}>월간</button>
        <button type="button" className={mode === "year" ? "active" : ""} aria-pressed={mode === "year"} onClick={() => setMode("year")}>연간</button>
      </div>
      <span className="cost-toolbar-label">전력·연료 비용 비교</span>
      <span className="period-chip">기준 {requestedDate}</span>
    </div>
    <section className="alert warning cost-scope-note"><CircleDollarSign size={19}/><div><strong>비용 범위 안내</strong><p>전력과 연료의 비용·단가를 각각 표시합니다. 용수·폐수 처리비는 시스템 관리 대상이 아닙니다.</p></div></section>
    <div className="cost-energy-grid">
      {metricDefs.map(item => <EnergyCostPanel key={`${item.id}-${factory}-${requestedDate}-${mode}`} metric={item.id} mode={mode} factory={factory} requestedDate={requestedDate}/>)}
    </div>
  </div>;
}

function EnergyCostPanel({ metric, mode, factory, requestedDate }: {
  metric: CostMetric;
  mode: CostMode;
  factory: string;
  requestedDate: string;
}) {
  const [data, setData] = useState<EnergyCostData>(() => emptyData(metric));
  const [loading, setLoading] = useState(true);
  const [live, setLive] = useState(false);
  const monthlyLegend = useSeriesToggle();
  const weeklyLegend = useSeriesToggle();

  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    setLoading(true);
    apiGet<EnergyCostData>(
      `/energy-cost?${query({ factory, date: requestedDate, metric, mode })}`,
      emptyData(metric),
      controller.signal,
    ).then(result => {
      if (!current) return;
      setData(result.data);
      setLive(result.live);
      setLoading(false);
    }).catch(() => {
      if (current) setLoading(false);
    });
    return () => {
      current = false;
      controller.abort();
    };
  }, [factory, requestedDate, metric, mode]);

  // 원인분해·KPI 기간은 조회 모드를 따른다 — 별도 YTD/MTD 토글을 두면 같은 화면의
  // 카드들이 서로 다른 기간을 보여 비교가 성립하지 않는다.
  const scope: "ytd" | "mtd" = mode === "year" ? "ytd" : "mtd";
  const scopeLabel = mode === "year" ? "연 누계" : "당월";
  const previousLabel = mode === "year" ? "전년 동기" : "전년 동월";
  const period = data[scope];
  const label = metric === "power" ? "전력" : "연료";
  const Icon = metric === "power" ? Zap : Flame;
  const costColor = metric === "power" ? "var(--chart-power)" : "var(--chart-fuel)";
  const priceColor = "var(--chart-actual)";
  const secondaryKey = "price";
  const previousSecondaryKey = "previousPrice";
  const secondaryLabel = `${label} 단가`;
  const secondaryUnit = data.priceUnit ?? "원";
  const monthly = useMemo(
    () => data.monthly.filter(row => row.cost != null || row.previousCost != null),
    [data.monthly],
  );
  const monthlyLegendItems = useMemo<LegendItem[]>(() => [
    { key: "previousCost", label: "전년 비용", color: "var(--chart-previous)" },
    { key: "cost", label: "금년 비용", color: costColor },
    { key: previousSecondaryKey, label: `전년 ${secondaryLabel}`, color: "var(--chart-target)" },
    { key: secondaryKey, label: `금년 ${secondaryLabel}`, color: priceColor },
  ], [secondaryLabel, costColor, priceColor]);
  const coverageNote = period.coverage != null && period.coverage < 0.99
    ? `비용 반영률 ${(period.coverage * 100).toFixed(1)}%`
    : undefined;
  const weekly = data.weekly ?? [];
  const weeklyExcluded = data.weeklyExcluded ?? [];
  const weeklyCostTotal = weekly.reduce((acc, row) => acc + (row.cost ?? 0), 0);
  const weeklyUsageTotal = weekly.reduce((acc, row) => acc + (row.usage ?? 0), 0);
  const weeklyLegendItems: LegendItem[] = [
    { key: "cost", label: "주 비용", color: costColor },
    { key: "price", label: secondaryLabel, color: priceColor },
  ];

  return <section className={`cost-energy-panel cost-energy-${metric}`} aria-labelledby={`cost-${metric}-title`} aria-busy={loading}>
    <header className="cost-energy-header">
      <div className="cost-energy-icon"><Icon size={22}/></div>
      <div><h2 id={`cost-${metric}-title`}>{label}</h2><p>{metric === "power" ? "전력비와 kWh당 단가" : "연료비와 Nm³당 단가"}</p></div>
      <span>{scopeLabel} 분석</span>
    </header>
    {loading ? <div className="loading inline-loading" role="status"><RefreshCw className="spin"/>{label} 비용 데이터를 불러오는 중입니다.</div>
      : !live ? <div className="data-warning" role="alert"><CircleDollarSign size={20}/><div><strong>{label} 비용 API 연결 실패</strong><p>비용·단가는 예시값으로 대체하지 않습니다. API와 DB 연결을 확인하세요.</p></div></div>
      : <div className="cost-panel-content">
        {data.baseDate && data.baseDate !== requestedDate && <p className="cost-note">실적 기준 {data.baseDate}</p>}
        <div className="kpi-grid cost-kpis">
          <CostKpi label={`${scopeLabel} 비용`} value={period.cost} unit="백만원" change={period.costChange} note={coverageNote} icon={CircleDollarSign}/>
          <CostKpi label={`${previousLabel} 비용`} value={period.previousCost} unit="백만원" icon={CircleDollarSign}/>
          <CostKpi label={`${scopeLabel} ${secondaryLabel}`} value={period.price} unit={secondaryUnit} change={period.priceChange} icon={Gauge}/>
          <CostKpi label={`${scopeLabel} 단가 효과`} value={period.priceEffect} unit="백만원" note="단가가 전년 그대로였다면 달라졌을 금액" icon={Activity}/>
        </div>

        <div className="cost-panel-details">
          {mode === "year" && <article className="card chart-card span-all">
            <header className="card-title"><h3>월별 비용·{secondaryLabel}</h3><div className="card-title-side"><span>{data.year}년 · 비용 백만원 / {secondaryUnit}</span></div></header>
            <div className="chart cost-period-chart"><ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={monthly}>
                <CartesianGrid vertical={false}/><XAxis dataKey="month" tick={{ fontSize: 10 }} minTickGap={8}/><YAxis yAxisId="cost" width={46}/><YAxis yAxisId="secondary" orientation="right" width={48}/>
                <Tooltip {...tooltipStyle} formatter={(value: unknown, name: unknown) => [fmt(value, 2), String(name ?? "")]}/>
                {!monthlyLegend.isHidden("previousCost") && <Bar yAxisId="cost" dataKey="previousCost" name="전년 비용(백만원)" fill="var(--chart-previous)" opacity={0.45} radius={[3,3,0,0]} maxBarSize={22}/>}
                {!monthlyLegend.isHidden("cost") && <Bar yAxisId="cost" dataKey="cost" name="금년 비용(백만원)" fill={costColor} radius={[3,3,0,0]} maxBarSize={22}/>}
                {!monthlyLegend.isHidden(previousSecondaryKey) && <Line yAxisId="secondary" type="linear" dataKey={previousSecondaryKey} name={`전년 ${secondaryLabel}(${secondaryUnit})`} stroke="var(--chart-target)" strokeWidth={2} strokeDasharray="4 3" dot={false} connectNulls={false}/>}
                {!monthlyLegend.isHidden(secondaryKey) && <Line yAxisId="secondary" type="linear" dataKey={secondaryKey} name={`금년 ${secondaryLabel}(${secondaryUnit})`} stroke={priceColor} strokeWidth={2} dot={{ r: 3, fill: priceColor, stroke: "var(--card)", strokeWidth: 2 }} connectNulls={false}/>}
              </ComposedChart>
            </ResponsiveContainer></div>
            <ToggleLegend items={monthlyLegendItems} hidden={monthlyLegend.hidden} onToggle={monthlyLegend.toggle}/>
            <DataToggle><PivotTable periods={monthly.map(row => row.month)} periodLabel="월" totalLabel="YTD 누계" rows={[
              { key: "previousCost", label: "전년 비용(백만원)", values: monthly.map(row => row.previousCost), total: data.ytd.previousCost, format: value => value == null ? "-" : fmt(Number(value), 1) },
              { key: "cost", label: "금년 비용(백만원)", values: monthly.map(row => row.cost), total: data.ytd.cost, format: value => value == null ? "-" : fmt(Number(value), 1) },
              { key: previousSecondaryKey, label: `전년 ${secondaryLabel}(${secondaryUnit})`, values: monthly.map(row => row[previousSecondaryKey]), total: data.ytd.previousPrice, format: value => value == null ? "-" : fmt(Number(value), 2) },
              { key: secondaryKey, label: `금년 ${secondaryLabel}(${secondaryUnit})`, values: monthly.map(row => row[secondaryKey]), total: data.ytd.price, format: value => value == null ? "-" : fmt(Number(value), 2) },
            ]}/></DataToggle>
          </article>}

          {mode === "month" && <article className="card chart-card">
            <header className="card-title"><h3>주간 비용 집계</h3><div className="card-title-side"><span>월~일 7일 · 백만원 / {data.priceUnit}</span></div></header>
            {weekly.length > 0 ? <><div className="chart cost-period-chart"><ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={weekly}>
                <CartesianGrid vertical={false}/><XAxis dataKey="week" tick={{ fontSize: 10 }}/><YAxis yAxisId="cost" width={46}/><YAxis yAxisId="price" orientation="right" width={48} domain={["auto","auto"]}/>
                <Tooltip {...tooltipStyle} formatter={(value: unknown, name: unknown) => [fmt(value, 2), String(name ?? "")]}/>
                {!weeklyLegend.isHidden("cost") && <Bar yAxisId="cost" dataKey="cost" name="주 비용(백만원)" fill={costColor} radius={[3,3,0,0]} maxBarSize={34}/>}
                {!weeklyLegend.isHidden("price") && <Line yAxisId="price" type="linear" dataKey="price" name={`${data.label} 단가(${data.priceUnit})`} stroke={priceColor} strokeWidth={2} dot={{ r: 3, fill: priceColor, stroke: "var(--card)", strokeWidth: 2 }} connectNulls/>}
              </ComposedChart>
            </ResponsiveContainer></div>
            <ToggleLegend items={weeklyLegendItems} hidden={weeklyLegend.hidden} onToggle={weeklyLegend.toggle}/>
            <p className="cost-note">월~일 7일을 채운 주만 표시합니다{weeklyExcluded.length > 0 ? ` — ${weeklyExcluded.join(" · ")}은(는) 주가 완결되지 않아 제외했습니다` : ""}. 주 단가는 그 주의 Σ비용÷Σ사용량 가중평균입니다 — 일별 단가를 산술평균하면 저부하일이 과대 반영됩니다.</p>
            <DataToggle><PivotTable periods={weekly.map(row => row.week)} periodLabel="주차" totalLabel="표시 주 합계" rows={[
              { key: "cost", label: "비용(백만원)", values: weekly.map(row => row.cost), total: Math.round(weeklyCostTotal * 100) / 100, format: value => value == null ? "-" : fmt(Number(value), 2) },
              { key: "usage", label: `사용량(${data.usageUnit})`, values: weekly.map(row => row.usage), total: Math.round(weeklyUsageTotal * 10) / 10 },
              { key: "price", label: `단가(${data.priceUnit})`, values: weekly.map(row => row.price), total: weeklyUsageTotal > 0 ? Math.round(weeklyCostTotal * 1_000_000 / weeklyUsageTotal * 100) / 100 : null, format: (value: unknown) => value == null ? "-" : fmt(Number(value), 2) },
              { key: "days", label: "실적일수", values: weekly.map(row => row.days), total: weekly.reduce((acc, row) => acc + (row.days ?? 0), 0) },
            ]}/></DataToggle></> : <div className="cost-empty cost-period-chart"><Factory size={24}/><p>선택한 월에 월~일 7일을 채운 주간 실적이 없습니다. 당월 누적 비용과 일별 단가를 확인하세요.</p></div>}
          </article>}

          {period.costChangeNote && <section className="alert warning cost-scope-note"><Activity size={19}/><div><strong>전년비 비교 주의</strong><p>{period.costChangeNote}</p></div></section>}
          <article className="card chart-card">
            <header className="card-title"><h3>{scopeLabel} 비용 증감 원인</h3><div className="card-title-side"><span>생산량 · 효율 · 단가</span></div></header>
            {period.bridge ? <CostBridgeView bridge={period.bridge}/> : <div className="cost-empty"><Factory size={24}/><p>{period.bridgeNote ?? "비교 가능한 전년 실적이 없어 원인분해를 표시하지 않습니다."}</p></div>}
            {period.bridgeNote && period.bridge && <p className="cost-note">{period.bridgeNote}</p>}
            {coverageNote && <p className="cost-note warning">{coverageNote} — 비용이 없는 사용량은 원인분해에서 제외됩니다.</p>}
          </article>

          <article className="card table-card span-all">
            <header className="card-title"><h3>공장별 비용·단가 매트릭스</h3><div className="card-title-side"><span>{scopeLabel}</span></div></header>
            <div className="table-wrap"><table className="cost-matrix"><thead><tr><th>공장</th><th>비용(백만원)</th><th>사용량({data.usageUnit})</th><th>{secondaryLabel}({secondaryUnit})</th><th>단가 전년비</th><th>비용 반영률</th></tr></thead><tbody>
              {data.matrix.map(row => <tr key={row.factory}><td>{row.factory}</td><td>{fmt(row.cost)}</td><td>{fmt(row.usage)}</td><td>{fmt(row.price, 2)}</td><td className={row.priceChange != null && row.priceChange <= 0 ? "good" : "bad"}>{row.priceChange == null ? "-" : `${row.priceChange > 0 ? "+" : ""}${fmt(row.priceChange)}%`}</td><td>{row.coverage == null ? "-" : `${(row.coverage * 100).toFixed(1)}%`}</td></tr>)}
            </tbody></table></div>
          </article>

          {mode === "month" && <article className="card chart-card span-all">
            <header className="card-title"><h3>당월 일별 {data.label} 단가</h3><div className="card-title-side"><span>{data.priceUnit}</span></div></header>
            {data.dailyPrice.length > 0 ? <div className="chart"><ResponsiveContainer width="100%" height="100%"><ComposedChart data={data.dailyPrice}><CartesianGrid vertical={false}/><XAxis dataKey="date" interval="preserveStartEnd" minTickGap={22}/><YAxis domain={["auto","auto"]}/><Tooltip {...tooltipStyle} formatter={(value: unknown) => [fmt(value, 2), data.priceUnit ?? "단가"]}/><Line type="linear" dataKey="price" name={`${data.label} 단가`} stroke={costColor} strokeWidth={2} dot={false} activeDot={{ r: 4 }} connectNulls={false}/></ComposedChart></ResponsiveContainer></div> : <div className="cost-empty"><Gauge size={24}/><p>선택한 월의 일별 단가 실적이 없습니다.</p></div>}
          </article>}
        </div>
      </div>}
  </section>;
}
