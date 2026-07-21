"use client";

import { useEffect, useMemo, useState } from "react";
import { Activity, Bolt, BrainCircuit, Building2, CalendarDays, ChevronRight, Database, Download, Factory, Gauge, Menu, Moon, PackageCheck, RefreshCw, ShieldCheck, Sun, X } from "lucide-react";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, ComposedChart, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { apiGet, query } from "@/lib/bems-api";
import { downloadCsv } from "@/lib/bems-csv";
import { demo, factories, factoryColors } from "@/lib/bems-data";
import { DEFAULT_PAGE_VISIBILITY, PAGE_DEFS, type PageId } from "@/lib/bems-pages";
import { AdminScreen } from "@/components/screens/admin-screen";
import { PredictionHistory } from "@/components/screens/prediction-history";
import { PredictionRunner } from "@/components/screens/prediction-runner";
import { ReportScreen } from "@/components/screens/report-screen";
import { SevenDayCompare } from "@/components/seven-day-compare";
import { EnergyComposition } from "@/components/energy-composition";
import { FactoryYoy, IssuesCard } from "@/components/factory-yoy";
import { FeatureImportance } from "@/components/feature-importance";
import { PredictionMonitoring } from "@/components/prediction-monitoring";
import { ProductionItemTrend, ProductionItemYoy } from "@/components/production-item-trend";
import { PivotTable, type PivotRow } from "@/components/pivot-table";
import { ToggleLegend, useSeriesToggle, type LegendItem } from "@/components/toggle-legend";
import { DataToggle } from "@/components/data-toggle";
import { EventMarkerHint, dayLabelOf, eventMarkers, groupEventsByLabel, monthLabelOf, useFieldEvents, type EventTarget } from "@/components/event-markers";
import { PredictionGap } from "@/components/prediction-gap";

type Screen = PageId;
type DataScreen = Exclude<Screen, "report" | "admin">;
type IntensityMetric = "power" | "fuel" | "water";
type AnyData = Record<string, any>;

// 메뉴 정의(라벨·아이콘)는 lib/bems-pages.ts가 단일 소스 — 관리자 전용 메뉴의
// "페이지 노출 설정" 탭과 이 사이드바가 같은 목록을 공유한다.
const menus = PAGE_DEFS;

const titles: Record<Screen, [string, string]> = {
  dashboard: ["통합 에너지 대시보드", "실적과 AI 예측을 한눈에 확인합니다."],
  energy: ["에너지 사용량", "전력·연료·용수 사용 추이를 비교합니다."],
  intensity: ["에너지 원단위", "생산량 대비 에너지 효율을 추적합니다."],
  production: ["생산실적 분석", "계획 대비 생산성과 제품 믹스를 분석합니다."],
  prediction: ["AI 에너지 예측", "v5.3 예측값과 정상범주 이탈을 모니터링합니다."],
  report: ["AI 에너지 실적 보고서", "저장된 월간 보고서를 열람하고 생성합니다."],
  admin: ["관리자 전용 메뉴", "목표, 이벤트, 업로드와 예측 이력을 관리합니다."],
};

const endpoint: Record<DataScreen, string> = { dashboard: "/dashboard", energy: "/energy", intensity: "/intensity", production: "/production", prediction: "/predictions" };
const screenFallback: Record<DataScreen, AnyData> = {
  dashboard: demo.dashboard,
  energy: demo.energy,
  intensity: demo.intensity,
  production: demo.production,
  prediction: demo.predictions,
};
const isDataScreen = (screen: Screen): screen is DataScreen => screen !== "report" && screen !== "admin";
const intensityMetrics: { id: IntensityMetric; label: string }[] = [{ id: "power", label: "전력" }, { id: "fuel", label: "연료" }, { id: "water", label: "용수" }];
const intensityUnits: Record<IntensityMetric, string> = { power: "kWh/ton", fuel: "Nm³/ton", water: "ton/ton" };

// 공통 차트 팔레트 — 전 화면이 같은 의미에 같은 색을 쓴다.
// 실제 색상값은 globals.css의 --chart-* 변수가 제공하며, 다크모드에서는
// 다크 표면 대비로 재검증된 단계(amber 등)로 자동 전환된다.
const palette = {
  actual: "var(--chart-actual)",       // 실측·금년 (#2563eb)
  predicted: "var(--chart-predicted)", // AI 예측 (#8b5cf6)
  previous: "var(--chart-previous)",   // 전년 (#94a3b8)
  target: "var(--chart-target)",       // 목표·긍정 (#159568)
  band: "var(--chart-band)",           // 예측 범위 (#4f7cff)
  cat2: { IC: "var(--chart-actual)", MY: "var(--chart-target)", FM: "var(--chart-predicted)", SN: "var(--chart-amber)", ETC: "var(--chart-previous)" } as Record<string, string>,
};
const cat2Labels: Record<string, string> = { IC: "IC (아이스크림)", MY: "MY (유음료)", FM: "FM (발효유)", SN: "SN (스낵)", ETC: "기타" };
const tooltipStyle = { contentStyle: { borderRadius: 10, border: "1px solid var(--line)", background: "var(--card)", boxShadow: "0 6px 18px #12201814", fontSize: 12 }, labelStyle: { color: "var(--text)" } };
// dataviz 마크 스펙 — 시리즈색 채움 + 2px 표면색 링(라인 교차·중첩 시 판독 확보)
const seriesDot = (color: string) => ({ r: 3, fill: color, stroke: "var(--card)", strokeWidth: 2 });
const numberFormatter = (value: unknown) => typeof value === "number" ? value.toLocaleString("ko-KR", { maximumFractionDigits: 2 }) : Array.isArray(value) ? value.map(item => typeof item === "number" ? item.toLocaleString("ko-KR", { maximumFractionDigits: 2 }) : String(item)).join(" ~ ") : String(value ?? "-");

type ProductionMode = "month" | "range" | "year";
const productionModes: { id: ProductionMode; label: string }[] = [
  { id: "month", label: "월간" },
  { id: "range", label: "기간별" },
  { id: "year", label: "연간" },
];

function CsvButton({ filename, rows, columns, labels }: { filename: string; rows?: AnyData[]; columns: string[]; labels: Record<string, string> }) {
  return <button type="button" className="csv-button" title="현재 표 데이터를 CSV로 내려받습니다 (Excel 한글 호환)"
    disabled={!rows?.length} onClick={() => downloadCsv(filename, rows ?? [], columns, labels)}>
    <Download size={13}/>CSV
  </button>;
}

const fmt = (value: unknown, digits = 1) => typeof value === "number"
  ? value.toLocaleString("ko-KR", { maximumFractionDigits: digits })
  : typeof value === "string" ? value : "-";

// ISO 일자에 일수를 더한 ISO 일자 — 이벤트 조회 구간을 기준일 기준으로 잡을 때 쓴다.
function shiftIsoDate(iso: string, days: number) {
  if (!iso) return "";
  const parsed = Date.parse(`${iso.slice(0, 10)}T00:00:00`);
  if (Number.isNaN(parsed)) return "";
  const shifted = new Date(parsed + days * 86_400_000);
  return `${shifted.getFullYear()}-${String(shifted.getMonth() + 1).padStart(2, "0")}-${String(shifted.getDate()).padStart(2, "0")}`;
}

function localYesterday() {
  const yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);
  const month = String(yesterday.getMonth() + 1).padStart(2, "0");
  const day = String(yesterday.getDate()).padStart(2, "0");
  return `${yesterday.getFullYear()}-${month}-${day}`;
}

// 부분 결측을 완전한 집계로 오인하지 않게 — 결측이 없으면 아무것도 표시하지 않는다.
function CoverageChip({ coverage }: { coverage?: AnyData }) {
  const missing = Number(coverage?.missingDays ?? 0);
  if (!coverage || missing <= 0) return null;
  return <span className="coverage-chip" title="선택 기간 중 에너지 실적이 없는 날입니다. 주말·공휴일이면 정상입니다.">
    {coverage.expectedDays}일 중 {missing}일 데이터 없음
  </span>;
}

function Kpi({ label, value, unit, change, goodWhen = "down", icon: Icon = Activity }: { label: string; value: unknown; unit?: string; change?: number | null; goodWhen?: "down" | "up"; icon?: typeof Activity }) {
  const digits = unit === "ton/ton" ? 2 : 1;
  const isGood = goodWhen === "up" ? (change ?? 0) >= 0 : (change ?? 0) <= 0;
  return <article className="kpi card"><div className="kpi-icon"><Icon size={20}/></div><div><p>{label}</p><strong>{fmt(value, digits)} <small>{unit}</small></strong>{change != null && <span className={isGood ? "good" : "bad"}>{change > 0 ? "+" : ""}{fmt(change)}% 전년비</span>}</div></article>;
}

function Dashboard({ data, factory, date }: { data: AnyData; factory: string; date: string }) {
  const trend = (data.trend ?? []).map((row: AnyData) => ({
    ...row,
    band: row.lower != null && row.upper != null ? [row.lower, row.upper] : null,
  }));
  const trendLegend = useSeriesToggle();
  const trendItems: LegendItem[] = [{ key: "band", label: "P05~P95", color: palette.band }, { key: "predicted", label: "AI 예측", color: palette.predicted }, { key: "actual", label: "실제", color: palette.actual }];
  const powerYoyLegend = useSeriesToggle();
  const powerYoyItems: LegendItem[] = [{ key: "previous", label: "전년", color: palette.previous }, { key: "current", label: "금년", color: palette.actual }];
  // 7일 차트 구간의 현장 이벤트 — trend는 %m.%d 라벨이라 dayLabelOf와 짝이 맞는다.
  const baseDate: string = data.baseDate ?? "";
  const dashboardEvents = useFieldEvents(factory, shiftIsoDate(baseDate, -6), baseDate, Boolean(baseDate));
  const trendEvents = groupEventsByLabel(dashboardEvents, "power", dayLabelOf);
  const signals: AnyData[] = data.alert?.signals ?? [];
  return <>
    <section className="kpi-grid">{data.metrics?.map((m: AnyData) => <Kpi key={m.id} label={m.label} value={m.value} unit={m.unit} change={m.change} goodWhen={m.id === "production" ? "up" : "down"} icon={m.id === "production" ? Factory : Bolt}/>)}</section>
    <section className={`alert ${data.alert?.level ?? "normal"}`}><BrainCircuit size={22}/><div><strong>{data.alert?.title}</strong><p>{data.alert?.description}</p>
      {signals.length > 0 && <ul className="alert-signals">
        {signals.map((signal, index) => <li key={index}>
          <em className={signal.kind === "alert" ? "sig-alert" : "sig-drift"}>{signal.label}</em>
          <b>{signal.factory} {signal.target}</b>
          <span>{signal.detail}</span>
        </li>)}
      </ul>}
    </div></section>
    <section className="content-grid"><article className="card chart-card wide"><CardTitle title="최근 7일 전력 사용량" meta="MWh · AI P05~P95 정상범주"><CsvButton filename={`7day_trend_${factory}_${date}`} rows={data.trend} columns={["date","actual","predicted","lower","upper"]} labels={{date:"일자",actual:"실제(MWh)",predicted:"AI 예측(MWh)",lower:"P05(MWh)",upper:"P95(MWh)"}}/></CardTitle><Chart><AreaChart data={trend}><defs><linearGradient id="band" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={palette.band} stopOpacity={.22}/><stop offset="1" stopColor={palette.band} stopOpacity={.02}/></linearGradient></defs><CartesianGrid vertical={false}/><XAxis dataKey="date"/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/>{!trendLegend.isHidden("band") && <Area type="linear" dataKey="band" name="P05~P95" stroke="none" fill="url(#band)" connectNulls={false}/>}{!trendLegend.isHidden("predicted") && <Line type="linear" dataKey="predicted" name="AI 예측" stroke={palette.predicted} strokeDasharray="5 4" strokeWidth={2} dot={seriesDot(palette.predicted)} activeDot={{ r: 5 }}/>}{!trendLegend.isHidden("actual") && <Line type="linear" dataKey="actual" name="실제" stroke={palette.actual} strokeWidth={2} dot={seriesDot(palette.actual)} activeDot={{ r: 5 }}/>}{eventMarkers(trendEvents)}</AreaChart></Chart><EventMarkerHint count={trendEvents.size}/><ToggleLegend items={trendItems} hidden={trendLegend.hidden} onToggle={trendLegend.toggle}/></article>
    <article className="card chart-card"><CardTitle title="공장별 전력 원단위" meta="낮을수록 효율적"/><Chart><BarChart data={data.factoryComparison} layout="vertical"><CartesianGrid horizontal={false}/><XAxis type="number"/><YAxis dataKey="factory" type="category" width={58}/><Tooltip {...tooltipStyle} formatter={numberFormatter}/><Bar dataKey="value" name="kWh/ton" fill={palette.target} radius={[0,4,4,0]} maxBarSize={18}/></BarChart></Chart><p className="quad-caption">{WIP_FOOTNOTE}</p></article>
    <article className="card chart-card"><CardTitle title="월별 전년 비교" meta="kWh/ton"><CsvButton filename={`yoy_power_${factory}_${date}`} rows={data.yoy} columns={["month","previous","current"]} labels={{month:"월","previous":"전년(kWh/ton)",current:"금년(kWh/ton)"}}/></CardTitle><Chart><LineChart data={data.yoy}><CartesianGrid vertical={false}/><XAxis dataKey="month"/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/>{!powerYoyLegend.isHidden("previous") && <Line dataKey="previous" name="전년" stroke={palette.previous} strokeWidth={2} dot={seriesDot(palette.previous)}/>}{!powerYoyLegend.isHidden("current") && <Line dataKey="current" name="금년" stroke={palette.actual} strokeWidth={2} dot={seriesDot(palette.actual)} activeDot={{ r: 5 }}/>}</LineChart></Chart><ToggleLegend items={powerYoyItems} hidden={powerYoyLegend.hidden} onToggle={powerYoyLegend.toggle}/></article>
    <article className="card events"><CardTitle title="최근 현장 이벤트" meta={`${data.events?.length ?? 0}건`}/>{data.events?.map((event: AnyData) => <div className="event" key={event.id}><time>{event.date}</time><span>{event.factory}</span><div><b>{event.tag}</b><p>{event.note}</p></div></div>)}</article>
    <SevenDayCompare trend={data.trend ?? []} factory={factory} date={date}/>
    <FactoryYoy rows={data.yoyFactories ?? []} period={data.yoyPeriod} factory={factory} date={date}/>
    {/* 월간 주요 이슈(좁게)와 공장별 에너지 비율(넓게)을 한 행에 배치해 공간 절약 */}
    <div className="dash-pair">
      {(data.yoyFactories?.length ?? 0) > 0 && <IssuesCard rows={data.yoyFactories}/>}
      <EnergyComposition rows={data.composition ?? []} label={data.compositionLabel} className=""/>
    </div></section></>;
}

// 광주만 원단위 분모에 판매용 재공품이 더해진다 — 다른 공장과 나란히 비교되는
// 표·차트에는 분모 정의가 다르다는 각주를 반드시 함께 둔다.
const WIP_FOOTNOTE = "※ 광주는 자사 완제품 외에 외부 판매용 재공품(탈지분유·살균유 등)이 생산량 분모에 포함됩니다 — 다른 공장과 분모 정의가 달라 절대 비교 시 유의하세요.";

// 원단위 변동 원인분해 막대 — 전년 원단위에서 두 효과를 거쳐 금년 원단위에 도달하는 흐름.
function BridgeBars({ bridge, unit }: { bridge: AnyData; unit: string }) {
  const steps = [
    { key: "usage", label: "사용량 효과", value: Number(bridge.usageEffect) || 0 },
    { key: "production", label: "생산량 효과", value: Number(bridge.productionEffect) || 0 },
  ];
  const scale = Math.max(...steps.map(step => Math.abs(step.value)), 0.0001);
  return <div className="bridge">
    <div className="bridge-end"><span>전년 동기</span><b>{fmt(bridge.previous, 2)}</b><small>{unit}</small></div>
    <div className="bridge-steps">
      {steps.map(step => <div className="bridge-step" key={step.key}>
        <span>{step.label}</span>
        <i><em className={step.value >= 0 ? "up" : "down"} style={{ width: `${Math.abs(step.value) / scale * 100}%` }}/></i>
        <b className={step.value >= 0 ? "bad" : "good"}>{step.value >= 0 ? "+" : ""}{fmt(step.value, 2)}</b>
      </div>)}
    </div>
    <div className="bridge-end"><span>금년</span><b>{fmt(bridge.current, 2)}</b><small>{unit}</small></div>
  </div>;
}

type EnergyMode = "recent" | "range";
const energyModes: { id: EnergyMode; label: string }[] = [
  { id: "recent", label: "당월" },
  { id: "range", label: "기간 지정" },
];
const energyMetricLabels: Record<string, string> = { power: "전력", fuel: "연료", water: "용수", wastewater: "폐수" };
// 에너지원 상징색 (2026-07-18 사용자 지정) — globals.css의 --chart-* 변수와 동기.
const energyMetricColors: Record<string, string> = {
  power: "var(--chart-power)", fuel: "var(--chart-fuel)",
  water: "var(--chart-water)", wastewater: "var(--chart-wastewater)",
};

// legacy '전년대비 사용량' 누계 테이블 — 월별 금년/전년/증감량/증감률에 누계 행을 더한다.
function buildYoyTable(yoy: AnyData[], metric: string) {
  const rows = (yoy ?? []).map(row => {
    const current = row[metric]?.current ?? null;
    const previous = row[metric]?.previous ?? null;
    const diff = current != null && previous != null ? current - previous : null;
    const diffPct = diff != null && previous > 0 ? (diff / previous) * 100 : null;
    return { month: row.month as string, previous, current, diff, diffPct };
  });
  // 누계는 금년 실적이 있는 월까지만 전년과 같은 기간으로 합산한다 —
  // 전년 전체(12개월) vs 금년 일부를 비교하면 증감률이 왜곡되기 때문.
  const compared = rows.filter(row => row.current != null);
  const basis = compared.length ? compared : rows.filter(row => row.previous != null);
  const sum = (key: "previous" | "current") => basis.reduce((acc, row) => acc + (row[key] ?? 0), 0);
  const sumPrev = sum("previous");
  const sumCurr = sum("current");
  const total = basis.length ? {
    month: compared.length ? `누계 (1~${compared[compared.length - 1].month})` : "누계",
    previous: sumPrev,
    current: compared.length ? sumCurr : null,
    diff: compared.length ? sumCurr - sumPrev : null,
    diffPct: compared.length && sumPrev > 0 ? ((sumCurr - sumPrev) / sumPrev) * 100 : null,
  } : null;
  return { rows, total };
}

function Energy({ data, factory, mode, onModeChange, rangeFrom, rangeTo, onRangeChange }: {
  data: AnyData; factory: string; mode: EnergyMode; onModeChange: (mode: EnergyMode) => void;
  rangeFrom: string; rangeTo: string; onRangeChange: (from: string, to: string) => void;
}) {
  const [metric, setMetric] = useState("power"); const units: AnyData = { power: "MWh", fuel: "천 Nm³", water: "천 ton", wastewater: "천 ton" };
  const [compareFactories, setCompareFactories] = useState(false);
  const values = data.daily?.map((r: AnyData) => Number(r[metric]) || 0) ?? []; const total = values.reduce((a: number,b: number)=>a+b,0);
  const periodLabel = data.dateFrom && data.dateTo ? `${data.dateFrom} ~ ${data.dateTo}` : "";
  const summaryMeta = mode === "range" ? "선택 기간" : "당월";
  // 공장별 비교 (legacy compare_factories) — 전사 조회에서만 서버가 공장별 시리즈 제공
  const byFactoryRows = (data.dailyByFactory ?? []).map((row: AnyData) => ({
    date: row.date,
    ...Object.fromEntries(Object.entries(row.metrics ?? {}).map(([name, metricValues]) => [name, (metricValues as AnyData)?.[metric] ?? null])),
  }));
  const compareNames = ["남양주", "김해", "광주", "논산", "경산"].filter(name => byFactoryRows.some((row: AnyData) => row[name] != null));
  const comparing = compareFactories && compareNames.length > 0;
  const yoyTable = buildYoyTable(data.yoy ?? [], metric);
  const yoyUnit = units[metric];
  const yoyCsvRows = [...yoyTable.rows, ...(yoyTable.total ? [yoyTable.total] : [])].map(row => ({
    month: row.month, previous: row.previous, current: row.current, diff: row.diff,
    diffPct: row.diffPct == null ? null : Math.round(row.diffPct * 10) / 10,
  }));
  const wasteRatioRows = (data.factories ?? []).map((r: AnyData) => ({ factory: r.factory, water: r.water, wastewater: r.wastewater, ratio: r.water > 0 ? Math.round(r.wastewater / r.water * 100) / 100 : null }));
  const isWater = metric === "water" || metric === "wastewater";
  // 일별 사용 추이 — 범례 on/off(끄면 autoscale) + 전치 데이터 표(시간을 열로, 지표를 행으로).
  const dailyLegend = useSeriesToggle();
  const dailySeriesForTable = comparing ? byFactoryRows : (data.daily ?? []);
  const dailyPeriods = dailySeriesForTable.map((r: AnyData) => r.date);
  const dailyRowDefs: LegendItem[] = comparing
    ? compareNames.map(name => ({ key: name, label: name, color: factoryColors[name] }))
    : metric === "power"
    ? [
        { key: "power", label: "전체 전력", color: energyMetricColors.power },
        { key: "freezing", label: "냉동", color: palette.actual },
        { key: "compressor", label: "공압", color: palette.target },
        { key: "other", label: "기타", color: palette.previous },
      ]
    : [{ key: metric, label: energyMetricLabels[metric], color: energyMetricColors[metric] }];
  const dailyPivotRows: PivotRow[] = dailyRowDefs.map(def => {
    const rowValues = dailySeriesForTable.map((r: AnyData) => (typeof r[def.key] === "number" ? r[def.key] : null));
    const total = rowValues.reduce((acc: number, v: number | null) => acc + (v ?? 0), 0);
    return { key: def.key, label: def.label, values: rowValues, total };
  });
  const showDailyLegend = dailyRowDefs.length > 1;
  const yoyLegend = useSeriesToggle();
  // 현장 이벤트를 일별 차트에 점선 마커로 — 스파이크 옆에서 원인 메모를 바로 읽게 한다.
  const fieldEvents = useFieldEvents(factory, data.dateFrom ?? "", data.dateTo ?? "");
  const dailyEvents = groupEventsByLabel(fieldEvents, metric as EventTarget, dayLabelOf);
  return <><div className="segmented" role="group" aria-label="에너지 지표 선택">{Object.entries(energyMetricLabels).map(([id,label])=><button type="button" className={metric===id?"active":""} aria-pressed={metric===id} onClick={()=>setMetric(id)} key={id}>{label}</button>)}</div>
    <div className="mode-row">
      <div className="segmented" role="group" aria-label="사용량 조회 방식">{energyModes.map(item => <button type="button" key={item.id} className={mode === item.id ? "active" : ""} aria-pressed={mode === item.id} onClick={() => onModeChange(item.id)}>{item.label}</button>)}</div>
      {mode === "range" && <div className="range-fields">
        <label><span>시작일</span><input type="date" value={rangeFrom} max={rangeTo} onChange={event => onRangeChange(event.target.value, rangeTo)}/></label>
        <label><span>종료일</span><input type="date" value={rangeTo} min={rangeFrom} onChange={event => onRangeChange(rangeFrom, event.target.value)}/></label>
      </div>}
      {(data.dailyByFactory?.length ?? 0) > 0 && <label className="check-toggle"><input type="checkbox" checked={compareFactories} onChange={event => setCompareFactories(event.target.checked)}/>공장별 비교</label>}
      {periodLabel && <span className="period-chip">{periodLabel}</span>}
      <CoverageChip coverage={data.coverage}/>
    </div>
    <section className="kpi-grid compact"><Kpi label="기간 누계" value={total} unit={units[metric]} icon={Bolt}/><Kpi label="일평균" value={values.length?total/values.length:0} unit={units[metric]} icon={Activity}/><Kpi label="최대 사용량" value={values.length?Math.max(...values):0} unit={units[metric]} icon={Gauge}/></section>
    <section className="content-grid">
      <article className="card chart-card wide"><CardTitle title={comparing ? "일별 사용 추이 · 공장별 비교" : metric === "power" ? "일별 사용 추이 · 설비 분해" : "일별 사용 추이"} meta={units[metric]}>{comparing
        ? <CsvButton filename={`energy_daily_factories_${metric}`} rows={byFactoryRows} columns={["date", ...compareNames]} labels={{date:"일자",...Object.fromEntries(compareNames.map(name=>[name,`${name}(${units[metric]})`]))}}/>
        : <CsvButton filename={`energy_daily_${metric}`} rows={data.daily} columns={["date","power","freezing","compressor","other","fuel","water","wastewater"]} labels={{date:"일자",power:"전력(MWh)",freezing:"냉동(MWh)",compressor:"공압(MWh)",other:"기타(MWh)",fuel:"연료(천 Nm³)",water:"용수(천 ton)",wastewater:"폐수(천 ton)"}}/>}</CardTitle>
        <Chart>{comparing
        ? <LineChart data={byFactoryRows}><CartesianGrid vertical={false}/><XAxis dataKey="date" interval="preserveStartEnd" minTickGap={18}/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/>{compareNames.map(name => !dailyLegend.isHidden(name) && <Line key={name} type="linear" dataKey={name} name={name} stroke={factoryColors[name]} strokeWidth={2} dot={false} activeDot={{ r: 5 }} connectNulls={false}/>)}{eventMarkers(dailyEvents)}</LineChart>
        : metric === "power"
        ? <ComposedChart data={data.daily}><CartesianGrid vertical={false}/><XAxis dataKey="date" interval="preserveStartEnd" minTickGap={18}/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/>{!dailyLegend.isHidden("power") && <Area type="linear" dataKey="power" name="전체 전력" stroke={energyMetricColors.power} strokeWidth={2} fill={energyMetricColors.power} fillOpacity={0.1}/>}{!dailyLegend.isHidden("freezing") && <Line type="linear" dataKey="freezing" name="냉동" stroke={palette.actual} strokeWidth={2} dot={false}/>}{!dailyLegend.isHidden("compressor") && <Line type="linear" dataKey="compressor" name="공압" stroke={palette.target} strokeWidth={2} dot={false}/>}{!dailyLegend.isHidden("other") && <Line type="linear" dataKey="other" name="기타" stroke={palette.previous} strokeWidth={2} strokeDasharray="4 3" dot={false}/>}{eventMarkers(dailyEvents)}</ComposedChart>
        : <AreaChart data={data.daily}><CartesianGrid vertical={false}/><XAxis dataKey="date" interval="preserveStartEnd" minTickGap={18}/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/><Area type="linear" dataKey={metric} stroke={energyMetricColors[metric]} strokeWidth={2} fill={energyMetricColors[metric]} fillOpacity={0.1}/>{eventMarkers(dailyEvents)}</AreaChart>}</Chart>
        {showDailyLegend && <ToggleLegend items={dailyRowDefs} hidden={dailyLegend.hidden} onToggle={dailyLegend.toggle}/>}
        <EventMarkerHint count={dailyEvents.size}/>
        <DataToggle><PivotTable periods={dailyPeriods} rows={dailyPivotRows} totalLabel="누계"/></DataToggle></article>
      <article className="card list"><CardTitle title="설비 구성" meta={summaryMeta}/>{data.equipment?.map((r:AnyData)=><div className="progress" key={r.name}><div><span>{r.name}</span><b>{fmt(r.value)}%</b></div><i><em style={{width:`${r.value}%`}}/></i></div>)}</article>
      <article className="card chart-card wide"><CardTitle title={`전년대비 ${energyMetricLabels[metric]} 사용량`} meta={`${data.yoyYear ?? ""}년 vs 전년 · ${yoyUnit}`}><CsvButton filename={`energy_yoy_${metric}_${data.yoyYear ?? ""}`} rows={yoyCsvRows} columns={["month","previous","current","diff","diffPct"]} labels={{month:"월",previous:`전년(${yoyUnit})`,current:`금년(${yoyUnit})`,diff:`증감량(${yoyUnit})`,diffPct:"증감률(%)"}}/></CardTitle>
        <Chart><LineChart data={yoyTable.rows}><CartesianGrid vertical={false}/><XAxis dataKey="month"/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/>{!yoyLegend.isHidden("previous") && <Line type="linear" dataKey="previous" name="전년" stroke={palette.previous} strokeWidth={2} dot={seriesDot(palette.previous)} connectNulls/>}{!yoyLegend.isHidden("current") && <Line type="linear" dataKey="current" name="금년" stroke={energyMetricColors[metric]} strokeWidth={2} dot={seriesDot(energyMetricColors[metric])} activeDot={{ r: 5 }} connectNulls/>}</LineChart></Chart>
        <ToggleLegend items={[{key:"previous",label:"전년",color:palette.previous},{key:"current",label:"금년",color:energyMetricColors[metric]}]} hidden={yoyLegend.hidden} onToggle={yoyLegend.toggle}/>
        <DataToggle><PivotTable periods={yoyTable.rows.map(row => row.month)} totalLabel={yoyTable.total?.month ?? "누계"} rows={[
          { key: "previous", label: `전년 실적(${yoyUnit})`, values: yoyTable.rows.map(row => row.previous), total: yoyTable.total?.previous ?? null },
          { key: "current", label: `금년 실적(${yoyUnit})`, values: yoyTable.rows.map(row => row.current), total: yoyTable.total?.current ?? null },
          { key: "diff", label: `증감량(${yoyUnit})`, values: yoyTable.rows.map(row => row.diff), total: yoyTable.total?.diff ?? null },
          { key: "diffPct", label: "증감률(%)", values: yoyTable.rows.map(row => row.diffPct), total: yoyTable.total?.diffPct ?? null,
            format: value => value == null ? "-" : `${Number(value) > 0 ? "+" : ""}${fmt(Number(value))}`,
            className: value => value == null ? undefined : Number(value) > 0 ? "bad" : "good" },
        ]}/></DataToggle></article>
      {isWater
        ? <article className="card chart-card"><CardTitle title="공장별 폐수/용수 비율" meta={`${summaryMeta} · 낮을수록 양호`}><CsvButton filename={`wastewater_ratio_${data.dateFrom ?? ""}`} rows={wasteRatioRows} columns={["factory","water","wastewater","ratio"]} labels={{factory:"공장",water:"용수(천 ton)",wastewater:"폐수(천 ton)",ratio:"폐수/용수"}}/></CardTitle><Chart><BarChart data={wasteRatioRows}><CartesianGrid vertical={false}/><XAxis dataKey="factory"/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/><Bar dataKey="ratio" name="폐수/용수 비율" fill={energyMetricColors.wastewater} radius={[4,4,0,0]} maxBarSize={22}/></BarChart></Chart></article>
        : <article className="card table-card"><CardTitle title="공장별 사용량" meta={`${summaryMeta} 누계`}><CsvButton filename="energy_factories" rows={data.factories} columns={["factory","power","fuel","water","wastewater"]} labels={{factory:"공장",power:"전력(MWh)",fuel:"연료(천 Nm³)",water:"용수(천 ton)",wastewater:"폐수(천 ton)"}}/></CardTitle><DataTable rows={data.factories} columns={["factory",metric]} labels={{factory:"공장",[metric]:units[metric]}}/></article>}
      {isWater && <article className="card table-card span-all"><CardTitle title="공장별 사용량" meta={`${summaryMeta} 누계`}><CsvButton filename="energy_factories" rows={data.factories} columns={["factory","power","fuel","water","wastewater"]} labels={{factory:"공장",power:"전력(MWh)",fuel:"연료(천 Nm³)",water:"용수(천 ton)",wastewater:"폐수(천 ton)"}}/></CardTitle><DataTable rows={data.factories} columns={["factory",metric]} labels={{factory:"공장",[metric]:units[metric]}}/></article>}
    </section></>;
}

function Intensity({ data, factory, metric, onMetricChange, mode, onModeChange, rangeFrom, rangeTo, onRangeChange }: {
  data: AnyData; factory: string; metric: IntensityMetric; onMetricChange: (metric: IntensityMetric) => void;
  mode: EnergyMode; onModeChange: (mode: EnergyMode) => void;
  rangeFrom: string; rangeTo: string; onRangeChange: (from: string, to: string) => void;
}) {
  const periodLabel = data.dateFrom && data.dateTo ? `${data.dateFrom} ~ ${data.dateTo}` : "";
  const [showCumulative, setShowCumulative] = useState(false);
  const metricColor = energyMetricColors[metric] ?? palette.actual;
  // 주말·공휴일 등 생산 실적이 없는 날은 value가 null로 내려온다 — 그 날짜 자체를
  // 배열에서 제거해 x축에 빈 구간이 생기지 않고 실적 있는 날끼리 바로 이어지게 한다.
  const measuredDays: AnyData[] = (data.daily ?? []).filter((row: AnyData) => row.value != null);
  // 가동일 필터 — 원단위는 분모가 작아지면 불안정해지는 비율이라, 소량 생산일의
  // 원단위는 "효율이 나빴다"가 아니라 "고정부하가 소량에 배분됐다"는 뜻이다.
  // 그대로 같은 축에 두면 한 점이 평일 밴드를 수십 배로 눌러 증감이 안 보인다.
  // 임계값은 기간 중앙 생산량의 50% — 실측상 가동일(중앙값의 80% 이상)과
  // 비가동일(30% 이하) 사이가 비어 있어 어디를 잡아도 같은 결과가 나온다.
  const [operatingOnly, setOperatingOnly] = useState(true);
  const sortedTons = measuredDays.map((row: AnyData) => Number(row.productionTon) || 0).sort((a: number, b: number) => a - b);
  const medianTon = sortedTons.length ? sortedTons[Math.floor(sortedTons.length / 2)] : 0;
  const operatingThreshold = medianTon * 0.5;
  const isOperating = (row: AnyData) => medianTon <= 0 || (Number(row.productionTon) || 0) >= operatingThreshold;
  const dailySeries = operatingOnly ? measuredDays.filter(isOperating) : measuredDays;
  const excludedDays = measuredDays.length - dailySeries.length;
  // 연간 '누계 추이 보기' (legacy 규칙, 원단위 페이지) — 각 월을 1월부터의 가중
  // 누계 원단위(Σ사용량 ÷ Σ생산톤)로 재계산한다. 단순 월 원단위 평균과 다르다.
  const monthlyBase = data.monthly ?? [];
  const monthlySeries = (() => {
    if (!showCumulative) return monthlyBase;
    let curUsage = 0, curTon = 0, prevUsage = 0, prevTon = 0;
    const targetPct = data.targetPct;
    return monthlyBase.map((row: AnyData) => {
      curUsage += Number(row.currentUsage) || 0; curTon += Number(row.currentTon) || 0;
      prevUsage += Number(row.previousUsage) || 0; prevTon += Number(row.previousTon) || 0;
      const current = curTon > 0 && row.current != null ? Math.round(curUsage / curTon * 100) / 100 : null;
      const previous = prevTon > 0 ? Math.round(prevUsage / prevTon * 100) / 100 : null;
      const target = previous != null && targetPct != null ? Math.round(previous * (1 - targetPct / 100) * 100) / 100 : null;
      return { month: row.month, current, previous, target };
    });
  })();
  // 전년대비 테이블 — 월별 증감률은 클라이언트 계산, 누계 행은 서버의 가중 평균값.
  const yoyRows = monthlySeries.map((row: AnyData) => ({
    month: row.month, previous: row.previous, current: row.current,
    change: row.current != null && row.previous > 0 ? Math.round((row.current / row.previous - 1) * 1000) / 10 : null,
  }));
  const cumulative = data.yoyCumulative;
  // 일별 원단위 표의 누계는 단순 평균이 아니라 가중 누계(Σ사용량÷Σ생산톤) — 서버가
  // 각 날짜에 실어 보내는 usage/productionTon 원자료로 클라이언트가 재계산한다.
  // 차트에서 비가동일을 빼도 누계는 전체 기간 기준이어야 실제 실적과 맞는다.
  const dailyUsageTotal = measuredDays.reduce((acc: number, row: AnyData) => acc + (Number(row.usage) || 0), 0);
  const dailyTonTotal = measuredDays.reduce((acc: number, row: AnyData) => acc + (Number(row.productionTon) || 0), 0);
  const dailyWeightedTotal = dailyTonTotal > 0 ? Math.round(dailyUsageTotal / dailyTonTotal * 100) / 100 : null;
  const monthlyLegend = useSeriesToggle();
  const showMonthlyTotal = Boolean(cumulative) && !showCumulative;
  const intensityEvents = groupEventsByLabel(
    useFieldEvents(factory, data.dateFrom ?? "", data.dateTo ?? ""), metric as EventTarget, dayLabelOf,
  );
  // P1-2 원인분해 — 원단위는 사용량÷생산량이라 값만으로는 악화 원인을 알 수 없다.
  const [bridgeScope, setBridgeScope] = useState<"ytd" | "mtd">("ytd");
  const bridge: AnyData | null = data.bridge?.[bridgeScope] ?? null;
  return <><div className="segmented" role="group" aria-label="원단위 지표 선택">{intensityMetrics.map(item=><button type="button" key={item.id} className={metric===item.id?"active":""} aria-pressed={metric===item.id} onClick={()=>onMetricChange(item.id)}>{item.label}</button>)}</div>
    <div className="mode-row">
      <div className="segmented" role="group" aria-label="원단위 일별 조회 방식">{energyModes.map(item => <button type="button" key={item.id} className={mode === item.id ? "active" : ""} aria-pressed={mode === item.id} onClick={() => onModeChange(item.id)}>{item.label}</button>)}</div>
      {mode === "range" && <div className="range-fields">
        <label><span>시작일</span><input type="date" value={rangeFrom} max={rangeTo} onChange={event => onRangeChange(event.target.value, rangeTo)}/></label>
        <label><span>종료일</span><input type="date" value={rangeTo} min={rangeFrom} onChange={event => onRangeChange(rangeFrom, event.target.value)}/></label>
      </div>}
      {periodLabel && <span className="period-chip">{periodLabel}</span>}
      <CoverageChip coverage={data.coverage}/>
    </div>
    <section className="kpi-grid compact"><Kpi label="MTD 원단위" value={data.summary?.mtd?.current} unit={data.unit} change={data.summary?.mtd?.change} icon={Gauge}/><Kpi label="YTD 원단위" value={data.summary?.ytd?.current} unit={data.unit} change={data.summary?.ytd?.change} icon={CalendarDays}/><Kpi label="절감 목표" value={data.targetPct} unit="%" icon={ShieldCheck}/></section>
    <section className="content-grid">
      <article className="card chart-card span-all"><CardTitle title="일별 원단위 추이" meta={`${data.unit} · ${operatingOnly ? "가동일" : "실적 있는 날 전체"}`}>
          <label className="check-toggle"><input type="checkbox" checked={operatingOnly} onChange={event => setOperatingOnly(event.target.checked)}/>가동일만 보기</label>
          <CsvButton filename={`intensity_daily_${metric}_${(data.dateFrom ?? "").replaceAll("-","")}`} rows={measuredDays} columns={["date","value","productionTon"]} labels={{date:"일자",value:`원단위(${data.unit})`,productionTon:"생산량(ton)"}}/></CardTitle>
        <Chart><LineChart data={dailySeries}><CartesianGrid vertical={false}/><XAxis dataKey="date" interval="preserveStartEnd" minTickGap={18}/><YAxis domain={["auto","auto"]}/><Tooltip {...tooltipStyle} formatter={numberFormatter}/><Line type="linear" dataKey="value" name={`원단위(${data.unit})`} stroke={metricColor} strokeWidth={2} connectNulls={false} dot={seriesDot(metricColor)} activeDot={{ r: 5 }}/>{eventMarkers(intensityEvents)}</LineChart></Chart>
        {operatingOnly && excludedDays > 0 && <p className="quad-caption">비가동 {excludedDays}일(생산량이 기간 중앙값의 50% 미만)을 차트에서 제외했습니다 — 소량 생산일은 고정부하가 분모에 몰려 원단위가 수십 배로 튀어 평일 증감을 가립니다. 아래 데이터 표에는 모든 날짜가 그대로 있습니다.</p>}
        <EventMarkerHint count={intensityEvents.size}/>
        <DataToggle><PivotTable periods={measuredDays.map((row: AnyData) => row.date)} totalLabel={`가중 누계(${data.unit})`} rows={[
          { key: "value", label: `원단위(${data.unit})`, values: measuredDays.map((row: AnyData) => row.value), total: dailyWeightedTotal,
            format: value => value == null ? "-" : fmt(Number(value), 2),
            // 비가동일은 값이 튀므로 흐리게 — 평일과 같은 무게로 읽히지 않게 한다.
            className: (_value, index) => index >= 0 && !isOperating(measuredDays[index]) ? "off-day" : undefined },
          { key: "productionTon", label: "생산량(ton)", values: measuredDays.map((row: AnyData) => row.productionTon), total: Math.round(dailyTonTotal * 10) / 10,
            className: (_value, index) => index >= 0 && !isOperating(measuredDays[index]) ? "off-day" : undefined },
        ]}/></DataToggle></article>
      <article className="card chart-card wide"><CardTitle title={`${data.year}년 원단위 추이${showCumulative ? " · 누계" : ""}`} meta={data.unit}><label className="check-toggle"><input type="checkbox" checked={showCumulative} onChange={event => setShowCumulative(event.target.checked)}/>누계 추이 보기</label><CsvButton filename={`intensity_monthly_${metric}_${data.year}`} rows={monthlySeries} columns={["month","previous","target","current"]} labels={{month:"월",previous:`전년(${data.unit})`,target:`목표(${data.unit})`,current:`금년(${data.unit})`}}/></CardTitle>
        <Chart><LineChart data={monthlySeries}><CartesianGrid vertical={false}/><XAxis dataKey="month"/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/>{!monthlyLegend.isHidden("previous") && <Line type="linear" dataKey="previous" name="전년" stroke={palette.previous} strokeWidth={2} dot={seriesDot(palette.previous)} connectNulls/>}{!monthlyLegend.isHidden("target") && <Line type="linear" dataKey="target" name="목표" stroke={palette.target} strokeWidth={2} strokeDasharray="5 4" dot={false} connectNulls/>}{!monthlyLegend.isHidden("current") && <Line type="linear" dataKey="current" name="금년" stroke={metricColor} strokeWidth={2} dot={seriesDot(metricColor)} activeDot={{ r: 5 }} connectNulls/>}</LineChart></Chart>
        <ToggleLegend items={[{key:"previous",label:"전년",color:palette.previous},{key:"target",label:"목표",color:palette.target},{key:"current",label:"금년",color:metricColor}]} hidden={monthlyLegend.hidden} onToggle={monthlyLegend.toggle}/>
        <DataToggle><PivotTable periods={yoyRows.map((row: AnyData) => row.month)} totalLabel={showMonthlyTotal ? `누계(1~${cumulative.lastMonth}월)·가중` : "-"} rows={[
          { key: "previous", label: "전년", values: yoyRows.map((row: AnyData) => row.previous), total: showMonthlyTotal ? cumulative.previous : null, format: value => value == null ? "-" : fmt(Number(value), 2) },
          { key: "current", label: "금년", values: yoyRows.map((row: AnyData) => row.current), total: showMonthlyTotal ? cumulative.current : null, format: value => value == null ? "-" : fmt(Number(value), 2) },
          { key: "change", label: "증감률(%)", values: yoyRows.map((row: AnyData) => row.change), total: showMonthlyTotal ? cumulative.change : null,
            format: value => value == null ? "-" : `${Number(value) > 0 ? "+" : ""}${fmt(Number(value))}`,
            className: value => value == null ? undefined : Number(value) > 0 ? "bad" : "good" },
        ]}/><p className="quad-caption">누계 추이 보기는 각 월을 1월부터의 가중 평균(Σ사용량 ÷ Σ생산톤)으로 다시 그립니다.</p></DataToggle></article>
      {bridge && <article className="card chart-card span-all">
        <CardTitle title="원단위 변동 원인" meta={`${bridgeScope === "ytd" ? "연 누계" : "당월"} · 전년 동기 대비 · ${data.unit}`}/>
        <div className="segmented" role="group" aria-label="원인분해 기간">
          {([["ytd", "연 누계"], ["mtd", "당월"]] as const).map(([id, label]) => <button type="button" key={id} className={bridgeScope === id ? "active" : ""} aria-pressed={bridgeScope === id} onClick={() => setBridgeScope(id)}>{label}</button>)}
        </div>
        <p className="quad-caption">원단위는 사용량 ÷ 생산량이라 값만 봐서는 어느 쪽 때문에 변했는지 알 수 없습니다. 두 효과의 합은 전체 변동과 정확히 일치합니다.</p>
        <BridgeBars bridge={bridge} unit={data.unit}/>
        <div className="table-wrap"><table>
          <thead><tr><th>구분</th><th>전년 동기</th><th>금년</th><th>증감</th></tr></thead>
          <tbody>
            <tr><td>사용량</td><td>{fmt(bridge.usagePrev)}</td><td>{fmt(bridge.usageCurr)}</td><td className={Number(bridge.usageChange) > 0 ? "bad" : "good"}>{bridge.usageChange == null ? "-" : `${Number(bridge.usageChange) > 0 ? "+" : ""}${fmt(bridge.usageChange)}%`}</td></tr>
            <tr><td>생산량(ton)</td><td>{fmt(bridge.tonPrev)}</td><td>{fmt(bridge.tonCurr)}</td><td className={Number(bridge.tonChange) >= 0 ? "good" : "bad"}>{bridge.tonChange == null ? "-" : `${Number(bridge.tonChange) > 0 ? "+" : ""}${fmt(bridge.tonChange)}%`}</td></tr>
            <tr className="total-row"><td>원단위({data.unit})</td><td>{fmt(bridge.previous, 2)}</td><td>{fmt(bridge.current, 2)}</td><td className={bridge.current > bridge.previous ? "bad" : "good"}>{`${bridge.current > bridge.previous ? "+" : ""}${fmt(bridge.current - bridge.previous, 2)}`}</td></tr>
          </tbody>
        </table></div>
      </article>}
      <article className="card table-card span-all"><CardTitle title="공장 효율 매트릭스" meta="MTD 기준"><CsvButton filename={`intensity_matrix_${metric}`} rows={data.matrix} columns={["factory","current","previous","change"]} labels={{factory:"공장",current:`금년(${data.unit})`,previous:`전년(${data.unit})`,change:"증감률(%)"}}/></CardTitle><DataTable rows={data.matrix} columns={["factory","current","previous","change"]} labels={{factory:"공장",current:"금년",previous:"전년",change:"증감률(%)"}}/><p className="quad-caption">{WIP_FOOTNOTE}</p></article>
    </section></>;
}

function Production({ data, factory, date, mode, onModeChange, rangeFrom, rangeTo, onRangeChange }: {
  data: AnyData; factory: string; date: string; mode: ProductionMode; onModeChange: (mode: ProductionMode) => void;
  rangeFrom: string; rangeTo: string; onRangeChange: (from: string, to: string) => void;
}) {
  const s = data.summary ?? {};
  const planAllowed = data.planAllowed !== false;
  const periodLabel = data.dateFrom && data.dateTo ? `${data.dateFrom} ~ ${data.dateTo}` : "";
  const trendTitle = mode === "year" ? "월별 생산량 (제품유형별)" : "제품유형별 일일 생산량";
  // 품목 순위 탭 (legacy 계획 미달/초과 Top 탭) — 계획 유효 기간에만 미달/초과 노출
  const [rankTab, setRankTab] = useState<"top" | "under" | "over">("top");
  const gapAvailable = planAllowed && ((data.underItems?.length ?? 0) > 0 || (data.overItems?.length ?? 0) > 0);
  const activeRankTab = gapAvailable ? rankTab : "top";
  const rankRows: AnyData[] = activeRankTab === "top" ? data.topItems ?? [] : activeRankTab === "under" ? data.underItems ?? [] : data.overItems ?? [];
  const rankTitle = activeRankTab === "top" ? "주요 품목 계획 대비 실적" : activeRankTab === "under" ? "계획 미달 Top" : "계획 초과 Top";
  // 연간 모드 — 생산계획은 주 단위로 수립·집계되므로 연 누계(Burn-up)가 아니라
  // 월별 계획 대비 실적으로 본다. 진행 중인 달은 부분 실적이라 별도로 알린다.
  const monthlyPlanLegend = useSeriesToggle();
  const monthlyPlanRows: AnyData[] = data.monthlyPlan ?? [];
  const monthlyPlanItems: LegendItem[] = [
    { key: "plan", label: "계획", color: palette.previous },
    { key: "actual", label: "실적", color: "var(--chart-production)" },
    { key: "rate", label: "달성률(%)", color: palette.actual },
  ];
  const partialMonth = monthlyPlanRows.find((row: AnyData) => row.partial)?.month ?? "";
  const planTotals = monthlyPlanRows.reduce(
    (acc: { plan: number; actual: number }, row: AnyData) => row.rate == null ? acc
      : { plan: acc.plan + (row.plan ?? 0), actual: acc.actual + (row.actual ?? 0) },
    { plan: 0, actual: 0 },
  );
  // 생산 차트의 x축 라벨은 모드마다 다르다(월간 07.12 / 기간별 ISO / 연간 7월) —
  // 이벤트 마커도 같은 포맷으로 맞춰야 축에 붙는다.
  const productionEvents = groupEventsByLabel(
    useFieldEvents(factory, data.dateFrom ?? "", data.dateTo ?? ""), "production",
    mode === "year" ? monthLabelOf : mode === "range" ? (iso: string) => iso : dayLabelOf,
  );
  // 상단 필터로 선택한 공장이 실제로 생산하지 않는 제품유형(예: 광주는 IC·MY 미생산)은
  // 범례·차트·데이터 표 어디에도 노출하지 않는다 — 전 기간 값이 전부 0/공백이면 제외.
  const cat2ActiveKeys = (["IC", "MY", "FM", "SN", "ETC"] as const).filter(key => (data.daily ?? []).some((row: AnyData) => Number(row[key]) > 0));
  const productionLegend = useSeriesToggle();
  // 광주 전용 — 자사 완제품 실적만으로는 빠지는 판매용 반제품(탈지분유·생크림 등)까지
  // 합산한, 원단위 분모와 동일한 정의의 실질 생산량. 백엔드가 값을 채워줬을 때만 노출한다.
  const showUtilityProd = factory === "광주" && (data.daily ?? []).some((row: AnyData) => row.utilityProd != null);
  const productionLegendItems: LegendItem[] = [
    ...cat2ActiveKeys.map(key => ({ key, label: cat2Labels[key] ?? key, color: palette.cat2[key] })),
    ...(showUtilityProd ? [{ key: "utilityProd", label: "유틸리티 사용 총 생산량", color: "var(--chart-production)" }] : []),
  ];
  const csvColumns = ["date", "IC", "MY", "FM", "SN", "ETC", ...(showUtilityProd ? ["utilityProd"] : [])];
  const csvLabels = { date: mode === "year" ? "월" : "일자", IC: "IC(ton)", MY: "MY(ton)", FM: "FM(ton)", SN: "SN(ton)", ETC: "기타(ton)", ...(showUtilityProd ? { utilityProd: "유틸리티 사용 총 생산량(ton)" } : {}) };
  const productionPivotRows: PivotRow[] = [
    ...cat2ActiveKeys.flatMap<PivotRow>(key => {
      const numbersOf = (field: string): (number | null)[] =>
        (data.daily ?? []).map((row: AnyData) => (typeof row[field] === "number" ? row[field] : null));
      const sumOf = (values: (number | null)[]) => values.reduce<number>((acc, value) => acc + (value ?? 0), 0);
      const rowValues = numbersOf(key);
      const total = sumOf(rowValues);
      const currentRow: PivotRow = { key, label: cat2Labels[key] ?? key, values: rowValues, total };
      // 연간 모드에서는 유형마다 전년·증감률 행을 붙여 유형별 전년비를 표에서도 읽게 한다.
      if (mode !== "year") return [currentRow];
      const previousValues = numbersOf(`prev${key}`);
      const previousTotal = sumOf(previousValues);
      return [
        currentRow,
        { key: `prev${key}`, label: "└ 전년", values: previousValues, total: previousTotal },
        {
          key: `chg${key}`, label: "└ 증감률(%)",
          values: rowValues.map((value, index) => {
            const previous = previousValues[index];
            return value == null || previous == null || previous === 0 ? null : Math.round((value / previous - 1) * 1000) / 10;
          }),
          total: previousTotal > 0 ? Math.round((total / previousTotal - 1) * 1000) / 10 : null,
          format: (value: unknown) => value == null ? "-" : `${Number(value) > 0 ? "+" : ""}${fmt(Number(value))}`,
          className: (value: unknown) => value == null ? undefined : Number(value) >= 0 ? "good" : "bad",
        },
      ];
    }),
    ...(showUtilityProd ? [(() => {
      const rowValues = (data.daily ?? []).map((row: AnyData) => (typeof row.utilityProd === "number" ? row.utilityProd : null));
      const total = rowValues.reduce((acc: number, v: number | null) => acc + (v ?? 0), 0);
      return { key: "utilityProd", label: "유틸리티 사용 총 생산량(ton)", values: rowValues, total };
    })()] : []),
  ];
  // 연간 모드에서는 상단 2열(월별 계획비 옆)에, 그 외 모드에서는 유형별 차트 아래에 놓는다.
  const itemRankingCard = <article className="card table-card"><CardTitle title={rankTitle} meta={`${s.items ?? 0}개 품목`}><CsvButton filename={`item_ranking_${activeRankTab}_${mode}_${(data.dateFrom ?? "").replaceAll("-", "")}`} rows={rankRows} columns={activeRankTab === "top" ? ["name", "plan", "actual", "rate"] : ["name", "plan", "actual", "variance", "rate"]} labels={{ name: "품목", plan: "계획(ton)", actual: "실적(ton)", variance: "편차(ton)", rate: "달성률(%)" }}/></CardTitle>
    {gapAvailable && <div className="segmented" role="group" aria-label="품목 순위 구분">{([["top","실적 Top"],["under","미달 Top"],["over","초과 Top"]] as const).map(([id,label]) => <button type="button" key={id} className={activeRankTab === id ? "active" : ""} aria-pressed={activeRankTab === id} onClick={() => setRankTab(id)}>{label}</button>)}</div>}
    {activeRankTab === "top"
      ? <DataTable rows={rankRows} columns={["name", "plan", "actual", "rate"]} labels={{ name: "품목", plan: "계획", actual: "실적", rate: "달성률(%)" }}/>
      : <div className="table-wrap"><table><thead><tr><th>품목</th><th>계획</th><th>실적</th><th>편차</th><th>달성률(%)</th></tr></thead><tbody>
          {rankRows.map((row: AnyData, index: number) => <tr key={index}><td>{row.name}</td><td>{fmt(row.plan)}</td><td>{fmt(row.actual)}</td><td className={Number(row.variance) < 0 ? "bad" : "good"}>{Number(row.variance) > 0 ? "+" : ""}{fmt(row.variance)}</td><td>{row.rate == null ? "-" : fmt(row.rate)}</td></tr>)}
          {rankRows.length === 0 && <tr><td colSpan={5}>{activeRankTab === "under" ? "계획 대비 미달 품목이 없습니다." : "계획 대비 초과 품목이 없습니다."}</td></tr>}
        </tbody></table></div>}
  </article>;
  return <>
    <div className="mode-row">
      <div className="segmented" role="group" aria-label="생산실적 조회 모드">{productionModes.map(item => <button type="button" key={item.id} className={mode === item.id ? "active" : ""} aria-pressed={mode === item.id} onClick={() => onModeChange(item.id)}>{item.label}</button>)}</div>
      {mode === "range" && <div className="range-fields">
        <label><span>시작일</span><input type="date" value={rangeFrom} max={rangeTo} onChange={event => onRangeChange(event.target.value, rangeTo)}/></label>
        <label><span>종료일</span><input type="date" value={rangeTo} min={rangeFrom} onChange={event => onRangeChange(rangeFrom, event.target.value)}/></label>
      </div>}
      {periodLabel && <span className="period-chip">{periodLabel} · {fmt(s.days, 0)}일</span>}
    </div>
    {mode === "range" && !planAllowed && <div className="info-note">기간별 계획 대비 지표는 선택 범위가 완전한 월(1일~말일)로 구성될 때만 표시합니다.</div>}
    <section className="kpi-grid">
      <Kpi label={mode === "year" ? "연계획" : "누계 계획"} value={planAllowed ? s.plan : "N/A"} unit={planAllowed ? "ton" : undefined} icon={CalendarDays}/>
      <Kpi label="누계 실적" value={s.actual} unit="ton" goodWhen="up" icon={Factory}/>
      <Kpi label="계획 달성률" value={planAllowed ? s.progress : "N/A"} unit={planAllowed ? "%" : undefined} icon={Gauge}/>
      <Kpi label={mode === "year" ? "연말 예상 실적" : mode === "month" ? "월말 예상 실적" : "예상 실적"} value={mode === "range" ? "N/A" : s.forecast} unit={mode === "range" ? undefined : "ton"} icon={PackageCheck}/>
    </section>
    {(data.insights?.length ?? 0) > 0 && <section className="card insight-list">{data.insights.map((message: string, index: number) => <p key={index}>{message}</p>)}</section>}
    <section className="content-grid">
      {/* 연간 모드 상단 — 월별 계획 대비 실적(Burn-up 대체)과 품목 계획 대비를 한 행에 */}
      {mode === "year" && monthlyPlanRows.length > 0 && <div className="quad-grid span-all">
        <article className="card chart-card"><CardTitle title="월별 계획 대비 실적" meta="ton · 달성률(%)"><CsvButton filename={`production_monthly_plan_${(data.dateFrom ?? "").slice(0,4)}`} rows={monthlyPlanRows} columns={["month","plan","actual","rate"]} labels={{month:"월",plan:"계획(ton)",actual:"실적(ton)",rate:"달성률(%)"}}/></CardTitle>
          <ToggleLegend items={monthlyPlanItems} hidden={monthlyPlanLegend.hidden} onToggle={monthlyPlanLegend.toggle}/>
          <Chart><ComposedChart data={monthlyPlanRows}><CartesianGrid vertical={false}/><XAxis dataKey="month"/><YAxis yAxisId="ton"/><YAxis yAxisId="rate" orientation="right" unit="%"/><Tooltip {...tooltipStyle} formatter={numberFormatter}/>
            {!monthlyPlanLegend.isHidden("plan") && <Bar yAxisId="ton" dataKey="plan" name="계획" fill={palette.previous} radius={[4,4,0,0]} maxBarSize={22}/>}
            {!monthlyPlanLegend.isHidden("actual") && <Bar yAxisId="ton" dataKey="actual" name="실적" fill="var(--chart-production)" radius={[4,4,0,0]} maxBarSize={22}/>}
            {!monthlyPlanLegend.isHidden("rate") && <Line yAxisId="rate" type="linear" dataKey="rate" name="달성률(%)" stroke={palette.actual} strokeWidth={2} dot={seriesDot(palette.actual)} connectNulls={false}/>}
            <ReferenceLine yAxisId="rate" y={100} stroke="var(--muted)" strokeDasharray="4 4"/>
          </ComposedChart></Chart>
          {partialMonth && <p className="quad-caption">※ {partialMonth}은 기준일까지의 실적이라 달성률이 낮게 보입니다 — 월 마감 후 다시 확인하세요.</p>}
          <DataToggle><PivotTable periods={monthlyPlanRows.map((row: AnyData) => row.month)} periodLabel="월" totalLabel="누계" rows={[
            { key: "plan", label: "계획(ton)", values: monthlyPlanRows.map((row: AnyData) => row.plan), total: monthlyPlanRows.reduce((acc: number, row: AnyData) => acc + (row.plan ?? 0), 0) },
            { key: "actual", label: "실적(ton)", values: monthlyPlanRows.map((row: AnyData) => row.actual), total: monthlyPlanRows.reduce((acc: number, row: AnyData) => acc + (row.actual ?? 0), 0) },
            { key: "rate", label: "달성률(%)", values: monthlyPlanRows.map((row: AnyData) => row.rate),
              total: planTotals.plan > 0 ? Math.round(planTotals.actual / planTotals.plan * 1000) / 10 : null,
              format: value => value == null ? "-" : fmt(Number(value)),
              className: value => value == null ? undefined : Number(value) >= 100 ? "good" : "bad" },
          ]}/></DataToggle>
        </article>
        {itemRankingCard}
      </div>}
      <article className="card chart-card span-all"><CardTitle title={trendTitle} meta={mode === "year" ? "ton · 금년 vs 전년" : "ton"}><CsvButton filename={`production_${mode}_${(data.dateFrom ?? "").replaceAll("-", "")}`} rows={data.daily} columns={csvColumns} labels={csvLabels}/></CardTitle>
        <ToggleLegend items={productionLegendItems} hidden={productionLegend.hidden} onToggle={productionLegend.toggle}/>
        {mode === "year" && <p className="quad-caption">막대 한 쌍은 왼쪽이 금년, 오른쪽(옅은 색)이 전년입니다. 유형별 합이 곧 총 생산량이라 별도 총량 차트 없이 유형별 전년비를 함께 읽을 수 있습니다.</p>}
        <Chart><ComposedChart data={data.daily}><CartesianGrid vertical={false}/><XAxis dataKey="date" interval="preserveStartEnd" minTickGap={18}/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/>
          {cat2ActiveKeys.filter(key => !productionLegend.isHidden(key)).map((key, index, visible) => <Bar key={key} dataKey={key} name={`${cat2Labels[key] ?? key} 금년`} stackId="a" fill={palette.cat2[key]} stroke="var(--card)" strokeWidth={1} maxBarSize={22} radius={index === visible.length - 1 ? [4,4,0,0] : undefined}/>)}
          {mode === "year" && cat2ActiveKeys.filter(key => !productionLegend.isHidden(key)).map((key, index, visible) => <Bar key={`prev${key}`} dataKey={`prev${key}`} name={`${cat2Labels[key] ?? key} 전년`} stackId="p" fill={palette.cat2[key]} fillOpacity={0.42} stroke="var(--card)" strokeWidth={1} maxBarSize={22} radius={index === visible.length - 1 ? [4,4,0,0] : undefined}/>)}
          {showUtilityProd && !productionLegend.isHidden("utilityProd") && <Line type="linear" dataKey="utilityProd" name="유틸리티 사용 총 생산량" stroke="var(--chart-production)" strokeWidth={2} dot={seriesDot("var(--chart-production)")} activeDot={{ r: 5 }} connectNulls/>}
          {eventMarkers(productionEvents)}</ComposedChart></Chart><EventMarkerHint count={productionEvents.size}/>
        <DataToggle><PivotTable periods={(data.daily ?? []).map((row: AnyData) => row.date)} rows={productionPivotRows} totalLabel="누계(ton)"/></DataToggle></article>
      {mode !== "year" && itemRankingCard}
      <article className="card list"><CardTitle title="제품 믹스" meta="구성비"/>{data.mix?.map((r: AnyData) => <div className="progress" key={r.name}><div><span>{cat2Labels[r.name] ?? r.name}</span><b>{fmt(r.value)}%</b></div><i><em style={{ width: `${r.value}%` }}/></i></div>)}
        {(data.wipMix?.length ?? 0) > 0 && <div className="sub-section">
          <p className="quad-caption">재공품 믹스 · 판매용 반제품(탈지분유·살균유 등) 구성비</p>
          {data.wipMix.map((r: AnyData) => <div className="progress" key={r.name}><div><span>{r.name}</span><b>{fmt(r.value)}%</b></div><i><em style={{ width: `${r.value}%`, background: "linear-gradient(90deg,var(--chart-amber),#b45309)" }}/></i></div>)}
        </div>}
      </article>
      <ProductionItemTrend factory={factory} date={date} mode={mode} rangeFrom={rangeFrom} rangeTo={rangeTo}/>
      <ProductionItemYoy factory={factory} date={date} mode={mode} rangeFrom={rangeFrom} rangeTo={rangeTo}/>
    </section>
  </>;
}

const diagnosableFactories = ["남양주1", "남양주2", "김해", "광주", "논산"];
// v5.3 모델 학습 대상 — 경산(2026-07 신규)은 학습 데이터가 없어 예측을 제공하지 못한다.
const predictableFactories = ["전사", "남양주", "남양주1", "남양주2", "김해", "광주", "논산"];

// 남양주는 1·2 공장의 집계라 평면 목록에서는 관계가 드러나지 않는다 — optgroup으로 계층 표현.
const FACTORY_GROUPS: { label: string; options: string[] }[] = [
  { label: "", options: ["전사"] },
  { label: "남양주 (집계)", options: ["남양주", "남양주1", "남양주2"] },
  { label: "개별 공장", options: ["김해", "광주", "논산", "경산"] },
];

// 현업 화면 어디서든 지금 보는 데이터가 며칠 자인지 알 수 있게 — 지금까지는
// 관리자 탭에서만 동기화 상태를 볼 수 있었다.
function DataFreshnessBadge() {
  const [status, setStatus] = useState<AnyData | null>(null);
  useEffect(() => {
    const abort = new AbortController();
    apiGet<AnyData>("/data-status", {}, abort.signal)
      .then(result => { if (result.live) setStatus(result.data); })
      .catch(() => {});
    return () => abort.abort();
  }, []);
  if (!status?.energy?.lastDate) return null;
  const lagging: AnyData[] = status.laggingFactories ?? [];
  const stale = Boolean(status.energy?.stale || status.production?.stale) || lagging.length > 0;
  const label = (entry: AnyData) => String(entry?.lastDate ?? "-").slice(5).replace("-", ".");
  const laggingText = lagging.map(item => `${item.factory} ${String(item.lastDate).slice(5)}까지`).join(" · ");
  return <span className={`freshness${stale ? " stale" : ""}`}
    title={[
      `에너지 ${status.energy?.lastDate ?? "-"} (${status.energy?.lagDays ?? "-"}일 전)`,
      `생산 ${status.production?.lastDate ?? "-"} (${status.production?.lagDays ?? "-"}일 전)`,
      ...(lagging.length ? [`원본 미도착: ${laggingText} — 최신일 집계·메일에서 제외됩니다.`] : []),
    ].join("\n")}>
    <Database size={13}/>에너지 {label(status.energy)} · 생산 {label(status.production)}
    {lagging.length > 0 && <em className="freshness-gap">{lagging.length}개 공장 지연</em>}
  </span>;
}

// 예측 화면은 현업(오늘 이상인가·왜인가)과 모델 운영자(버전·정확도·변수)의 관심사가
// 다르다. 위쪽은 현업 판단에 필요한 것만 두고, 운영 정보는 접이식으로 내린다.
function Prediction({ data, factory, date, isAdmin }: { data: AnyData; factory: string; date: string; isAdmin: boolean }) {
  const diagnosable = diagnosableFactories.includes(factory);
  const status: AnyData = data.status ?? {};
  const signals: AnyData[] = data.signals ?? [];
  return <>
    <section className="kpi-grid compact">
      <Kpi label="반복 이탈" value={status.repeated ?? 0} unit="건" icon={Activity}/>
      <Kpi label="지속 편차" value={status.drift ?? 0} unit="건" icon={Gauge}/>
      <Kpi label="판정" value={status.label} icon={ShieldCheck}/>
    </section>
    <article className="card insight-list">
      <CardTitle title="조치 대상" meta="최근 7일 · 판정 규칙 적용"/>
      {signals.length === 0
        ? <p className="panel-copy">반복 이탈과 지속 편차가 없습니다. 하루만 벗어난 건은 정상범주(90%) 특성상 흔해 조치 대상이 아닙니다.</p>
        : <ul className="alert-signals">
            {signals.map((signal, index) => <li key={index}>
              <em className={signal.kind === "alert" ? "sig-alert" : "sig-drift"}>{signal.label}</em>
              <b>{signal.factory} {signal.target}</b>
              <span>{signal.detail}</span>
            </li>)}
          </ul>}
    </article>
    <PredictionGap factory={factory} date={date}/>
    <PredictionHistory rows={data.latest ?? []} factory={factory} isAdmin={isAdmin} diagnosable={diagnosable}/>
    <article className="card ops-panel">
      <details>
        <summary>모델 운영 정보 — 버전 · 성능 · 변수 영향도</summary>
        <div className="ops-body">
          <section className="model-banner">
            <div><BrainCircuit/><span>운영 모델</span><strong>{data.model?.version}</strong></div>
            <div><span>상태</span><strong>{data.model?.state}</strong></div>
            <div><span>최근 학습</span><strong>{data.model?.trainedAt}</strong></div>
          </section>
          {diagnosable ? <FeatureImportance factory={factory}/> : <div className="info-note">모델 변수 영향도는 개별 공장(남양주1·남양주2·김해·광주·논산) 선택 시 표시됩니다.</div>}
          <PredictionMonitoring factory={factory}/>
          {isAdmin && <PredictionRunner factory={factory} date={date} isAdmin={isAdmin}/>}
        </div>
      </details>
    </article>
  </>;
}

function CardTitle({ title, meta, children }: { title: string; meta: string; children?: React.ReactNode }) { return <header className="card-title"><h3>{title}</h3><div className="card-title-side">{children}<span>{meta}</span></div></header> }
function Chart({ children }: { children: React.ReactElement }) { return <div className="chart"><ResponsiveContainer width="100%" height="100%">{children}</ResponsiveContainer></div> }
function DataTable({ rows=[], columns, labels }: { rows?: AnyData[]; columns:string[]; labels:AnyData }) { return <div className="table-wrap"><table><thead><tr>{columns.map(c=><th key={c}>{labels[c]??c}</th>)}</tr></thead><tbody>{rows.map((r,i)=><tr key={i}>{columns.map(c=><td key={c}>{typeof r[c]==="number"?fmt(r[c]):r[c]??"-"}</td>)}</tr>)}</tbody></table></div> }

export function BemsApp() {
  const [screen,setScreen]=useState<Screen>("dashboard"), [factory,setFactory]=useState("전사"), [date,setDate]=useState(localYesterday), [mobile,setMobile]=useState(false);
  const [intensityMetric,setIntensityMetric]=useState<IntensityMetric>("power");
  // 실제 테마는 layout.tsx 인라인 스크립트가 첫 페인트 전에 <html data-theme>로 적용한다.
  // 이 상태는 토글 아이콘 표시용이며, SSR 불일치를 피하려고 mount 후 동기화한다.
  const [theme,setTheme]=useState<"light"|"dark">("light");
  useEffect(()=>{setTheme(document.documentElement.dataset.theme==="dark"?"dark":"light")},[]);
  const toggleTheme=()=>{
    const next=theme==="light"?"dark":"light";
    document.documentElement.dataset.theme=next;
    try{localStorage.setItem("bems-theme",next)}catch{}
    setTheme(next);
  };
  const [productionMode,setProductionMode]=useState<ProductionMode>("month");
  const [productionRange,setProductionRange]=useState<{from:string;to:string}>(()=>{
    const today=localYesterday();
    const from=new Date(Date.parse(`${today}T00:00:00`)-30*86_400_000);
    return {from:`${from.getFullYear()}-${String(from.getMonth()+1).padStart(2,"0")}-${String(from.getDate()).padStart(2,"0")}`,to:today};
  });
  const [energyMode,setEnergyMode]=useState<EnergyMode>("recent");
  const [energyRange,setEnergyRange]=useState<{from:string;to:string}>(()=>{
    const today=localYesterday();
    const from=new Date(Date.parse(`${today}T00:00:00`)-30*86_400_000);
    return {from:`${from.getFullYear()}-${String(from.getMonth()+1).padStart(2,"0")}-${String(from.getDate()).padStart(2,"0")}`,to:today};
  });
  const [intensityMode,setIntensityMode]=useState<EnergyMode>("recent");
  const [intensityRange,setIntensityRange]=useState<{from:string;to:string}>(()=>{
    const today=localYesterday();
    const from=new Date(Date.parse(`${today}T00:00:00`)-30*86_400_000);
    return {from:`${from.getFullYear()}-${String(from.getMonth()+1).padStart(2,"0")}-${String(from.getDate()).padStart(2,"0")}`,to:today};
  });
  const [data,setData]=useState<AnyData>(demo.dashboard), [session,setSession]=useState<AnyData>(demo.session), [sessionLive,setSessionLive]=useState(false), [live,setLive]=useState(false), [loading,setLoading]=useState(true);
  const [pageVisibility,setPageVisibility]=useState<Record<string,boolean>>(DEFAULT_PAGE_VISIBILITY);
  const fallback=useMemo(()=>{
    if (!isDataScreen(screen)) return {};
    const selected=screenFallback[screen];
    return screen==="intensity" ? {...selected,metric:intensityMetric,unit:intensityUnits[intensityMetric]} : selected;
  },[screen,intensityMetric]);
  useEffect(()=>{apiGet("/session",demo.session).then(r=>{setSession(r.data);setSessionLive(r.live)});},[]);
  useEffect(()=>{apiGet("/settings/page-visibility",DEFAULT_PAGE_VISIBILITY).then(r=>setPageVisibility(r.data));},[]);
  // 관리자에게는 노출 설정과 무관하게 항상 전체 메뉴를 보여준다 — 잘못 숨겨도 스스로 복구 가능해야 하므로.
  const visibleMenus=useMemo(()=>session.role==="admin"?menus:menus.filter(item=>pageVisibility[item.id]!==false),[session.role,pageVisibility]);
  // 조회 사용자가 보던 화면이 방금 숨김 처리되면(다른 세션의 관리자가 설정 변경) 첫 노출 메뉴로 이동.
  useEffect(()=>{
    if (session.role==="admin"||visibleMenus.length===0) return;
    if (!visibleMenus.some(item=>item.id===screen)) setScreen(visibleMenus[0].id);
  },[session.role,visibleMenus,screen]);
  useEffect(()=>{
    if (!isDataScreen(screen)) { setLoading(false); return; }
    const controller=new AbortController();
    setLoading(true);
    const suffix=query({
      factory,date,
      ...(screen==="intensity"?{metric:intensityMetric,...(intensityMode==="range"?{date_from:intensityRange.from,date_to:intensityRange.to}:{})}:{}),
      ...(screen==="production"?{mode:productionMode,...(productionMode==="range"?{date_from:productionRange.from,date_to:productionRange.to}:{})}:{}),
      ...(screen==="energy"&&energyMode==="range"?{date_from:energyRange.from,date_to:energyRange.to}:{}),
    });
    apiGet(`${endpoint[screen]}?${suffix}`,fallback,controller.signal).then(r=>{setData(r.data);setLive(r.live);setLoading(false)}).catch(()=>{});
    return()=>controller.abort();
  },[screen,factory,date,intensityMetric,intensityMode,intensityRange,productionMode,productionRange,energyMode,energyRange,fallback]);
  const [title,subtitle]=titles[screen];
  // P2-1 — 기간지정 모드에서는 상단 기준일이 차트에 반영되지 않는다. 활성 상태로 두면
  // 화면이 거짓 정보를 주므로 비활성 처리하고 이유를 함께 보여준다.
  const dateIgnored =
    (screen==="energy"&&energyMode==="range") ||
    (screen==="intensity"&&intensityMode==="range") ||
    (screen==="production"&&productionMode==="range");
  const dateLabel = screen==="production"&&productionMode!=="range" ? (productionMode==="year"?"기준 연도":"기준 월") : "기준일";
  const statusLive=isDataScreen(screen)?live:sessionLive;
  const statusTitle=isDataScreen(screen)?(live?"Local DB":"예시 데이터"):(sessionLive?"BEMS API":"API 연결 실패");
  const statusDetail=isDataScreen(screen)?(live?"MySQL 연결됨":"API 연결 실패"):(sessionLive?"권한 확인됨":"세션 확인 실패");
  return <div className="app-shell"><aside className={mobile?"open":""} aria-label="주 메뉴"><div className="brand"><div><Bolt size={21}/></div><span>AI ELITE<strong>BEMS NEXT</strong></span><button type="button" className="close" aria-label="메뉴 닫기" onClick={()=>setMobile(false)}><X/></button></div><nav>{visibleMenus.map(item=>{
      const Icon=item.icon;
      // 선택 공장에 학습된 모델이 없으면 예측 화면은 빈 화면이 된다 — 들어가기 전에 막고 이유를 알린다.
      const unsupported = item.id==="prediction" && !predictableFactories.includes(factory);
      return <button type="button" key={item.id} className={`${screen===item.id?"active":""}${unsupported?" unsupported":""}`} aria-current={screen===item.id?"page":undefined} disabled={unsupported}
        title={unsupported?`${factory}은 v5.3 모델 학습 대상이 아니어서 예측을 제공하지 않습니다.`:undefined}
        onClick={()=>{setScreen(item.id);setMobile(false)}}><Icon size={19}/><span>{item.label}</span><ChevronRight size={15}/></button>})}</nav><div className="side-status" role="status" aria-live="polite"><Database size={17}/><div><b>{statusTitle}</b><span>{statusDetail}</span></div><i className={statusLive?"on":""}/></div><footer>v1.0 · Internal Network</footer></aside>{mobile&&<button type="button" className="scrim" aria-label="메뉴 닫기" onClick={()=>setMobile(false)}/>}<main><header className="topbar"><button type="button" className="menu" aria-label="메뉴 열기" onClick={()=>setMobile(true)}><Menu/></button><div className="heading"><h1>{title}</h1><p>{subtitle}</p></div><div className="filters"><label><Building2 size={16}/><select aria-label="공장 선택" value={factory} onChange={e=>setFactory(e.target.value)}>
      {FACTORY_GROUPS.map(group => group.options.length === 1 && !group.label
        ? <option key={group.options[0]}>{group.options[0]}</option>
        : <optgroup key={group.label} label={group.label}>{group.options.map(f => <option key={f}>{f}</option>)}</optgroup>)}
    </select></label><label className={dateIgnored?"is-muted":""} title={dateIgnored?"기간 지정 모드에서는 아래 시작일·종료일이 적용됩니다.":undefined}><CalendarDays size={16}/><input type="date" aria-label={`${dateLabel} 선택`} value={date} disabled={dateIgnored} onChange={e=>setDate(e.target.value)}/>{dateIgnored&&<em className="filter-note">미적용</em>}</label><DataFreshnessBadge/><button type="button" className="theme-toggle" aria-label="화면 테마 전환" title={theme==="dark"?"라이트 모드로 전환":"다크 모드로 전환"} onClick={toggleTheme}>{theme==="dark"?<Sun size={16}/>:<Moon size={16}/>}</button><div className={`role ${session.role}`}><ShieldCheck size={16}/>{session.role==="admin"?"관리자":"조회 사용자"}</div></div></header><div className="mobile-title"><h1>{title}</h1><p>{subtitle}</p></div><div className="workspace" aria-busy={loading}>{loading?<div className="loading" role="status" aria-live="polite"><RefreshCw className="spin"/>데이터를 불러오는 중입니다.</div>:<>{!live&&isDataScreen(screen)&&<section className="data-warning" role="alert"><Database size={20}/><div><strong>API 연결 실패 · 예시 데이터 표시 중</strong><p>현재 화면의 수치는 데모 값이며 실제 운영 판단에 사용할 수 없습니다.</p></div></section>}{screen==="dashboard"&&<Dashboard data={data} factory={factory} date={date}/>} {screen==="energy"&&<Energy data={data} factory={factory} mode={energyMode} onModeChange={setEnergyMode} rangeFrom={energyRange.from} rangeTo={energyRange.to} onRangeChange={(from,to)=>{ if(from&&to&&from>to){ setEnergyRange({from:to,to}); } else { setEnergyRange({from,to}); } }}/>} {screen==="intensity"&&<Intensity data={data} factory={factory} metric={intensityMetric} onMetricChange={setIntensityMetric} mode={intensityMode} onModeChange={setIntensityMode} rangeFrom={intensityRange.from} rangeTo={intensityRange.to} onRangeChange={(from,to)=>{ if(from&&to&&from>to){ setIntensityRange({from:to,to}); } else { setIntensityRange({from,to}); } }}/>} {screen==="production"&&<Production data={data} factory={factory} date={date} mode={productionMode} onModeChange={setProductionMode} rangeFrom={productionRange.from} rangeTo={productionRange.to} onRangeChange={(from,to)=>{ if(from&&to&&from>to){ setProductionRange({from:to,to}); } else { setProductionRange({from,to}); } }}/>} {screen==="prediction"&&<Prediction data={data} factory={factory} date={date} isAdmin={session.role==="admin"}/>} {screen==="report"&&<ReportScreen factory={factory} date={date} isAdmin={session.role==="admin"}/>} {screen==="admin"&&<AdminScreen factory={factory} date={date} isAdmin={session.role==="admin"}/>}</>}</div></main></div>;
}
