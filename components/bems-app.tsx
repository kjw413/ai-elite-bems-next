"use client";

import { useEffect, useMemo, useState } from "react";
import { Activity, BarChart3, Bolt, BrainCircuit, Building2, CalendarDays, ChevronRight, Database, Download, Factory, FileText, Gauge, Mail, Menu, Moon, PackageCheck, RefreshCw, Settings, ShieldCheck, Sun, X } from "lucide-react";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, ComposedChart, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { apiGet, apiRequest, query } from "@/lib/bems-api";
import { downloadCsv } from "@/lib/bems-csv";
import { demo, factories } from "@/lib/bems-data";
import { AdminScreen } from "@/components/screens/admin-screen";
import { PredictionHistory } from "@/components/screens/prediction-history";
import { PredictionRunner } from "@/components/screens/prediction-runner";
import { ReportScreen } from "@/components/screens/report-screen";
import { SevenDayCompare } from "@/components/seven-day-compare";
import { FactoryYoy, IssuesCard } from "@/components/factory-yoy";
import { FeatureImportance } from "@/components/feature-importance";

type Screen = "dashboard" | "energy" | "intensity" | "production" | "prediction" | "report" | "admin";
type DataScreen = Exclude<Screen, "report" | "admin">;
type IntensityMetric = "power" | "fuel" | "water";
type AnyData = Record<string, any>;

const menus: { id: Screen; label: string; icon: typeof Activity }[] = [
  { id: "dashboard", label: "통합 대시보드", icon: BarChart3 },
  { id: "energy", label: "에너지 사용량", icon: Bolt },
  { id: "intensity", label: "에너지 원단위", icon: Gauge },
  { id: "production", label: "생산실적 분석", icon: PackageCheck },
  { id: "prediction", label: "AI 에너지 예측", icon: BrainCircuit },
  { id: "report", label: "AI 실적 보고서", icon: FileText },
  { id: "admin", label: "관리자·현장 메모", icon: Settings },
];

const titles: Record<Screen, [string, string]> = {
  dashboard: ["통합 에너지 대시보드", "실적과 AI 예측을 한눈에 확인합니다."],
  energy: ["에너지 사용량", "전력·연료·용수 사용 추이를 비교합니다."],
  intensity: ["에너지 원단위", "생산량 대비 에너지 효율을 추적합니다."],
  production: ["생산실적 분석", "계획 대비 생산성과 제품 믹스를 분석합니다."],
  prediction: ["AI 에너지 예측", "v5.3 예측값과 정상범주 이탈을 모니터링합니다."],
  report: ["AI 에너지 실적 보고서", "저장된 월간 보고서를 열람하고 생성합니다."],
  admin: ["관리자·현장 메모", "목표, 이벤트, 업로드와 예측 이력을 관리합니다."],
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
const tooltipStyle = { contentStyle: { borderRadius: 10, border: "1px solid var(--line)", background: "var(--card)", boxShadow: "0 6px 18px #1e315514", fontSize: 12 }, labelStyle: { color: "var(--text)" } };
const numberFormatter = (value: unknown) => typeof value === "number" ? value.toLocaleString("ko-KR", { maximumFractionDigits: 2 }) : Array.isArray(value) ? value.map(item => typeof item === "number" ? item.toLocaleString("ko-KR", { maximumFractionDigits: 2 }) : String(item)).join(" ~ ") : String(value ?? "-");

type ProductionMode = "month" | "range" | "year";
const productionModes: { id: ProductionMode; label: string }[] = [
  { id: "month", label: "월별" },
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

function localToday() {
  const today = new Date();
  const month = String(today.getMonth() + 1).padStart(2, "0");
  const day = String(today.getDate()).padStart(2, "0");
  return `${today.getFullYear()}-${month}-${day}`;
}

function Kpi({ label, value, unit, change, goodWhen = "down", icon: Icon = Activity }: { label: string; value: unknown; unit?: string; change?: number | null; goodWhen?: "down" | "up"; icon?: typeof Activity }) {
  const digits = unit === "ton/ton" ? 2 : 1;
  const isGood = goodWhen === "up" ? (change ?? 0) >= 0 : (change ?? 0) <= 0;
  return <article className="kpi card"><div className="kpi-icon"><Icon size={20}/></div><div><p>{label}</p><strong>{fmt(value, digits)} <small>{unit}</small></strong>{change != null && <span className={isGood ? "good" : "bad"}>{change > 0 ? "+" : ""}{fmt(change)}% 전년비</span>}</div></article>;
}

const mailPeriods = [
  { id: "daily", label: "일간" },
  { id: "weekly", label: "주간" },
  { id: "monthly", label: "월간" },
] as const;
type MailPeriod = (typeof mailPeriods)[number]["id"];

// legacy 대시보드 '📧 메일 송부'의 React 이식 — tools/mail 빌더·발송 파이프라인을
// 백엔드 API로 재사용한다. 관리자(호스트 PC)에게만 노출된다.
function MailPanel({ date }: { date: string }) {
  const [period, setPeriod] = useState<MailPeriod>("daily");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const help: Record<MailPeriod, string> = {
    daily: `기준일 ${date} 원단위 상세 · 즉시 점검 대상`,
    weekly: "직전 완결 주 (월~일, 전주비)",
    monthly: "직전 완결 월 (전년 동월비·YTD)",
  };
  async function send() {
    if (sending) return;
    const label = mailPeriods.find(item => item.id === period)?.label ?? period;
    if (!window.confirm(`${label} 에너지 리포트를 .env에 설정된 수신자에게 즉시 발송합니다. 계속하시겠습니까?`)) return;
    setSending(true); setError(""); setNotice("");
    try {
      const result = await apiRequest<{ label: string; refDate: string; recordCount: number; to: string[] }>("/mail/send", {
        method: "POST",
        body: JSON.stringify({ period, ...(period === "daily" ? { date } : {}) }),
      });
      setNotice(`${result.label} 메일 발송 완료 · 기준 ${result.refDate} · 공장 ${result.recordCount}개 · 수신 ${result.to.join(", ")}`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "메일 발송에 실패했습니다.");
    } finally {
      setSending(false);
    }
  }
  return <section className="card mail-bar">
    <div className="mail-bar-title"><Mail size={17}/><b>에너지 리포트 메일</b><small>{help[period]}</small></div>
    <div className="segmented" role="group" aria-label="메일 발송 주기">{mailPeriods.map(item => <button type="button" key={item.id} className={period === item.id ? "active" : ""} aria-pressed={period === item.id} onClick={() => setPeriod(item.id)}>{item.label}</button>)}</div>
    <button type="button" className="primary-button" disabled={sending} onClick={() => void send()}><Mail size={15}/>{sending ? "발송 중..." : "발송"}</button>
    {error && <div className="form-message error">{error}</div>}
    {notice && <div className="form-message success">{notice}</div>}
  </section>;
}

function Dashboard({ data, factory, date, isAdmin }: { data: AnyData; factory: string; date: string; isAdmin: boolean }) {
  const trend = (data.trend ?? []).map((row: AnyData) => ({
    ...row,
    band: row.lower != null && row.upper != null ? [row.lower, row.upper] : null,
  }));
  return <>{isAdmin && <MailPanel date={date}/>}
    <section className="kpi-grid">{data.metrics?.map((m: AnyData) => <Kpi key={m.id} label={m.label} value={m.value} unit={m.unit} change={m.change} goodWhen={m.id === "production" ? "up" : "down"} icon={m.id === "production" ? Factory : Bolt}/>)}</section>
    <section className={`alert ${data.alert?.level ?? "normal"}`}><BrainCircuit size={22}/><div><strong>{data.alert?.title}</strong><p>{data.alert?.description}</p></div></section>
    <section className="content-grid"><article className="card chart-card wide"><CardTitle title="최근 7일 전력 사용량" meta="MWh · AI P05~P95 정상범주"><CsvButton filename={`7day_trend_${factory}_${date}`} rows={data.trend} columns={["date","actual","predicted","lower","upper"]} labels={{date:"일자",actual:"실제(MWh)",predicted:"AI 예측(MWh)",lower:"P05(MWh)",upper:"P95(MWh)"}}/></CardTitle><Chart><AreaChart data={trend}><defs><linearGradient id="band" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={palette.band} stopOpacity={.22}/><stop offset="1" stopColor={palette.band} stopOpacity={.02}/></linearGradient></defs><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="date"/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/><Legend/><Area type="monotone" dataKey="band" name="P05~P95" stroke="none" fill="url(#band)" connectNulls={false}/><Line type="monotone" dataKey="predicted" name="AI 예측" stroke={palette.predicted} strokeDasharray="5 4" strokeWidth={2}/><Line type="monotone" dataKey="actual" name="실제" stroke={palette.actual} strokeWidth={3}/></AreaChart></Chart></article>
    <article className="card chart-card"><CardTitle title="공장별 전력 원단위" meta="낮을수록 효율적"/><Chart><BarChart data={data.factoryComparison} layout="vertical"><CartesianGrid strokeDasharray="3 3"/><XAxis type="number"/><YAxis dataKey="factory" type="category" width={58}/><Tooltip {...tooltipStyle} formatter={numberFormatter}/><Bar dataKey="value" name="kWh/ton" fill={palette.target} radius={[0,6,6,0]}/></BarChart></Chart></article>
    <article className="card chart-card"><CardTitle title="월별 전년 비교" meta="kWh/ton"><CsvButton filename={`yoy_power_${factory}_${date}`} rows={data.yoy} columns={["month","previous","current"]} labels={{month:"월","previous":"전년(kWh/ton)",current:"금년(kWh/ton)"}}/></CardTitle><Chart><LineChart data={data.yoy}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="month"/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/><Legend/><Line dataKey="previous" name="전년" stroke={palette.previous}/><Line dataKey="current" name="금년" stroke={palette.actual} strokeWidth={3}/></LineChart></Chart></article>
    <article className="card events"><CardTitle title="최근 현장 이벤트" meta={`${data.events?.length ?? 0}건`}/>{data.events?.map((event: AnyData) => <div className="event" key={event.id}><time>{event.date}</time><span>{event.factory}</span><div><b>{event.tag}</b><p>{event.note}</p></div></div>)}</article>
    <SevenDayCompare trend={data.trend ?? []} factory={factory} date={date}/>
    <FactoryYoy rows={data.yoyFactories ?? []} period={data.yoyPeriod} factory={factory} date={date}/>
    {(data.yoyFactories?.length ?? 0) > 0 && <IssuesCard rows={data.yoyFactories}/>}</section></>;
}

type EnergyMode = "recent" | "range";
const energyModes: { id: EnergyMode; label: string }[] = [
  { id: "recent", label: "최근 30일" },
  { id: "range", label: "기간 지정" },
];
const energyMetricLabels: Record<string, string> = { power: "전력", fuel: "연료", water: "용수", wastewater: "폐수" };

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

function Energy({ data, mode, onModeChange, rangeFrom, rangeTo, onRangeChange }: {
  data: AnyData; mode: EnergyMode; onModeChange: (mode: EnergyMode) => void;
  rangeFrom: string; rangeTo: string; onRangeChange: (from: string, to: string) => void;
}) {
  const [metric, setMetric] = useState("power"); const units: AnyData = { power: "MWh", fuel: "천 Nm³", water: "천 ton", wastewater: "천 ton" };
  const values = data.daily?.map((r: AnyData) => Number(r[metric]) || 0) ?? []; const total = values.reduce((a: number,b: number)=>a+b,0);
  const periodLabel = data.dateFrom && data.dateTo ? `${data.dateFrom} ~ ${data.dateTo}` : "";
  const summaryMeta = mode === "range" ? "선택 기간" : "이번 달";
  const yoyTable = buildYoyTable(data.yoy ?? [], metric);
  const yoyUnit = units[metric];
  const yoyCsvRows = [...yoyTable.rows, ...(yoyTable.total ? [yoyTable.total] : [])].map(row => ({
    month: row.month, previous: row.previous, current: row.current, diff: row.diff,
    diffPct: row.diffPct == null ? null : Math.round(row.diffPct * 10) / 10,
  }));
  return <><div className="segmented" role="group" aria-label="에너지 지표 선택">{Object.entries(energyMetricLabels).map(([id,label])=><button type="button" className={metric===id?"active":""} aria-pressed={metric===id} onClick={()=>setMetric(id)} key={id}>{label}</button>)}</div>
    <div className="mode-row">
      <div className="segmented" role="group" aria-label="사용량 조회 방식">{energyModes.map(item => <button type="button" key={item.id} className={mode === item.id ? "active" : ""} aria-pressed={mode === item.id} onClick={() => onModeChange(item.id)}>{item.label}</button>)}</div>
      {mode === "range" && <div className="range-fields">
        <label><span>시작일</span><input type="date" value={rangeFrom} max={rangeTo} onChange={event => onRangeChange(event.target.value, rangeTo)}/></label>
        <label><span>종료일</span><input type="date" value={rangeTo} min={rangeFrom} onChange={event => onRangeChange(rangeFrom, event.target.value)}/></label>
      </div>}
      {periodLabel && <span className="period-chip">{periodLabel}</span>}
    </div>
    <section className="kpi-grid compact"><Kpi label="기간 누계" value={total} unit={units[metric]} icon={Bolt}/><Kpi label="일평균" value={values.length?total/values.length:0} unit={units[metric]} icon={Activity}/><Kpi label="최대 사용량" value={values.length?Math.max(...values):0} unit={units[metric]} icon={Gauge}/></section><section className="content-grid"><article className="card chart-card wide"><CardTitle title={metric === "power" ? "일별 사용 추이 · 설비 분해" : "일별 사용 추이"} meta={units[metric]}><CsvButton filename={`energy_daily_${metric}`} rows={data.daily} columns={["date","power","freezing","compressor","other","fuel","water","wastewater"]} labels={{date:"일자",power:"전력(MWh)",freezing:"냉동(MWh)",compressor:"공압(MWh)",other:"기타(MWh)",fuel:"연료(천 Nm³)",water:"용수(천 ton)",wastewater:"폐수(천 ton)"}}/></CardTitle><Chart>{metric === "power"
      ? <ComposedChart data={data.daily}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="date" interval="preserveStartEnd" minTickGap={18}/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/><Legend/><Area type="monotone" dataKey="power" name="전체 전력" stroke={palette.actual} strokeWidth={3} fill="var(--chart-area-fill)"/><Line type="monotone" dataKey="freezing" name="냉동" stroke="var(--chart-amber)" strokeWidth={2} dot={false}/><Line type="monotone" dataKey="compressor" name="공압" stroke={palette.target} strokeWidth={2} dot={false}/><Line type="monotone" dataKey="other" name="기타" stroke={palette.previous} strokeWidth={2} strokeDasharray="4 3" dot={false}/></ComposedChart>
      : <AreaChart data={data.daily}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="date" interval="preserveStartEnd" minTickGap={18}/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/><Area type="monotone" dataKey={metric} stroke={palette.actual} strokeWidth={3} fill="var(--chart-area-fill)"/></AreaChart>}</Chart></article><article className="card list"><CardTitle title="설비 구성" meta={summaryMeta}/>{data.equipment?.map((r:AnyData)=><div className="progress" key={r.name}><div><span>{r.name}</span><b>{fmt(r.value)}%</b></div><i><em style={{width:`${r.value}%`}}/></i></div>)}</article>
    {(metric === "water" || metric === "wastewater") && <article className="card chart-card wide"><CardTitle title="공장별 폐수/용수 비율" meta={`${summaryMeta} · 낮을수록 처리 효율 양호`}><CsvButton filename={`wastewater_ratio_${data.dateFrom ?? ""}`} rows={(data.factories ?? []).map((r: AnyData) => ({...r, ratio: r.water > 0 ? Math.round(r.wastewater / r.water * 100) / 100 : null}))} columns={["factory","water","wastewater","ratio"]} labels={{factory:"공장",water:"용수(천 ton)",wastewater:"폐수(천 ton)",ratio:"폐수/용수"}}/></CardTitle><Chart><BarChart data={(data.factories ?? []).map((r: AnyData) => ({factory: r.factory, ratio: r.water > 0 ? Math.round(r.wastewater / r.water * 100) / 100 : null}))}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="factory"/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/><Bar dataKey="ratio" name="폐수/용수 비율" fill={palette.actual} radius={[4,4,0,0]}/></BarChart></Chart></article>}
    <article className="card chart-card wide"><CardTitle title={`전년대비 ${energyMetricLabels[metric]} 사용량`} meta={`${data.yoyYear ?? ""}년 vs 전년 · ${yoyUnit}`}><CsvButton filename={`energy_yoy_${metric}_${data.yoyYear ?? ""}`} rows={yoyCsvRows} columns={["month","previous","current","diff","diffPct"]} labels={{month:"월",previous:`전년(${yoyUnit})`,current:`금년(${yoyUnit})`,diff:`증감량(${yoyUnit})`,diffPct:"증감률(%)"}}/></CardTitle><Chart><BarChart data={yoyTable.rows}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="month"/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/><Legend/><Bar dataKey="previous" name="전년" fill={palette.previous} radius={[4,4,0,0]}/><Bar dataKey="current" name="금년" fill={palette.actual} radius={[4,4,0,0]}/></BarChart></Chart>
      <div className="table-wrap yoy-table"><table><thead><tr><th>월</th><th>전년 실적</th><th>금년 실적</th><th>증감량</th><th>증감률(%)</th></tr></thead><tbody>
        {yoyTable.rows.map(row => <tr key={row.month}><td>{row.month}</td><td>{row.previous == null ? "-" : fmt(row.previous)}</td><td>{row.current == null ? "-" : fmt(row.current)}</td><td>{row.diff == null ? "-" : fmt(row.diff)}</td><td className={row.diffPct == null ? "" : row.diffPct > 0 ? "bad" : "good"}>{row.diffPct == null ? "-" : `${row.diffPct > 0 ? "+" : ""}${fmt(row.diffPct)}`}</td></tr>)}
        {yoyTable.total && <tr className="total-row"><td>{yoyTable.total.month}</td><td>{fmt(yoyTable.total.previous)}</td><td>{fmt(yoyTable.total.current)}</td><td>{fmt(yoyTable.total.diff)}</td><td className={yoyTable.total.diffPct == null ? "" : yoyTable.total.diffPct > 0 ? "bad" : "good"}>{yoyTable.total.diffPct == null ? "-" : `${yoyTable.total.diffPct > 0 ? "+" : ""}${fmt(yoyTable.total.diffPct)}`}</td></tr>}
      </tbody></table></div></article>
    <article className="card table-card wide"><CardTitle title="공장별 사용량" meta={`${summaryMeta} 누계`}><CsvButton filename="energy_factories" rows={data.factories} columns={["factory","power","fuel","water","wastewater"]} labels={{factory:"공장",power:"전력(MWh)",fuel:"연료(천 Nm³)",water:"용수(천 ton)",wastewater:"폐수(천 ton)"}}/></CardTitle><DataTable rows={data.factories} columns={["factory",metric]} labels={{factory:"공장",[metric]:units[metric]}}/></article></section></>;
}

function Intensity({ data, metric, onMetricChange, mode, onModeChange, rangeFrom, rangeTo, onRangeChange }: {
  data: AnyData; metric: IntensityMetric; onMetricChange: (metric: IntensityMetric) => void;
  mode: EnergyMode; onModeChange: (mode: EnergyMode) => void;
  rangeFrom: string; rangeTo: string; onRangeChange: (from: string, to: string) => void;
}) {
  const periodLabel = data.dateFrom && data.dateTo ? `${data.dateFrom} ~ ${data.dateTo}` : "";
  // 전년대비 테이블 — 월별 증감률은 클라이언트 계산, 누계 행은 서버의 가중 평균
  // (Σ사용량 ÷ Σ생산톤, 동월 누계) 값을 사용한다. 단순 평균 합산은 왜곡되기 때문.
  const yoyRows = (data.monthly ?? []).map((row: AnyData) => ({
    month: row.month, previous: row.previous, current: row.current,
    change: row.current != null && row.previous > 0 ? Math.round((row.current / row.previous - 1) * 1000) / 10 : null,
  }));
  const cumulative = data.yoyCumulative;
  return <><div className="segmented" role="group" aria-label="원단위 지표 선택">{intensityMetrics.map(item=><button type="button" key={item.id} className={metric===item.id?"active":""} aria-pressed={metric===item.id} onClick={()=>onMetricChange(item.id)}>{item.label}</button>)}</div>
    <div className="mode-row">
      <div className="segmented" role="group" aria-label="원단위 일별 조회 방식">{energyModes.map(item => <button type="button" key={item.id} className={mode === item.id ? "active" : ""} aria-pressed={mode === item.id} onClick={() => onModeChange(item.id)}>{item.label}</button>)}</div>
      {mode === "range" && <div className="range-fields">
        <label><span>시작일</span><input type="date" value={rangeFrom} max={rangeTo} onChange={event => onRangeChange(event.target.value, rangeTo)}/></label>
        <label><span>종료일</span><input type="date" value={rangeTo} min={rangeFrom} onChange={event => onRangeChange(rangeFrom, event.target.value)}/></label>
      </div>}
      {periodLabel && <span className="period-chip">{periodLabel}</span>}
    </div>
    <section className="kpi-grid compact"><Kpi label="MTD 원단위" value={data.summary?.mtd?.current} unit={data.unit} change={data.summary?.mtd?.change} icon={Gauge}/><Kpi label="YTD 원단위" value={data.summary?.ytd?.current} unit={data.unit} change={data.summary?.ytd?.change} icon={CalendarDays}/><Kpi label="절감 목표" value={data.targetPct} unit="%" icon={ShieldCheck}/></section><section className="content-grid">
    <article className="card chart-card span-all"><CardTitle title="일별 원단위 추이" meta={`${data.unit} · 생산 실적 있는 날만 표시`}><CsvButton filename={`intensity_daily_${metric}_${(data.dateFrom ?? "").replaceAll("-","")}`} rows={data.daily} columns={["date","value"]} labels={{date:"일자",value:`원단위(${data.unit})`}}/></CardTitle><Chart><LineChart data={data.daily}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="date" interval="preserveStartEnd" minTickGap={18}/><YAxis domain={["auto","auto"]}/><Tooltip {...tooltipStyle} formatter={numberFormatter}/><Line dataKey="value" name={`원단위(${data.unit})`} stroke={palette.actual} strokeWidth={2.5} connectNulls={false} dot={{ r: 2.5 }}/></LineChart></Chart></article>
    <article className="card chart-card span-all"><CardTitle title={`${data.year}년 원단위 추이`} meta={data.unit}><CsvButton filename={`intensity_monthly_${metric}_${data.year}`} rows={data.monthly} columns={["month","previous","target","current"]} labels={{month:"월",previous:`전년(${data.unit})`,target:`목표(${data.unit})`,current:`금년(${data.unit})`}}/></CardTitle><Chart><LineChart data={data.monthly}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="month"/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/><Legend/><Line dataKey="previous" name="전년" stroke={palette.previous}/><Line dataKey="target" name="목표" stroke={palette.target} strokeDasharray="5 4"/><Line dataKey="current" name="금년" stroke={palette.actual} strokeWidth={3}/></LineChart></Chart>
      <div className="table-wrap yoy-table"><table><thead><tr><th>월</th><th>전년</th><th>금년</th><th>증감률(%)</th></tr></thead><tbody>
        {yoyRows.map((row: AnyData) => <tr key={row.month}><td>{row.month}</td><td>{row.previous == null ? "-" : fmt(row.previous, 2)}</td><td>{row.current == null ? "-" : fmt(row.current, 2)}</td><td className={row.change == null ? "" : row.change > 0 ? "bad" : "good"}>{row.change == null ? "-" : `${row.change > 0 ? "+" : ""}${fmt(row.change)}`}</td></tr>)}
        {cumulative && <tr className="total-row"><td>누계 (1~{cumulative.lastMonth}월) · 가중</td><td>{cumulative.previous == null ? "-" : fmt(cumulative.previous, 2)}</td><td>{cumulative.current == null ? "-" : fmt(cumulative.current, 2)}</td><td className={cumulative.change == null ? "" : cumulative.change > 0 ? "bad" : "good"}>{cumulative.change == null ? "-" : `${cumulative.change > 0 ? "+" : ""}${fmt(cumulative.change)}`}</td></tr>}
      </tbody></table></div>
      <p className="quad-caption">누계는 단순 평균이 아니라 가중 평균(Σ사용량 ÷ Σ생산톤)으로, 금년 실적이 있는 월까지 전년과 같은 기간을 합산합니다.</p></article>
    <article className="card table-card wide"><CardTitle title="공장 효율 매트릭스" meta="MTD 기준"><CsvButton filename={`intensity_matrix_${metric}`} rows={data.matrix} columns={["factory","current","previous","change"]} labels={{factory:"공장",current:`금년(${data.unit})`,previous:`전년(${data.unit})`,change:"증감률(%)"}}/></CardTitle><DataTable rows={data.matrix} columns={["factory","current","previous","change"]} labels={{factory:"공장",current:"금년",previous:"전년",change:"증감률(%)"}}/></article></section></>;
}

function Production({ data, mode, onModeChange, rangeFrom, rangeTo, onRangeChange }: {
  data: AnyData; mode: ProductionMode; onModeChange: (mode: ProductionMode) => void;
  rangeFrom: string; rangeTo: string; onRangeChange: (from: string, to: string) => void;
}) {
  const s = data.summary ?? {};
  const planAllowed = data.planAllowed !== false;
  const periodLabel = data.dateFrom && data.dateTo ? `${data.dateFrom} ~ ${data.dateTo}` : "";
  const trendTitle = mode === "year" ? "월별 생산량 (제품유형별)" : "제품유형별 일일 생산량";
  const csvColumns = ["date", "IC", "MY", "FM", "SN", "ETC"];
  const csvLabels = { date: mode === "year" ? "월" : "일자", IC: "IC(ton)", MY: "MY(ton)", FM: "FM(ton)", SN: "SN(ton)", ETC: "기타(ton)" };
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
      <Kpi label={mode === "year" ? "연말 착지 예상" : "예상 착지"} value={mode === "range" ? "N/A" : s.forecast} unit={mode === "range" ? undefined : "ton"} icon={PackageCheck}/>
    </section>
    <section className="content-grid">
      {mode === "year" && (data.burnup?.length ?? 0) > 0 && <article className="card chart-card wide"><CardTitle title="연간 Burn-up" meta="월별 누적 실적 vs 계획 누계 (ton)"/><Chart><LineChart data={data.burnup}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="month"/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/><Legend/><Line dataKey="cumPlan" name="누적 계획" stroke={palette.previous} strokeDasharray="6 4" strokeWidth={2} dot={false}/><Line dataKey="cumActual" name="누적 실적" stroke={palette.actual} strokeWidth={3} connectNulls={false}/></LineChart></Chart></article>}
      <article className="card chart-card wide"><CardTitle title={trendTitle} meta="ton"><CsvButton filename={`production_${mode}_${(data.dateFrom ?? "").replaceAll("-", "")}`} rows={data.daily} columns={csvColumns} labels={csvLabels}/></CardTitle><Chart><BarChart data={data.daily}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="date" interval={mode === "month" ? 0 : "preserveStartEnd"} minTickGap={18}/><YAxis/><Tooltip {...tooltipStyle} formatter={numberFormatter}/><Legend formatter={(value: string) => cat2Labels[value] ?? value}/><Bar dataKey="IC" stackId="a" fill={palette.cat2.IC}/><Bar dataKey="MY" stackId="a" fill={palette.cat2.MY}/><Bar dataKey="FM" stackId="a" fill={palette.cat2.FM}/><Bar dataKey="SN" stackId="a" fill={palette.cat2.SN}/><Bar dataKey="ETC" stackId="a" fill={palette.cat2.ETC}/></BarChart></Chart></article>
      <article className="card list"><CardTitle title="제품 믹스" meta="구성비"/>{data.mix?.map((r: AnyData) => <div className="progress" key={r.name}><div><span>{cat2Labels[r.name] ?? r.name}</span><b>{fmt(r.value)}%</b></div><i><em style={{ width: `${r.value}%` }}/></i></div>)}</article>
      <article className="card table-card wide"><CardTitle title="주요 품목 계획 대비 실적" meta={`${s.items ?? 0}개 품목`}><CsvButton filename={`item_ranking_${mode}_${(data.dateFrom ?? "").replaceAll("-", "")}`} rows={data.topItems} columns={["name", "plan", "actual", "rate"]} labels={{ name: "품목", plan: "계획(ton)", actual: "실적(ton)", rate: "달성률(%)" }}/></CardTitle><DataTable rows={data.topItems} columns={["name", "plan", "actual", "rate"]} labels={{ name: "품목", plan: "계획", actual: "실적", rate: "달성률(%)" }}/></article>
    </section>
  </>;
}

const diagnosableFactories = ["남양주1", "남양주2", "김해", "광주", "논산"];

function Prediction({ data, factory, date, isAdmin }: { data: AnyData; factory: string; date: string; isAdmin: boolean }) { return <><section className="model-banner"><div><BrainCircuit/><span>운영 모델</span><strong>{data.model?.version}</strong></div><div><span>상태</span><strong>{data.model?.state}</strong></div><div><span>최근 학습</span><strong>{data.model?.trainedAt}</strong></div></section><PredictionRunner factory={factory} date={date} isAdmin={isAdmin}/><section className="kpi-grid compact"><Kpi label="정상 예측" value={data.status?.normal} unit="건" icon={ShieldCheck}/><Kpi label="정상범주 이탈" value={data.status?.alert} unit="건" icon={Activity}/><Kpi label="모니터링 상태" value={data.status?.label} icon={BrainCircuit}/></section>{diagnosableFactories.includes(factory) ? <FeatureImportance factory={factory}/> : <div className="info-note">모델 변수 영향도는 개별 공장(남양주1·남양주2·김해·광주·논산) 선택 시 표시됩니다.</div>}<PredictionHistory rows={data.latest ?? []} factory={factory} isAdmin={isAdmin} diagnosable={diagnosableFactories.includes(factory)}/></> }

function CardTitle({ title, meta, children }: { title: string; meta: string; children?: React.ReactNode }) { return <header className="card-title"><h3>{title}</h3><div className="card-title-side">{children}<span>{meta}</span></div></header> }
function Chart({ children }: { children: React.ReactElement }) { return <div className="chart"><ResponsiveContainer width="100%" height="100%">{children}</ResponsiveContainer></div> }
function DataTable({ rows=[], columns, labels }: { rows?: AnyData[]; columns:string[]; labels:AnyData }) { return <div className="table-wrap"><table><thead><tr>{columns.map(c=><th key={c}>{labels[c]??c}</th>)}</tr></thead><tbody>{rows.map((r,i)=><tr key={i}>{columns.map(c=><td key={c}>{typeof r[c]==="number"?fmt(r[c]):r[c]??"-"}</td>)}</tr>)}</tbody></table></div> }

export function BemsApp() {
  const [screen,setScreen]=useState<Screen>("dashboard"), [factory,setFactory]=useState("전사"), [date,setDate]=useState(localToday), [mobile,setMobile]=useState(false);
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
    const today=localToday();
    const from=new Date(Date.parse(`${today}T00:00:00`)-30*86_400_000);
    return {from:`${from.getFullYear()}-${String(from.getMonth()+1).padStart(2,"0")}-${String(from.getDate()).padStart(2,"0")}`,to:today};
  });
  const [energyMode,setEnergyMode]=useState<EnergyMode>("recent");
  const [energyRange,setEnergyRange]=useState<{from:string;to:string}>(()=>{
    const today=localToday();
    const from=new Date(Date.parse(`${today}T00:00:00`)-30*86_400_000);
    return {from:`${from.getFullYear()}-${String(from.getMonth()+1).padStart(2,"0")}-${String(from.getDate()).padStart(2,"0")}`,to:today};
  });
  const [intensityMode,setIntensityMode]=useState<EnergyMode>("recent");
  const [intensityRange,setIntensityRange]=useState<{from:string;to:string}>(()=>{
    const today=localToday();
    const from=new Date(Date.parse(`${today}T00:00:00`)-30*86_400_000);
    return {from:`${from.getFullYear()}-${String(from.getMonth()+1).padStart(2,"0")}-${String(from.getDate()).padStart(2,"0")}`,to:today};
  });
  const [data,setData]=useState<AnyData>(demo.dashboard), [session,setSession]=useState<AnyData>(demo.session), [sessionLive,setSessionLive]=useState(false), [live,setLive]=useState(false), [loading,setLoading]=useState(true);
  const fallback=useMemo(()=>{
    if (!isDataScreen(screen)) return {};
    const selected=screenFallback[screen];
    return screen==="intensity" ? {...selected,metric:intensityMetric,unit:intensityUnits[intensityMetric]} : selected;
  },[screen,intensityMetric]);
  useEffect(()=>{apiGet("/session",demo.session).then(r=>{setSession(r.data);setSessionLive(r.live)});},[]);
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
  const statusLive=isDataScreen(screen)?live:sessionLive;
  const statusTitle=isDataScreen(screen)?(live?"Local DB":"예시 데이터"):(sessionLive?"BEMS API":"API 연결 실패");
  const statusDetail=isDataScreen(screen)?(live?"MySQL 연결됨":"API 연결 실패"):(sessionLive?"권한 확인됨":"세션 확인 실패");
  return <div className="app-shell"><aside className={mobile?"open":""} aria-label="주 메뉴"><div className="brand"><div><Bolt size={21}/></div><span>AI ELITE<strong>BEMS NEXT</strong></span><button type="button" className="close" aria-label="메뉴 닫기" onClick={()=>setMobile(false)}><X/></button></div><nav>{menus.map(item=>{const Icon=item.icon;return <button type="button" key={item.id} className={screen===item.id?"active":""} aria-current={screen===item.id?"page":undefined} onClick={()=>{setScreen(item.id);setMobile(false)}}><Icon size={19}/><span>{item.label}</span><ChevronRight size={15}/></button>})}</nav><div className="side-status" role="status" aria-live="polite"><Database size={17}/><div><b>{statusTitle}</b><span>{statusDetail}</span></div><i className={statusLive?"on":""}/></div><footer>v1.0 · Internal Network</footer></aside>{mobile&&<button type="button" className="scrim" aria-label="메뉴 닫기" onClick={()=>setMobile(false)}/>}<main><header className="topbar"><button type="button" className="menu" aria-label="메뉴 열기" onClick={()=>setMobile(true)}><Menu/></button><div className="heading"><h1>{title}</h1><p>{subtitle}</p></div><div className="filters"><label><Building2 size={16}/><select aria-label="공장 선택" value={factory} onChange={e=>setFactory(e.target.value)}>{factories.map(f=><option key={f}>{f}</option>)}</select></label><label><CalendarDays size={16}/><input type="date" aria-label="기준일 선택" value={date} onChange={e=>setDate(e.target.value)}/></label><button type="button" className="theme-toggle" aria-label="화면 테마 전환" title={theme==="dark"?"라이트 모드로 전환":"다크 모드로 전환"} onClick={toggleTheme}>{theme==="dark"?<Sun size={16}/>:<Moon size={16}/>}</button><div className={`role ${session.role}`}><ShieldCheck size={16}/>{session.role==="admin"?"관리자":"조회 사용자"}</div></div></header><div className="mobile-title"><h1>{title}</h1><p>{subtitle}</p></div><div className="workspace" aria-busy={loading}>{loading?<div className="loading" role="status" aria-live="polite"><RefreshCw className="spin"/>데이터를 불러오는 중입니다.</div>:<>{!live&&isDataScreen(screen)&&<section className="data-warning" role="alert"><Database size={20}/><div><strong>API 연결 실패 · 예시 데이터 표시 중</strong><p>현재 화면의 수치는 데모 값이며 실제 운영 판단에 사용할 수 없습니다.</p></div></section>}{screen==="dashboard"&&<Dashboard data={data} factory={factory} date={date} isAdmin={session.role==="admin"}/>} {screen==="energy"&&<Energy data={data} mode={energyMode} onModeChange={setEnergyMode} rangeFrom={energyRange.from} rangeTo={energyRange.to} onRangeChange={(from,to)=>{ if(from&&to&&from>to){ setEnergyRange({from:to,to}); } else { setEnergyRange({from,to}); } }}/>} {screen==="intensity"&&<Intensity data={data} metric={intensityMetric} onMetricChange={setIntensityMetric} mode={intensityMode} onModeChange={setIntensityMode} rangeFrom={intensityRange.from} rangeTo={intensityRange.to} onRangeChange={(from,to)=>{ if(from&&to&&from>to){ setIntensityRange({from:to,to}); } else { setIntensityRange({from,to}); } }}/>} {screen==="production"&&<Production data={data} mode={productionMode} onModeChange={setProductionMode} rangeFrom={productionRange.from} rangeTo={productionRange.to} onRangeChange={(from,to)=>{ if(from&&to&&from>to){ setProductionRange({from:to,to}); } else { setProductionRange({from,to}); } }}/>} {screen==="prediction"&&<Prediction data={data} factory={factory} date={date} isAdmin={session.role==="admin"}/>} {screen==="report"&&<ReportScreen factory={factory} date={date} isAdmin={session.role==="admin"}/>} {screen==="admin"&&<AdminScreen factory={factory} date={date} isAdmin={session.role==="admin"}/>}</>}</div></main></div>;
}
