"use client";

import { useState } from "react";
import { BellRing, Download } from "lucide-react";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { downloadCsv } from "@/lib/bems-csv";

type AnyData = Record<string, any>;
type YoyMode = "intensity" | "usage" | "production";

// legacy 대시보드 '월간 원단위/사용량/생산량 전년비'의 React 이식.
// 당월 1일~기준일 vs 전년 동기(공장별)를 지표 구분 3모드로 비교한다.
const yoyModes: { id: YoyMode; label: string }[] = [
  { id: "intensity", label: "원단위" },
  { id: "usage", label: "사용량" },
  { id: "production", label: "생산량" },
];

type MetricDef = { key: string; label: string; unit: string; color: string };
const intensityDefs: MetricDef[] = [
  { key: "power", label: "전력 원단위", unit: "kWh/ton", color: "var(--chart-power)" },
  { key: "fuel", label: "연료 원단위", unit: "Nm³/ton", color: "var(--chart-fuel)" },
  { key: "water", label: "용수 원단위", unit: "ton/ton", color: "var(--chart-water)" },
  { key: "wwratio", label: "폐수/용수", unit: "비율", color: "var(--chart-wastewater)" },
];
const usageDefs: MetricDef[] = [
  { key: "power", label: "전력 사용량", unit: "MWh", color: "var(--chart-power)" },
  { key: "fuel", label: "연료 사용량", unit: "Nm³", color: "var(--chart-fuel)" },
  { key: "water", label: "용수 사용량", unit: "ton", color: "var(--chart-water)" },
  { key: "wastewater", label: "폐수 사용량", unit: "ton", color: "var(--chart-wastewater)" },
];
const productionDefs: MetricDef[] = [
  { key: "production", label: "생산량 (DB 실적)", unit: "ton", color: "var(--chart-production)" },
];

const fmtNum = (value: number | null | undefined, digits = 2) =>
  value == null ? "-" : value.toLocaleString("ko-KR", { maximumFractionDigits: digits });

function changePct(current: number | null, previous: number | null): number | null {
  if (current == null || previous == null || previous <= 0) return null;
  return (current / previous - 1) * 100;
}

function pairOf(entry: AnyData, mode: YoyMode, key: string): { current: number | null; previous: number | null } {
  const section = mode === "production" ? entry.production : entry[mode]?.[key];
  return { current: section?.current ?? null, previous: section?.previous ?? null };
}

// 색 규칙 (legacy 동일): 원단위 = 낮을수록 좋음(＋빨강/－파랑), 생산량 = 높을수록
// 좋음(＋파랑/－빨강), 사용량 = 중립 — 생산량 변동 영향이 섞여 개선/악화 단정 불가.
function changeClass(mode: YoyMode, change: number | null): string {
  if (change == null || mode === "usage") return "";
  if (mode === "production") return change > 0 ? "good" : "bad";
  return change > 0 ? "bad" : "good";
}

function ModeChart({ rows, mode, def }: { rows: AnyData[]; mode: YoyMode; def: MetricDef }) {
  const chartData = rows.map(entry => {
    const { current, previous } = pairOf(entry, mode, def.key);
    return { factory: entry.factory, previous, current, change: changePct(current, previous) };
  });
  return <div className="quad-cell">
    <div className="quad-head" style={{ borderColor: def.color }}>
      <b>{def.label}</b><small>[{def.unit}]</small>
    </div>
    <div className="chart quad-chart">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={chartData} margin={{ top: 8, right: 12, bottom: 0, left: -14 }}>
          <CartesianGrid vertical={false}/>
          <XAxis dataKey="factory" tick={{ fontSize: 11 }}/>
          <YAxis tick={{ fontSize: 11 }}/>
          <Tooltip contentStyle={{ borderRadius: 10, border: "1px solid var(--line)", background: "var(--card)", fontSize: 12 }}
            formatter={(value: unknown, name: unknown) => [fmtNum(typeof value === "number" ? value : null), String(name ?? "")]}
            labelFormatter={(label: unknown) => {
              const name = String(label ?? "");
              const change = chartData.find(item => item.factory === name)?.change;
              return change == null ? name : `${name} · 전년 동기 대비 ${change > 0 ? "+" : ""}${change.toFixed(1)}%`;
            }}/>
          <Legend wrapperStyle={{ fontSize: 11 }}/>
          <Bar dataKey="previous" name="전년" fill="var(--chart-previous)" radius={[4, 4, 0, 0]} maxBarSize={22}/>
          <Bar dataKey="current" name="금년" fill={def.color} radius={[4, 4, 0, 0]} maxBarSize={22}/>
        </BarChart>
      </ResponsiveContainer>
    </div>
  </div>;
}

export function FactoryYoy({ rows, period, factory, date }: { rows: AnyData[]; period?: AnyData; factory: string; date: string }) {
  const [mode, setMode] = useState<YoyMode>("intensity");
  if (!rows?.length) return null;
  const defs = mode === "intensity" ? intensityDefs : mode === "usage" ? usageDefs : productionDefs;
  const periodMeta = period?.currentFrom
    ? `${period.currentFrom} ~ ${period.currentTo} vs 전년 동기`
    : "당월 1일~기준일 vs 전년 동기";
  const tableRows = defs.flatMap(def => rows.map(entry => {
    const { current, previous } = pairOf(entry, mode, def.key);
    const change = changePct(current, previous);
    return { factory: entry.factory, metric: `${def.label} [${def.unit}]`, previous, current,
      change: change == null ? null : Math.round(change * 10) / 10 };
  }));
  return <article className="card chart-card span-all">
    <header className="card-title">
      <h3>월간 원단위·사용량·생산량 전년비</h3>
      <div className="card-title-side">
        <button type="button" className="csv-button" title="현재 모드의 공장별 전년비 데이터를 CSV로 내려받습니다"
          onClick={() => downloadCsv(`yoy_${mode}_${factory}_${date}`, tableRows,
            ["factory", "metric", "previous", "current", "change"],
            { factory: "공장", metric: "지표", previous: "전년", current: "금년", change: "증감률(%)" })}>
          <Download size={13}/>CSV
        </button>
        <span>{periodMeta}</span>
      </div>
    </header>
    <div className="mode-row"><div className="segmented" role="group" aria-label="전년비 지표 구분">
      {yoyModes.map(item => <button type="button" key={item.id} className={mode === item.id ? "active" : ""} aria-pressed={mode === item.id} onClick={() => setMode(item.id)}>{item.label}</button>)}
    </div></div>
    <div className={defs.length > 1 ? "quad-grid" : ""}>
      {defs.map(def => <ModeChart key={def.key} rows={rows} mode={mode} def={def}/>)}
    </div>
    {mode === "usage" && <p className="quad-caption">※ 사용량 증감은 중립색으로 표시합니다 — 생산량 변동의 영향이 섞여 있어 증감만으로 개선/악화를 판단할 수 없습니다. 효율 판단은 &lsquo;원단위&rsquo;를 선택하세요.</p>}
    <details className="quad-details">
      <summary>상세 비교 테이블</summary>
      <div className="table-wrap yoy-table"><table>
        <thead><tr><th>공장</th><th>지표</th><th>전년</th><th>금년</th><th>증감률(%)</th></tr></thead>
        <tbody>{tableRows.map((row, index) => <tr key={index}>
          <td>{row.factory}</td><td>{row.metric}</td><td>{fmtNum(row.previous)}</td><td>{fmtNum(row.current)}</td>
          <td className={changeClass(mode, row.change)}>{row.change == null ? "-" : `${row.change > 0 ? "+" : ""}${row.change.toFixed(1)}`}</td>
        </tr>)}</tbody>
      </table></div>
    </details>
  </article>;
}

type Issue = { icon: string; title: string; desc: string; sev: number };

// legacy _render_issue_alerts 규칙: 원단위 전년비 악화(+)는 전부, 생산량은 10% 이상
// 감소만 이슈로 올리고 심각도 내림차순 Top 5를 표시한다.
export function buildIssues(rows: AnyData[]): Issue[] {
  const issues: Issue[] = [];
  const intensityLabels: Record<string, string> = { power: "전력 원단위", fuel: "연료 원단위", water: "용수 원단위" };
  for (const entry of rows ?? []) {
    for (const [key, label] of Object.entries(intensityLabels)) {
      const change = changePct(entry.intensity?.[key]?.current ?? null, entry.intensity?.[key]?.previous ?? null);
      if (change != null && change > 0) {
        issues.push({ icon: change >= 5 ? "🔥" : "⚠️", title: `${entry.factory} ${label} 악화`,
          desc: `전년 동기 대비 +${change.toFixed(1)}% 증가`, sev: change });
      }
    }
    const production = changePct(entry.production?.current ?? null, entry.production?.previous ?? null);
    if (production != null && production < -10) {
      issues.push({ icon: "⚠️", title: `${entry.factory} 생산량 감소`,
        desc: `전년 동기 대비 ${production.toFixed(1)}% 감소`, sev: -production });
    }
  }
  return issues.sort((a, b) => b.sev - a.sev).slice(0, 5);
}

export function IssuesCard({ rows, className = "" }: { rows: AnyData[]; className?: string }) {
  const issues = buildIssues(rows);
  return <article className={`card issues-card ${className}`}>
    <header className="card-title"><h3>월간 주요 이슈</h3><div className="card-title-side"><BellRing size={16}/><span>당월 vs 전년 동기</span></div></header>
    {issues.length === 0
      ? <div className="issues-empty">✅ 주요 이상 항목이 없습니다.</div>
      : issues.map((issue, index) => <div className="issue-row" key={index}>
          <span aria-hidden>{issue.icon}</span>
          <div><b>{issue.title}</b><p>{issue.desc}</p></div>
          <i className={issue.sev >= 5 ? "sev-high" : "sev-mid"}/>
        </div>)}
  </article>;
}
