"use client";

// 차트 아래 데이터 표만 접는 토글 — 차트는 항상 노출하고 표만 기본 접힘으로 둔다.
// 세부 데이터를 원할 때만 펼쳐 보게 해 세로 공간을 아낀다.
export function DataToggle({ label = "데이터 표 보기", children }: { label?: string; children: React.ReactNode }) {
  return <details className="data-toggle"><summary>{label}</summary><div className="data-toggle-body">{children}</div></details>;
}
