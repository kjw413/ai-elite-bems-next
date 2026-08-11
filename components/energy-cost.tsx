"use client";

import { useEffect, useMemo, useState, type ComponentType } from "react";
import { Activity, CircleDollarSign, Factory, Gauge, RefreshCw } from "lucide-react";
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

type CostMetric = "total" | "power" | "fuel";
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
  { id: "total", label: "전력+연료" },
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
  label: metric === "power" ? "전력" : metric === "fuel" ? "연료" : "합계",
  priceUnit: metric === "power" ? "원/kWh" : metric === "fuel" ? "원/Nm³" : null,
  usageUnit: metric === "power" ? "kWh" : metric === "fuel" ? "Nm³" : null,
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
const toThousandWonPerTon = (value: NullableNumber) => value == null ? null : value / 1000;


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
  const [metric, setMetric] = useState<CostMetric>("total");
  const [mode, setMode] = useState<CostMode>("month");
  const [data, setData] = useState<EnergyCostData>(() => emptyData("total"));
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
  const isTotal = metric === "total";
  const secondaryKey = isTotal ? "costPerTon" : "price";
  const previousSecondaryKey = isTotal ? "previousCostPerTon" : "previousPrice";
  const secondaryLabel = isTotal ? "톤당 비용" : `${data.label} 단가`;
  const secondaryUnit = isTotal ? "천원/ton" : data.priceUnit ?? "원";
  const monthly = useMemo(
    () => data.monthly
      .filter(row => row.cost != null || row.previousCost != null)
      .map(row => isTotal ? {
        ...row,
        costPerTon: toThousandWonPerTon(row.costPerTon),
        previousCostPerTon: toThousandWonPerTon(row.previousCostPerTon),
      } : row),
    [data.monthly, isTotal],
  );
  const monthlyLegendItems = useMemo<LegendItem[]>(() => [
    { key: "previousCost", label: "전년 비용", color: "var(--chart-previous)" },
    { key: "cost", label: "금년 비용", color: "var(--chart-power)" },
    { key: previousSecondaryKey, label: `전년 ${secondaryLabel}`, color: "var(--chart-target)" },
    { key: secondaryKey, label: `금년 ${secondaryLabel}`, color: "var(--chart-fuel)" },
  ], [previousSecondaryKey, secondaryKey, secondaryLabel]);
  const coverageNote = period.coverage != null && period.coverage < 0.99
    ? `비용 반영률 ${(period.coverage * 100).toFixed(1)}%`
    : undefined;
  const weekly = data.weekly ?? [];
  const weeklyExcluded = data.weeklyExcluded ?? [];
  const weeklyCostTotal = weekly.reduce((acc, row) => acc + (row.cost ?? 0), 0);
  const weeklyUsageTotal = weekly.reduce((acc, row) => acc + (row.usage ?? 0), 0);
  const weeklyLegendItems: LegendItem[] = [
    { key: "cost", label: "주 비용", color: "var(--chart-power)" },
    ...(isTotal ? [] : [{ key: "price", label: `${data.label} 단가`, color: "var(--chart-fuel)" }]),
  ];

  if (loading) return <div className="loading inline-loading" role="status"><RefreshCw className="spin"/>비용 데이터를 불러오는 중입니다.</div>;
  if (!live) return <section className="data-warning" role="alert"><CircleDollarSign size={20}/><div><strong>비용 API 연결 실패</strong><p>비용·단가는 예시값으로 대체하지 않습니다. API와 DB 연결을 확인하세요.</p></div></section>;

  return <div className="energy-cost-screen">
    <div className="mode-row cost-toolbar">
      <div className="segmented" role="group" aria-label="비용 에너지원 선택">
        {metricDefs.map(item => <button type="button" key={item.id} className={metric === item.id ? "active" : ""} aria-pressed={metric === item.id} onClick={() => setMetric(item.id)}>{item.label}</button>)}
      </div>
      <div className="segmented" role="group" aria-label="비용 조회 모드">
        <button type="button" className={mode === "month" ? "active" : ""} aria-pressed={mode === "month"} onClick={() => setMode("month")}>월간</button>
        <button type="button" className={mode === "year" ? "active" : ""} aria-pressed={mode === "year"} onClick={() => setMode("year")}>연간</button>
      </div>
      <span className="period-chip">기준 {data.baseDate}</span>
    </div>

    {data.scopeNote && <section className="alert warning cost-scope-note"><CircleDollarSign size={19}/><div><strong>비용 범위 안내</strong><p>{data.scopeNote}</p></div></section>}
    {period.costChangeNote && <section className="alert warning cost-scope-note"><Activity size={19}/><div><strong>전년비 비교 주의</strong><p>{period.costChangeNote}</p></div></section>}

    <section className="kpi-grid">
      <CostKpi label={`${scopeLabel} 비용`} value={period.cost} unit="백만원" change={period.costChange} note={coverageNote} icon={CircleDollarSign}/>
      <CostKpi label={`${previousLabel} 비용`} value={period.previousCost} unit="백만원" icon={CircleDollarSign}/>
      <CostKpi label={`${scopeLabel} ${secondaryLabel}`} value={isTotal ? toThousandWonPerTon(period.costPerTon) : period.price} unit={secondaryUnit} change={isTotal ? null : period.priceChange} icon={Gauge}/>
      {/* 4번째 타일은 지표에 따라 갈린다 — 단가 효과는 전력·연료에서만 계산되므로(전력+연료 합은
          kWh 와 Nm³ 를 더할 수 없어 단가가 없다) 합계에서는 빈 타일 대신 전년 톤당 비용을 둔다. */}
      {isTotal
        ? <CostKpi label={`${previousLabel} ${secondaryLabel}`} value={toThousandWonPerTon(period.previousCostPerTon)} unit={secondaryUnit} icon={Gauge}/>
        : <CostKpi label={`${scopeLabel} 단가 효과`} value={period.priceEffect} unit="백만원" note="단가가 전년 그대로였다면 달라졌을 금액" icon={Activity}/>}
    </section>

    <section className="content-grid">
      {mode === "year" && <article className="card chart-card span-all">
        <header className="card-title"><h3>월별 비용·{secondaryLabel}</h3><div className="card-title-side"><span>{data.year}년 · 비용 백만원 / {secondaryUnit}</span></div></header>
        <div className="chart cost-monthly-chart"><ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={monthly}>
            <CartesianGrid vertical={false}/><XAxis dataKey="month"/><YAxis yAxisId="cost"/><YAxis yAxisId="secondary" orientation="right"/>
            <Tooltip {...tooltipStyle} formatter={(value: unknown, name: unknown) => [fmt(value, 2), String(name ?? "")]}/>
            {!monthlyLegend.isHidden("previousCost") && <Bar yAxisId="cost" dataKey="previousCost" name="전년 비용(백만원)" fill="var(--chart-previous)" opacity={0.45} radius={[3,3,0,0]} maxBarSize={22}/>}
            {!monthlyLegend.isHidden("cost") && <Bar yAxisId="cost" dataKey="cost" name="금년 비용(백만원)" fill="var(--chart-power)" radius={[3,3,0,0]} maxBarSize={22}/>}
            {!monthlyLegend.isHidden(previousSecondaryKey) && <Line yAxisId="secondary" type="linear" dataKey={previousSecondaryKey} name={`전년 ${secondaryLabel}(${secondaryUnit})`} stroke="var(--chart-target)" strokeWidth={2} strokeDasharray="4 3" dot={false} connectNulls={false}/>}
            {!monthlyLegend.isHidden(secondaryKey) && <Line yAxisId="secondary" type="linear" dataKey={secondaryKey} name={`금년 ${secondaryLabel}(${secondaryUnit})`} stroke="var(--chart-fuel)" strokeWidth={2} dot={{ r: 3, fill: "var(--chart-fuel)", stroke: "var(--card)", strokeWidth: 2 }} connectNulls={false}/>}
          </ComposedChart>
        </ResponsiveContainer></div>
        <ToggleLegend items={monthlyLegendItems} hidden={monthlyLegend.hidden} onToggle={monthlyLegend.toggle}/>
        <DataToggle><PivotTable periods={monthly.map(row => row.month)} periodLabel="월" totalLabel="YTD 누계" rows={[
          { key: "previousCost", label: "전년 비용(백만원)", values: monthly.map(row => row.previousCost), total: data.ytd.previousCost, format: value => value == null ? "-" : fmt(Number(value), 1) },
          { key: "cost", label: "금년 비용(백만원)", values: monthly.map(row => row.cost), total: data.ytd.cost, format: value => value == null ? "-" : fmt(Number(value), 1) },
          { key: previousSecondaryKey, label: `전년 ${secondaryLabel}(${secondaryUnit})`, values: monthly.map(row => row[previousSecondaryKey]), total: isTotal ? toThousandWonPerTon(data.ytd.previousCostPerTon) : data.ytd.previousPrice, format: value => value == null ? "-" : fmt(Number(value), 2) },
          { key: secondaryKey, label: `금년 ${secondaryLabel}(${secondaryUnit})`, values: monthly.map(row => row[secondaryKey]), total: isTotal ? toThousandWonPerTon(data.ytd.costPerTon) : data.ytd.price, format: value => value == null ? "-" : fmt(Number(value), 2) },
        ]}/></DataToggle>
      </article>}

      {/* 폭이 적게 필요한 카드는 한 띠에 나란히 — 주간 집계·원인분해·비용 구성이 각자 전폭을
          쓰면 화면의 절반이 빈 채로 세로만 길어진다. */}
      <div className="aux-grid span-all">
        {mode === "month" && weekly.length > 0 && <article className="card chart-card">
          <header className="card-title"><h3>주간 비용 집계</h3><div className="card-title-side"><span>월~일 7일 · 백만원{isTotal ? "" : ` / ${data.priceUnit}`}</span></div></header>
          <div className="chart aux-chart"><ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={weekly}>
              <CartesianGrid vertical={false}/><XAxis dataKey="week" tick={{ fontSize: 10 }}/><YAxis yAxisId="cost"/>{!isTotal && <YAxis yAxisId="price" orientation="right" domain={["auto","auto"]}/>}
              <Tooltip {...tooltipStyle} formatter={(value: unknown, name: unknown) => [fmt(value, 2), String(name ?? "")]}/>
              {!weeklyLegend.isHidden("cost") && <Bar yAxisId="cost" dataKey="cost" name="주 비용(백만원)" fill="var(--chart-power)" radius={[3,3,0,0]} maxBarSize={34}/>}
              {!isTotal && !weeklyLegend.isHidden("price") && <Line yAxisId="price" type="linear" dataKey="price" name={`${data.label} 단가(${data.priceUnit})`} stroke="var(--chart-fuel)" strokeWidth={2} dot={{ r: 3, fill: "var(--chart-fuel)", stroke: "var(--card)", strokeWidth: 2 }} connectNulls/>}
            </ComposedChart>
          </ResponsiveContainer></div>
          <ToggleLegend items={weeklyLegendItems} hidden={weeklyLegend.hidden} onToggle={weeklyLegend.toggle}/>
          <p className="cost-note">월~일 7일을 채운 주만 표시합니다{weeklyExcluded.length > 0 ? ` — ${weeklyExcluded.join(" · ")}은(는) 주가 완결되지 않아 제외했습니다` : ""}. 주 단가는 그 주의 Σ비용÷Σ사용량 가중평균입니다 — 일별 단가를 산술평균하면 저부하일이 과대 반영됩니다.</p>
          <DataToggle><PivotTable periods={weekly.map(row => row.week)} periodLabel="주차" totalLabel="표시 주 합계" rows={[
            { key: "cost", label: "비용(백만원)", values: weekly.map(row => row.cost), total: Math.round(weeklyCostTotal * 100) / 100, format: value => value == null ? "-" : fmt(Number(value), 2) },
            ...(isTotal ? [] : [
              { key: "usage", label: `사용량(${data.usageUnit})`, values: weekly.map(row => row.usage), total: Math.round(weeklyUsageTotal * 10) / 10 },
              { key: "price", label: `단가(${data.priceUnit})`, values: weekly.map(row => row.price), total: weeklyUsageTotal > 0 ? Math.round(weeklyCostTotal * 1_000_000 / weeklyUsageTotal * 100) / 100 : null, format: (value: unknown) => value == null ? "-" : fmt(Number(value), 2) },
            ]),
            { key: "days", label: "실적일수", values: weekly.map(row => row.days), total: weekly.reduce((acc, row) => acc + (row.days ?? 0), 0) },
          ]}/></DataToggle>
        </article>}

        <article className="card chart-card">
          <header className="card-title"><h3>{scopeLabel} 비용 증감 원인</h3><div className="card-title-side"><span>생산량 · 효율 · 단가</span></div></header>
          {period.bridge ? <CostBridgeView bridge={period.bridge}/> : <div className="cost-empty"><Factory size={24}/><p>{isTotal ? "전력 또는 연료를 선택하면 3요인 원인분해를 볼 수 있습니다." : period.bridgeNote ?? "비교 가능한 전년 실적이 없어 원인분해를 표시하지 않습니다."}</p></div>}
          {period.bridgeNote && period.bridge && <p className="cost-note">{period.bridgeNote}</p>}
          {coverageNote && <p className="cost-note warning">{coverageNote} — 비용이 없는 사용량은 원인분해에서 제외됩니다.</p>}
        </article>

        {mode === "year" && <article className="card list">
          <header className="card-title"><h3>에너지원별 비용 구성</h3><div className="card-title-side"><span>YTD · 백만원</span></div></header>
          <div className="cost-composition">
            {data.composition.map(row => <div key={row.metric}>
              <p><span>{row.label}</span><b>{fmt(row.cost)} <small>({fmt(row.share)}%)</small></b></p>
              <i><em className={row.metric} style={{ width: `${Math.max(0, Math.min(100, row.share))}%` }}/></i>
              <small className={row.change != null && row.change > 0 ? "bad" : "good"}>{row.change == null ? "전년비 -" : `전년비 ${row.change > 0 ? "+" : ""}${fmt(row.change)}%`}</small>
            </div>)}
          </div>
        </article>}
      </div>

      <article className="card table-card span-all">
        <header className="card-title"><h3>공장별 비용·단가 매트릭스</h3><div className="card-title-side"><span>{scopeLabel}</span></div></header>
        <div className="table-wrap"><table className="cost-matrix"><thead><tr><th>공장</th><th>비용(백만원)</th>{!isTotal && <th>사용량({data.usageUnit})</th>}<th>{secondaryLabel}({secondaryUnit})</th>{!isTotal && <th>단가 전년비</th>}<th>비용 반영률</th></tr></thead><tbody>
          {data.matrix.map(row => <tr key={row.factory}><td>{row.factory}</td><td>{fmt(row.cost)}</td>{!isTotal && <td>{fmt(row.usage)}</td>}<td>{fmt(isTotal ? toThousandWonPerTon(row.costPerTon) : row.price, 2)}</td>{!isTotal && <td className={row.priceChange != null && row.priceChange <= 0 ? "good" : "bad"}>{row.priceChange == null ? "-" : `${row.priceChange > 0 ? "+" : ""}${fmt(row.priceChange)}%`}</td>}<td>{row.coverage == null ? "-" : `${(row.coverage * 100).toFixed(1)}%`}</td></tr>)}
        </tbody></table></div>
      </article>

      {!isTotal && mode === "month" && data.dailyPrice.length > 0 && <article className="card chart-card span-all">
        <header className="card-title"><h3>당월 일별 {data.label} 단가</h3><div className="card-title-side"><span>{data.priceUnit}</span></div></header>
        <div className="chart"><ResponsiveContainer width="100%" height="100%"><ComposedChart data={data.dailyPrice}><CartesianGrid vertical={false}/><XAxis dataKey="date" interval="preserveStartEnd" minTickGap={22}/><YAxis domain={["auto","auto"]}/><Tooltip {...tooltipStyle} formatter={(value: unknown) => [fmt(value, 2), data.priceUnit ?? "단가"]}/><Line type="linear" dataKey="price" name={`${data.label} 단가`} stroke={metric === "power" ? "var(--chart-power)" : "var(--chart-fuel)"} strokeWidth={2} dot={false} activeDot={{ r: 4 }} connectNulls={false}/></ComposedChart></ResponsiveContainer></div>
      </article>}
    </section>
  </div>;
}
