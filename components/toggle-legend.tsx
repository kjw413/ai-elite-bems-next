"use client";

import { useState } from "react";

export type LegendItem = { key: string; label: string; color: string };

// 차트 범례를 on/off 토글 버튼으로 — 끄면 recharts 트리에서 해당 시리즈(Line/Bar/Area)
// 자체를 렌더링하지 않는다(숨김 처리가 아니라 완전히 제거). YAxis의 domain=["auto","auto"]
// (또는 recharts 기본 auto 동작)는 실제로 마운트된 시리즈만 보고 재계산하므로, 이렇게 하면
// 별도 로직 없이 "끈 시리즈를 뺀 나머지로 autoscale"이 자연히 만족된다.
export function useSeriesToggle(initialHidden: string[] = []) {
  const [hidden, setHidden] = useState<Set<string>>(() => new Set(initialHidden));
  const toggle = (key: string) => setHidden(prev => {
    const next = new Set(prev);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });
  return { hidden, toggle, isHidden: (key: string) => hidden.has(key) };
}

export function ToggleLegend({ items, hidden, onToggle }: {
  items: LegendItem[]; hidden: Set<string>; onToggle: (key: string) => void;
}) {
  if (!items.length) return null;
  return <div className="chart-legend" role="group" aria-label="범례 표시 전환">
    {items.map(item => {
      const off = hidden.has(item.key);
      return <button type="button" key={item.key} className={`legend-chip${off ? " off" : ""}`} aria-pressed={!off}
        onClick={() => onToggle(item.key)} title={off ? `${item.label} 표시` : `${item.label} 숨기기`}>
        <i style={{ background: item.color }}/>{item.label}
      </button>;
    })}
  </div>;
}
