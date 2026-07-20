"use client";

// 시간축(일·월 등)이 많은 데이터 표를 위한 공용 전치 테이블.
// 원래 "시간이 행, 지표가 열"인 표를 "지표가 행, 시간이 열"로 뒤집어 세로 스크롤을
// 줄이고, 맨 오른쪽에 누계(또는 이미 계산된 가중 총계) 열을 덧붙인다.
// 총계는 단순 합산이 항상 맞는 것은 아니므로(예: 원단위·증감률은 가중 재계산 필요)
// 호출부가 이미 계산한 값을 row.total로 넘긴다 — 이 컴포넌트는 배치만 담당한다.

type PivotCell = number | string | null | undefined;

export type PivotRow = {
  key: string;
  label: string;
  values: PivotCell[];
  total?: PivotCell;
  format?: (value: PivotCell, index: number) => React.ReactNode; // index === -1 → 누계 열
  className?: (value: PivotCell, index: number) => string | undefined;
};

const defaultFormat = (value: PivotCell) =>
  value == null || value === "" ? "-" : typeof value === "number" ? value.toLocaleString("ko-KR", { maximumFractionDigits: 2 }) : value;

export function PivotTable({ periods, periodLabel = "구분", rows, totalLabel = "누계" }: {
  periods: string[]; periodLabel?: string; rows: PivotRow[]; totalLabel?: string;
}) {
  if (!rows.length) return null;
  return <div className="table-wrap pivot-table"><table>
    <thead><tr><th>{periodLabel}</th>{periods.map((period, index) => <th key={`${period}-${index}`}>{period}</th>)}<th className="pivot-total">{totalLabel}</th></tr></thead>
    <tbody>{rows.map(row => <tr key={row.key}>
      <th scope="row">{row.label}</th>
      {row.values.map((value, index) => <td key={index} className={row.className?.(value, index)}>{row.format ? row.format(value, index) : defaultFormat(value)}</td>)}
      <td className={`pivot-total ${row.className?.(row.total ?? null, -1) ?? ""}`}>{row.format ? row.format(row.total ?? null, -1) : defaultFormat(row.total ?? null)}</td>
    </tr>)}</tbody>
  </table></div>;
}
