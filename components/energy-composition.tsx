"use client";

import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import { factoryColors } from "@/lib/bems-data";

type AnyData = Record<string, any>;

// legacy 대시보드 _render_energy_composition의 React 이식 — 공장별 에너지 사용
// 비중 도넛 4개 (YTD 누계). 슬라이스 색은 공장 아이덴티티 색을 그대로 쓴다.
const donutDefs = [
  { key: "power", title: "전력", unit: "MWh" },
  { key: "fuel", title: "연료", unit: "Nm³" },
  { key: "water", title: "용수", unit: "ton" },
  { key: "wastewater", title: "폐수", unit: "ton" },
] as const;

const fmtTotal = (value: number) =>
  value >= 1000 ? value.toLocaleString("ko-KR", { maximumFractionDigits: 0 }) : value.toLocaleString("ko-KR", { maximumFractionDigits: 1 });
function RatioLabel({ percent, payload, x, y, textAnchor }: AnyData) {
  if ((percent ?? 0) < 0.06) return null;
  const color = factoryColors[String(payload?.name)] ?? "var(--chart-previous)";
  return <text x={x} y={y} fill={color} textAnchor={textAnchor} dominantBaseline="central" fontSize={11}>
    {`${((percent ?? 0) * 100).toFixed(0)}%`}
  </text>;
}

function Donut({ rows, def }: { rows: AnyData[]; def: (typeof donutDefs)[number] }) {
  const data = rows
    .map(row => ({ name: String(row.factory), value: Number(row[def.key]) || 0 }))
    .filter(row => row.value > 0);
  const total = data.reduce((acc, row) => acc + row.value, 0);
  return <div className="donut-cell">
    <b className="donut-title">{def.title}</b>
    <div className="donut-chart">
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Tooltip contentStyle={{ borderRadius: 10, border: "1px solid var(--line)", background: "var(--card)", fontSize: 12 }}
            formatter={(value: unknown, name: unknown) => [
              `${fmtTotal(typeof value === "number" ? value : 0)} ${def.unit} (${total > 0 ? ((Number(value) || 0) / total * 100).toFixed(1) : 0}%)`,
              String(name ?? ""),
            ]}/>
          <Pie data={data} dataKey="value" nameKey="name" innerRadius="58%" outerRadius="74%" paddingAngle={2} strokeWidth={2} stroke="var(--card)"
            label={RatioLabel} labelLine={false}>
            {data.map(row => <Cell key={row.name} fill={factoryColors[row.name] ?? "var(--chart-previous)"}/>)}
          </Pie>
        </PieChart>
      </ResponsiveContainer>
      <div className="donut-center"><b>{fmtTotal(total)}</b><small>{def.unit}</small></div>
    </div>
  </div>;
}

export function EnergyComposition({ rows, label, className = "span-all" }: { rows: AnyData[]; label?: string; className?: string }) {
  if (!rows?.length) return null;
  const legendFactories = rows.map(row => String(row.factory));
  return <article className={`card chart-card ${className}`}>
    <header className="card-title">
      <h3>공장별 에너지 사용 비율</h3>
      <div className="card-title-side"><span>{label ?? "당해 누계"}</span></div>
    </header>
    <div className="donut-grid">
      {donutDefs.map(def => <Donut key={def.key} rows={rows} def={def}/>)}
    </div>
    <div className="donut-legend">
      {legendFactories.map(name => <span key={name}><i style={{ background: factoryColors[name] ?? "var(--chart-previous)" }}/>{name}</span>)}
    </div>
  </article>;
}
