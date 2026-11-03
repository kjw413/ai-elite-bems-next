"use client";

import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { eventMarkers, type FieldEvent } from "@/components/event-markers";
import { ToggleLegend, useSeriesToggle } from "@/components/toggle-legend";

type DailyPoint = Record<string, any>;
const productionColor = "var(--chart-production)";
const format = (value: unknown) => typeof value === "number" ? value.toLocaleString("ko-KR", { maximumFractionDigits: 2 }) : "-";
const axisFormat = (value: number) => value.toLocaleString("ko-KR", { notation: "compact", maximumFractionDigits: 1 });

function DailyTooltip({ active, payload, valueKey, valueLabel, unit, color }: {
  active?: boolean; payload?: readonly { payload?: DailyPoint }[];
  valueKey: string; valueLabel: string; unit: string; color: string;
}) {
  const row = payload?.[0]?.payload;
  if (!active || !row) return null;
  return <div className="daily-production-tooltip">
    <strong>{row.date}</strong>
    <div><span style={{ color }}>{valueLabel}</span><b>{format(row[valueKey])} <small>{unit}</small></b></div>
    <div><span style={{ color: productionColor }}>생산량</span><b>{format(row.productionTon)} <small>ton</small></b></div>
  </div>;
}

// 에너지 지표와 생산량은 단위가 다르므로 각 축에 두고 같은 날짜에 겹쳐 비교한다.
export function DailyProductionTrend({ rows, valueKey, valueLabel, unit, color, events, autoDomain = false }: {
  rows: DailyPoint[]; valueKey: string; valueLabel: string; unit: string; color: string;
  events: Map<string, FieldEvent[]>; autoDomain?: boolean;
}) {
  const legend = useSeriesToggle();
  return <>
    <div className="daily-production-labels">
      <b style={{ color }}>{valueLabel} <small>{unit} · 왼쪽 축</small></b>
      <b style={{ color: productionColor }}>생산량 <small>ton · 오른쪽 축</small></b>
    </div>
    <div className="chart daily-production-chart" aria-label={`일별 ${valueLabel}·생산량 차트`}><ResponsiveContainer width="100%" height="100%">
      <LineChart data={rows} margin={{ top: 10, right: 8, left: 0, bottom: 0 }}>
        <CartesianGrid vertical={false}/>
        <XAxis dataKey="date" interval="preserveStartEnd" minTickGap={18}/>
        <YAxis width={56} domain={autoDomain ? ["auto", "auto"] : [0, "auto"]} tickFormatter={axisFormat}/>
        <YAxis yAxisId="production" orientation="right" width={56} tickFormatter={axisFormat}/>
        <Tooltip content={<DailyTooltip valueKey={valueKey} valueLabel={valueLabel} unit={unit} color={color}/>}/>
        <Line type="linear" dataKey={valueKey} name={`${valueLabel}(${unit})`} stroke={color} strokeWidth={2} hide={legend.isHidden(valueKey)}
          dot={{ r: 3, fill: color, stroke: "var(--card)", strokeWidth: 2 }} activeDot={{ r: 5 }} connectNulls={false}/>
        <Line yAxisId="production" type="linear" dataKey="productionTon" name="생산량(ton)" stroke={productionColor} strokeWidth={2} strokeDasharray="5 3"
          hide={legend.isHidden("productionTon")} dot={{ r: 3, fill: productionColor, stroke: "var(--card)", strokeWidth: 2 }} activeDot={{ r: 5 }} connectNulls={false}/>
        {eventMarkers(events)}
      </LineChart>
    </ResponsiveContainer></div>
    <ToggleLegend items={[
      { key: valueKey, label: `${valueLabel}(${unit}) · 왼쪽 축`, color },
      { key: "productionTon", label: "생산량(ton) · 오른쪽 축 · 점선", color: productionColor },
    ]} hidden={legend.hidden} onToggle={legend.toggle}/>
    <p className="daily-production-note">두 선은 각각 왼쪽·오른쪽 축을 따릅니다. 날짜별 증감 방향을 비교하고, 마우스를 올려 실제 값을 확인하세요.</p>
  </>;
}
