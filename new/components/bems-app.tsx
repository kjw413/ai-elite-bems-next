"use client";

import { useEffect, useMemo, useState } from "react";
import { Activity, BarChart3, Bolt, BrainCircuit, Building2, CalendarDays, ChevronRight, Database, Factory, Gauge, Menu, PackageCheck, RefreshCw, ShieldCheck, X } from "lucide-react";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { apiGet, query } from "@/lib/bems-api";
import { demo, factories } from "@/lib/bems-data";

type Screen = "dashboard" | "energy" | "intensity" | "production" | "prediction";
type AnyData = Record<string, any>;

const menus: { id: Screen; label: string; icon: typeof Activity }[] = [
  { id: "dashboard", label: "통합 대시보드", icon: BarChart3 },
  { id: "energy", label: "에너지 사용량", icon: Bolt },
  { id: "intensity", label: "에너지 원단위", icon: Gauge },
  { id: "production", label: "생산실적 분석", icon: PackageCheck },
  { id: "prediction", label: "AI 에너지 예측", icon: BrainCircuit },
];

const titles: Record<Screen, [string, string]> = {
  dashboard: ["통합 에너지 대시보드", "실적과 AI 예측을 한눈에 확인합니다."],
  energy: ["에너지 사용량", "전력·연료·용수 사용 추이를 비교합니다."],
  intensity: ["에너지 원단위", "생산량 대비 에너지 효율을 추적합니다."],
  production: ["생산실적 분석", "계획 대비 생산성과 제품 믹스를 분석합니다."],
  prediction: ["AI 에너지 예측", "v5.3 예측값과 정상범주 이탈을 모니터링합니다."],
};

const endpoint: Record<Screen, string> = { dashboard: "/dashboard", energy: "/energy", intensity: "/intensity", production: "/production", prediction: "/predictions" };

const fmt = (value: unknown, digits = 1) => typeof value === "number" ? value.toLocaleString("ko-KR", { maximumFractionDigits: digits }) : "-";

function Kpi({ label, value, unit, change, icon: Icon = Activity }: { label: string; value: unknown; unit?: string; change?: number | null; icon?: typeof Activity }) {
  return <article className="kpi card"><div className="kpi-icon"><Icon size={20}/></div><div><p>{label}</p><strong>{fmt(value)} <small>{unit}</small></strong>{change != null && <span className={change <= 0 ? "good" : "bad"}>{change > 0 ? "+" : ""}{fmt(change)}% 전년비</span>}</div></article>;
}

function Dashboard({ data }: { data: AnyData }) {
  return <><section className="kpi-grid">{data.metrics?.map((m: AnyData) => <Kpi key={m.id} label={m.label} value={m.value} unit={m.unit} change={m.change} icon={m.id === "production" ? Factory : Bolt}/>)}</section>
    <section className={`alert ${data.alert?.level ?? "normal"}`}><BrainCircuit size={22}/><div><strong>{data.alert?.title}</strong><p>{data.alert?.description}</p></div></section>
    <section className="content-grid"><article className="card chart-card wide"><CardTitle title="최근 7일 전력 사용량" meta="MWh · AI P05~P95 정상범주"/><Chart><AreaChart data={data.trend}><defs><linearGradient id="band" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#4f7cff" stopOpacity={.22}/><stop offset="1" stopColor="#4f7cff" stopOpacity={.02}/></linearGradient></defs><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="date"/><YAxis/><Tooltip/><Area type="monotone" dataKey="upper" stroke="none" fill="url(#band)"/><Line type="monotone" dataKey="predicted" name="AI 예측" stroke="#8b5cf6" strokeDasharray="5 4" strokeWidth={2}/><Line type="monotone" dataKey="actual" name="실제" stroke="#2563eb" strokeWidth={3}/></AreaChart></Chart></article>
    <article className="card chart-card"><CardTitle title="공장별 전력 원단위" meta="낮을수록 효율적"/><Chart><BarChart data={data.factoryComparison} layout="vertical"><CartesianGrid strokeDasharray="3 3"/><XAxis type="number"/><YAxis dataKey="factory" type="category" width={58}/><Tooltip/><Bar dataKey="value" fill="#22a06b" radius={[0,6,6,0]}/></BarChart></Chart></article>
    <article className="card chart-card"><CardTitle title="월별 전년 비교" meta="kWh/ton"/><Chart><LineChart data={data.yoy}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="month"/><YAxis/><Tooltip/><Legend/><Line dataKey="previous" name="전년" stroke="#aeb8c8"/><Line dataKey="current" name="금년" stroke="#2563eb" strokeWidth={3}/></LineChart></Chart></article>
    <article className="card events"><CardTitle title="최근 현장 이벤트" meta={`${data.events?.length ?? 0}건`}/>{data.events?.map((event: AnyData) => <div className="event" key={event.id}><time>{event.date}</time><span>{event.factory}</span><div><b>{event.tag}</b><p>{event.note}</p></div></div>)}</article></section></>;
}

function Energy({ data }: { data: AnyData }) {
  const [metric, setMetric] = useState("power"); const units: AnyData = { power: "MWh", fuel: "천 Nm³", water: "천 ton", wastewater: "천 ton" };
  const values = data.daily?.map((r: AnyData) => Number(r[metric]) || 0) ?? []; const total = values.reduce((a: number,b: number)=>a+b,0);
  return <><div className="segmented">{Object.entries({power:"전력",fuel:"연료",water:"용수",wastewater:"폐수"}).map(([id,label])=><button className={metric===id?"active":""} onClick={()=>setMetric(id)} key={id}>{label}</button>)}</div><section className="kpi-grid compact"><Kpi label="기간 누계" value={total} unit={units[metric]} icon={Bolt}/><Kpi label="일평균" value={values.length?total/values.length:0} unit={units[metric]} icon={Activity}/><Kpi label="최대 사용량" value={values.length?Math.max(...values):0} unit={units[metric]} icon={Gauge}/></section><section className="content-grid"><article className="card chart-card wide"><CardTitle title="일별 사용 추이" meta={units[metric]}/><Chart><AreaChart data={data.daily}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="date"/><YAxis/><Tooltip/><Area type="monotone" dataKey={metric} stroke="#2563eb" strokeWidth={3} fill="#dce8ff"/></AreaChart></Chart></article><article className="card list"><CardTitle title="설비 구성" meta="이번 달"/>{data.equipment?.map((r:AnyData)=><div className="progress" key={r.name}><div><span>{r.name}</span><b>{fmt(r.value)}%</b></div><i><em style={{width:`${r.value}%`}}/></i></div>)}</article><article className="card table-card wide"><CardTitle title="공장별 사용량" meta="이번 달 누계"/><DataTable rows={data.factories} columns={["factory",metric]} labels={{factory:"공장",[metric]:units[metric]}}/></article></section></>;
}

function Intensity({ data }: { data: AnyData }) { return <><section className="kpi-grid compact"><Kpi label="MTD 원단위" value={data.summary?.mtd?.current} unit={data.unit} change={data.summary?.mtd?.change} icon={Gauge}/><Kpi label="YTD 원단위" value={data.summary?.ytd?.current} unit={data.unit} change={data.summary?.ytd?.change} icon={CalendarDays}/><Kpi label="절감 목표" value={data.targetPct} unit="%" icon={ShieldCheck}/></section><section className="content-grid"><article className="card chart-card wide"><CardTitle title={`${data.year}년 원단위 추이`} meta={data.unit}/><Chart><LineChart data={data.monthly}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="month"/><YAxis/><Tooltip/><Legend/><Line dataKey="previous" name="전년" stroke="#aeb8c8"/><Line dataKey="target" name="목표" stroke="#22a06b" strokeDasharray="5 4"/><Line dataKey="current" name="금년" stroke="#2563eb" strokeWidth={3}/></LineChart></Chart></article><article className="card table-card wide"><CardTitle title="공장 효율 매트릭스" meta="MTD 기준"/><DataTable rows={data.matrix} columns={["factory","current","previous","change"]} labels={{factory:"공장",current:"금년",previous:"전년",change:"증감률(%)"}}/></article></section></> }

function Production({ data }: { data: AnyData }) { const s=data.summary??{}; return <><section className="kpi-grid"><Kpi label="누계 계획" value={s.plan} unit="ton" icon={CalendarDays}/><Kpi label="누계 실적" value={s.actual} unit="ton" icon={Factory}/><Kpi label="계획 달성률" value={s.progress} unit="%" icon={Gauge}/><Kpi label="예상 착지" value={s.forecast} unit="ton" icon={PackageCheck}/></section><section className="content-grid"><article className="card chart-card wide"><CardTitle title="제품유형별 일일 생산량" meta="ton"/><Chart><BarChart data={data.daily}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="date"/><YAxis/><Tooltip/><Legend/><Bar dataKey="IC" stackId="a" fill="#2563eb"/><Bar dataKey="MY" stackId="a" fill="#22a06b"/><Bar dataKey="FM" stackId="a" fill="#8b5cf6"/><Bar dataKey="SN" stackId="a" fill="#f59e0b"/></BarChart></Chart></article><article className="card list"><CardTitle title="제품 믹스" meta="구성비"/>{data.mix?.map((r:AnyData)=><div className="progress" key={r.name}><div><span>{r.name}</span><b>{fmt(r.value)}%</b></div><i><em style={{width:`${r.value}%`}}/></i></div>)}</article><article className="card table-card wide"><CardTitle title="주요 품목 계획 대비 실적" meta={`${s.items??0}개 품목`}/><DataTable rows={data.topItems} columns={["name","plan","actual","rate"]} labels={{name:"품목",plan:"계획",actual:"실적",rate:"달성률(%)"}}/></article></section></> }

function Prediction({ data }: { data: AnyData }) { return <><section className="model-banner"><div><BrainCircuit/><span>운영 모델</span><strong>{data.model?.version}</strong></div><div><span>상태</span><strong>{data.model?.state}</strong></div><div><span>최근 학습</span><strong>{data.model?.trainedAt}</strong></div></section><section className="kpi-grid compact"><Kpi label="정상 예측" value={data.status?.normal} unit="건" icon={ShieldCheck}/><Kpi label="정상범주 이탈" value={data.status?.alert} unit="건" icon={Activity}/><Kpi label="모니터링 상태" value={data.status?.label} icon={BrainCircuit}/></section><article className="card table-card"><CardTitle title="최근 예측 이력" meta="P05~P95 정상범주"/><DataTable rows={data.latest} columns={["date","target","predicted","lower","upper","actual","status"]} labels={{date:"일자",target:"지표",predicted:"P50",lower:"P05",upper:"P95",actual:"실측",status:"판정"}}/></article></> }

function CardTitle({ title, meta }: { title: string; meta: string }) { return <header className="card-title"><h3>{title}</h3><span>{meta}</span></header> }
function Chart({ children }: { children: React.ReactElement }) { return <div className="chart"><ResponsiveContainer width="100%" height="100%">{children}</ResponsiveContainer></div> }
function DataTable({ rows=[], columns, labels }: { rows?: AnyData[]; columns:string[]; labels:AnyData }) { return <div className="table-wrap"><table><thead><tr>{columns.map(c=><th key={c}>{labels[c]??c}</th>)}</tr></thead><tbody>{rows.map((r,i)=><tr key={i}>{columns.map(c=><td key={c}>{typeof r[c]==="number"?fmt(r[c]):r[c]??"-"}</td>)}</tr>)}</tbody></table></div> }

export function BemsApp() {
  const [screen,setScreen]=useState<Screen>("dashboard"), [factory,setFactory]=useState("전사"), [date,setDate]=useState("2026-07-15"), [mobile,setMobile]=useState(false);
  const [data,setData]=useState<AnyData>(demo.dashboard), [session,setSession]=useState<AnyData>(demo.session), [live,setLive]=useState(false), [loading,setLoading]=useState(true);
  const fallback=useMemo(()=>demo[screen] as AnyData,[screen]);
  useEffect(()=>{apiGet("/session",demo.session).then(r=>setSession(r.data));},[]);
  useEffect(()=>{const controller=new AbortController(); setLoading(true); const suffix=query({factory,...(screen==="dashboard"?{date}:{}),...(screen==="intensity"?{metric:"power"}:{})}); apiGet(`${endpoint[screen]}?${suffix}`,fallback,controller.signal).then(r=>{setData(r.data);setLive(r.live);setLoading(false)}).catch(()=>{}); return()=>controller.abort();},[screen,factory,date,fallback]);
  const [title,subtitle]=titles[screen];
  return <div className="app-shell"><aside className={mobile?"open":""}><div className="brand"><div><Bolt size={21}/></div><span>AI ELITE<strong>BEMS NEXT</strong></span><button className="close" onClick={()=>setMobile(false)}><X/></button></div><nav>{menus.map(item=>{const Icon=item.icon;return <button key={item.id} className={screen===item.id?"active":""} onClick={()=>{setScreen(item.id);setMobile(false)}}><Icon size={19}/><span>{item.label}</span><ChevronRight size={15}/></button>})}</nav><div className="side-status"><Database size={17}/><div><b>{live?"Local DB":"예시 데이터"}</b><span>{live?"MySQL 연결됨":"API 연결 대기"}</span></div><i className={live?"on":""}/></div><footer>v1.0 · Internal Network</footer></aside>{mobile&&<button className="scrim" onClick={()=>setMobile(false)}/>}<main><header className="topbar"><button className="menu" onClick={()=>setMobile(true)}><Menu/></button><div className="heading"><h1>{title}</h1><p>{subtitle}</p></div><div className="filters"><label><Building2 size={16}/><select value={factory} onChange={e=>setFactory(e.target.value)}>{factories.map(f=><option key={f}>{f}</option>)}</select></label><label><CalendarDays size={16}/><input type="date" value={date} onChange={e=>setDate(e.target.value)}/></label><div className={`role ${session.role}`}><ShieldCheck size={16}/>{session.role==="admin"?"관리자":"조회 사용자"}</div></div></header><div className="mobile-title"><h1>{title}</h1><p>{subtitle}</p></div><div className="workspace">{loading?<div className="loading"><RefreshCw className="spin"/>데이터를 불러오는 중입니다.</div>:<>{screen==="dashboard"&&<Dashboard data={data}/>} {screen==="energy"&&<Energy data={data}/>} {screen==="intensity"&&<Intensity data={data}/>} {screen==="production"&&<Production data={data}/>} {screen==="prediction"&&<Prediction data={data}/>}</>}</div></main></div>;
}
