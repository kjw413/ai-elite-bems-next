"use client";

import { useEffect, useState } from "react";
import { ReferenceLine } from "recharts";
import { apiRequest, isAbortError, query } from "@/lib/bems-api";

export type FieldEvent = {
  id: number; factory: string; event_date: string;
  target: string; tag: string; severity: string; note: string;
};

// 이벤트의 target(power/fuel/water/wastewater/production/overall)과 차트 지표를 잇는다.
// overall(전반)은 어떤 차트에도 붙고, 그 외에는 같은 지표 차트에만 붙는다 —
// 전력 차트에 용수 센서고장 메모가 뜨면 오히려 판독을 방해한다.
export type EventTarget = "power" | "fuel" | "water" | "wastewater" | "production" | "overall";

const SEVERITY_COLOR: Record<string, string> = {
  critical: "var(--red)", warn: "var(--chart-amber)", info: "var(--chart-previous)",
};

// 선택 공장·기간의 현장 이벤트 — 차트 마커와 툴팁이 함께 쓴다.
export function useFieldEvents(factory: string, dateFrom: string, dateTo: string, enabled = true) {
  const [events, setEvents] = useState<FieldEvent[]>([]);
  useEffect(() => {
    if (!enabled || !dateFrom || !dateTo) { setEvents([]); return; }
    const abort = new AbortController();
    apiRequest<{ events: FieldEvent[] }>(
      `/events?${query({ factory, date_from: dateFrom, date_to: dateTo, limit: "200" })}`,
      { signal: abort.signal },
    )
      .then(response => { if (!abort.signal.aborted) setEvents(response.events ?? []); })
      // 이벤트는 보조 정보 — 조회 실패해도 차트 자체는 그대로 보여준다.
      .catch(requestError => { if (!isAbortError(requestError)) setEvents([]); });
    return () => abort.abort();
  }, [factory, dateFrom, dateTo, enabled]);
  return events;
}

// 이벤트를 x축 라벨(예: "07.12", "7월")에 맞춰 묶는다. labelOf는 화면의 축 포맷과 같아야 한다.
export function groupEventsByLabel(
  events: FieldEvent[], target: EventTarget, labelOf: (isoDate: string) => string,
): Map<string, FieldEvent[]> {
  const grouped = new Map<string, FieldEvent[]>();
  for (const event of events) {
    const eventTarget = String(event.target ?? "overall");
    if (eventTarget !== "overall" && eventTarget !== target) continue;
    const iso = String(event.event_date ?? "").slice(0, 10);
    if (!iso) continue;
    const label = labelOf(iso);
    if (!label) continue;
    const bucket = grouped.get(label);
    if (bucket) bucket.push(event); else grouped.set(label, [event]);
  }
  return grouped;
}

// 일별 축("07.12")과 월별 축("7월")용 기본 라벨 변환 — 화면 축 포맷과 짝을 맞춰 쓴다.
export const dayLabelOf = (iso: string) => `${iso.slice(5, 7)}.${iso.slice(8, 10)}`;
export const monthLabelOf = (iso: string) => `${Number(iso.slice(5, 7))}월`;

// 마커 깃발 — recharts가 label로 렌더링하며 viewBox를 넘겨준다.
// SVG <title>로 네이티브 hover 툴팁을 달아, 차트 Tooltip을 갈아엎지 않고도 메모를 읽게 한다.
function EventFlag({ viewBox, events }: { viewBox?: { x?: number; y?: number }; events: FieldEvent[] }) {
  const x = viewBox?.x ?? 0;
  const y = viewBox?.y ?? 0;
  const top = events[0];
  const color = SEVERITY_COLOR[String(top.severity)] ?? SEVERITY_COLOR.info;
  const text = events
    .map(event => `[${event.factory}] ${event.tag}: ${event.note}`)
    .join("\n");
  return <g transform={`translate(${x}, ${y})`} style={{ pointerEvents: "auto", cursor: "help" }}>
    <title>{`${top.event_date?.slice(0, 10)}\n${text}`}</title>
    {/* 히트 영역을 넉넉히 둬 작은 깃발에도 hover가 잡히게 한다 */}
    <rect x={-9} y={0} width={18} height={18} fill="transparent"/>
    <circle cx={0} cy={7} r={5} fill={color} stroke="var(--card)" strokeWidth={1.5}/>
    {events.length > 1 && <text x={0} y={10.5} textAnchor="middle" fontSize={8} fill="var(--card)" fontWeight={700}>{events.length}</text>}
  </g>;
}

// 차트 자식으로 펼쳐 넣는다: {...eventMarkers(grouped)} 가 아니라 배열을 그대로 children에 둔다.
// recharts는 ReferenceLine을 직계 자식으로 요구하므로 컴포넌트로 감싸지 않는다.
export function eventMarkers(grouped: Map<string, FieldEvent[]>) {
  return Array.from(grouped.entries()).map(([label, events]) => (
    <ReferenceLine key={`event-${label}`} x={label} stroke={SEVERITY_COLOR[String(events[0].severity)] ?? SEVERITY_COLOR.info}
      strokeDasharray="3 3" strokeOpacity={0.75} label={<EventFlag events={events}/>}/>
  ));
}

// 범례 아래 등에 붙이는 안내 — 마커가 무엇인지 모르면 점이 노이즈로 보인다.
export function EventMarkerHint({ count }: { count: number }) {
  if (!count) return null;
  return <p className="event-hint">현장 이벤트 {count}건이 차트에 점선으로 표시됩니다 — 점에 마우스를 올리면 메모를 볼 수 있습니다.</p>;
}
