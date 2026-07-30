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

type CostMetric = "total" | "power" | "fuel";
type CostScope = "ytd" | "mtd";
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
  coverage: NullableNumber;
  previousCoverage: NullableNumber;
  bridge: CostBridge | null;
  bridgeNote: string | null;
  costChangeComparable: boolean;
  costChangeNote: string | null;
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
  coverage: null,
  previousCoverage: null,
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
  const [metric, setMetric] = useState<CostMetric>("total");
  const [scope, setScope] = useState<CostScope>("ytd");
  const [data, setData] = useState<EnergyCostData>(() => emptyData("total"));
  const [loading, setLoading] = useState(true);
  const [live, setLive] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    setLoading(true);
    apiGet<EnergyCostData>(
      `/energy-cost?${query({ factory, date: requestedDate, metric })}`,
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
  }, [factory, requestedDate, metric]);

  const period = data[scope];
  const monthly = useMemo(
    () => data.monthly.filter(row => row.cost != null || row.previousCost != null),
    [data.monthly],
  );
  const isTotal = metric === "total";
  const secondaryKey = isTotal ? "costPerTon" : "price";
  const previousSecondaryKey = isTotal ? "previousCostPerTon" : "previousPrice";
  const secondaryLabel = isTotal ? "톤당 비용" : `${data.label} 단가`;
  const secondaryUnit = isTotal ? "원/ton" : data.priceUnit ?? "원";
  const coverageNote = period.coverage != null && period.coverage < 0.99
    ? `비용 반영률 ${(period.coverage * 100).toFixed(1)}%`
    : undefined;

  if (loading) return <div className="loading inline-loading" role="status"><RefreshCw className="spin"/>비용 데이터를 불러오는 중입니다.</div>;
  if (!live) return <section className="data-warning" role="alert"><CircleDollarSign size={20}/><div><strong>비용 API 연결 실패</strong><p>비용·단가는 예시값으로 대체하지 않습니다. API와 DB 연결을 확인하세요.</p></div></section>;

  return <div className="energy-cost-screen">
    <div className="mode-row cost-toolbar">
      <div className="segmented" role="group" aria-label="비용 에너지원 선택">
        {metricDefs.map(item => <button type="button" key={item.id} className={metric === item.id ? "active" : ""} aria-pressed={metric === item.id} onClick={() => setMetric(item.id)}>{item.label}</button>)}
      </div>
      <div className="segmented" role="group" aria-label="비용 원인분해 기간">
        <button type="button" className={scope === "ytd" ? "active" : ""} aria-pressed={scope === "ytd"} onClick={() => setScope("ytd")}>연 누계 YTD</button>
        <button type="button" className={scope === "mtd" ? "active" : ""} aria-pressed={scope === "mtd"} onClick={() => setScope("mtd")}>당월 MTD</button>
      </div>
      <span className="period-chip">기준 {data.baseDate}</span>
    </div>

    {data.scopeNote && <section className="alert warning cost-scope-note"><CircleDollarSign size={19}/><div><strong>비용 범위 안내</strong><p>{data.scopeNote}</p></div></section>}
    {period.costChangeNote && <section className="alert warning cost-scope-note"><Activity size={19}/><div><strong>전년비 비교 주의</strong><p>{period.costChangeNote}</p></div></section>}

    <section className="kpi-grid">
      <CostKpi label="연 누계 비용" value={data.ytd.cost} unit="백만원" change={data.ytd.costChange} note={data.ytd.coverage != null && data.ytd.coverage < 0.99 ? `비용 반영률 ${(data.ytd.coverage * 100).toFixed(1)}%` : undefined} icon={CircleDollarSign}/>
      <CostKpi label="당월 비용" value={data.mtd.cost} unit="백만원" change={data.mtd.costChange} icon={CircleDollarSign}/>
      <CostKpi label={`연 누계 ${secondaryLabel}`} value={isTotal ? data.ytd.costPerTon : data.ytd.price} unit={secondaryUnit} change={isTotal ? null : data.ytd.priceChange} icon={Gauge}/>
      <CostKpi label={`당월 ${secondaryLabel}`} value={isTotal ? data.mtd.costPerTon : data.mtd.price} unit={secondaryUnit} change={isTotal ? null : data.mtd.priceChange} icon={Gauge}/>
    </section>

    <section className="content-grid">
      <article className="card chart-card span-all">
        <header className="card-title"><h3>월별 비용·{secondaryLabel}</h3><div className="card-title-side"><span>{data.year}년 · 비용 백만원 / {secondaryUnit}</span></div></header>
        <div className="chart cost-monthly-chart"><ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={monthly}>
            <CartesianGrid vertical={false}/><XAxis dataKey="month"/><YAxis yAxisId="cost"/><YAxis yAxisId="secondary" orientation="right"/>
            <Tooltip {...tooltipStyle} formatter={(value: unknown, name: unknown) => [fmt(value, 2), String(name ?? "")]}/>
            <Bar yAxisId="cost" dataKey="previousCost" name="전년 비용(백만원)" fill="var(--chart-previous)" opacity={0.45} radius={[3,3,0,0]} maxBarSize={22}/>
            <Bar yAxisId="cost" dataKey="cost" name="금년 비용(백만원)" fill="var(--chart-power)" radius={[3,3,0,0]} maxBarSize={22}/>
            <Line yAxisId="secondary" type="linear" dataKey={previousSecondaryKey} name={`전년 ${secondaryLabel}(${secondaryUnit})`} stroke="var(--chart-previous)" strokeWidth={2} strokeDasharray="4 3" dot={false} connectNulls={false}/>
            <Line yAxisId="secondary" type="linear" dataKey={secondaryKey} name={`금년 ${secondaryLabel}(${secondaryUnit})`} stroke="var(--chart-fuel)" strokeWidth={2} dot={{ r: 3, fill: "var(--chart-fuel)", stroke: "var(--card)", strokeWidth: 2 }} connectNulls={false}/>
          </ComposedChart>
        </ResponsiveContainer></div>
      </article>

      <article className="card chart-card">
        <header className="card-title"><h3>{scope === "ytd" ? "연 누계" : "당월"} 비용 증감 원인</h3><div className="card-title-side"><span>생산량 · 효율 · 단가</span></div></header>
        {period.bridge ? <CostBridgeView bridge={period.bridge}/> : <div className="cost-empty"><Factory size={24}/><p>{isTotal ? "전력 또는 연료를 선택하면 3요인 원인분해를 볼 수 있습니다." : period.bridgeNote ?? "비교 가능한 전년 실적이 없어 원인분해를 표시하지 않습니다."}</p></div>}
        {period.bridgeNote && period.bridge && <p className="cost-note">{period.bridgeNote}</p>}
        {coverageNote && <p className="cost-note warning">{coverageNote} — 비용이 없는 사용량은 원인분해에서 제외됩니다.</p>}
      </article>

      <article className="card list">
        <header className="card-title"><h3>에너지원별 비용 구성</h3><div className="card-title-side"><span>YTD · 백만원</span></div></header>
        <div className="cost-composition">
          {data.composition.map(row => <div key={row.metric}>
            <p><span>{row.label}</span><b>{fmt(row.cost)} <small>({fmt(row.share)}%)</small></b></p>
            <i><em className={row.metric} style={{ width: `${Math.max(0, Math.min(100, row.share))}%` }}/></i>
            <small className={row.change != null && row.change > 0 ? "bad" : "good"}>{row.change == null ? "전년비 -" : `전년비 ${row.change > 0 ? "+" : ""}${fmt(row.change)}%`}</small>
          </div>)}
        </div>
      </article>

      <article className="card table-card span-all">
        <header className="card-title"><h3>공장별 비용·단가 매트릭스</h3><div className="card-title-side"><span>YTD</span></div></header>
        <div className="table-wrap"><table className="cost-matrix"><thead><tr><th>공장</th><th>비용(백만원)</th>{!isTotal && <th>사용량({data.usageUnit})</th>}<th>{secondaryLabel}({secondaryUnit})</th>{!isTotal && <th>단가 전년비</th>}<th>비용 반영률</th></tr></thead><tbody>
          {data.matrix.map(row => <tr key={row.factory}><td>{row.factory}</td><td>{fmt(row.cost)}</td>{!isTotal && <td>{fmt(row.usage)}</td>}<td>{fmt(isTotal ? row.costPerTon : row.price, 2)}</td>{!isTotal && <td className={row.priceChange != null && row.priceChange <= 0 ? "good" : "bad"}>{row.priceChange == null ? "-" : `${row.priceChange > 0 ? "+" : ""}${fmt(row.priceChange)}%`}</td>}<td>{row.coverage == null ? "-" : `${(row.coverage * 100).toFixed(1)}%`}</td></tr>)}
        </tbody></table></div>
      </article>

      {!isTotal && <article className="card chart-card span-all">
        <header className="card-title"><h3>최근 일별 {data.label} 단가</h3><div className="card-title-side"><span>최근 90일 · {data.priceUnit}</span></div></header>
        <div className="chart"><ResponsiveContainer width="100%" height="100%"><ComposedChart data={data.dailyPrice}><CartesianGrid vertical={false}/><XAxis dataKey="date" interval="preserveStartEnd" minTickGap={22}/><YAxis/><Tooltip {...tooltipStyle} formatter={(value: unknown) => [fmt(value, 2), data.priceUnit ?? "단가"]}/><Line type="linear" dataKey="price" name={`${data.label} 단가`} stroke={metric === "power" ? "var(--chart-power)" : "var(--chart-fuel)"} strokeWidth={2} dot={false} activeDot={{ r: 4 }} connectNulls={false}/></ComposedChart></ResponsiveContainer></div>
      </article>}
    </section>
  </div>;
}
