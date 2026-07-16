// CSV 내보내기 유틸 — legacy csv_download와 동일하게 utf-8-sig(BOM)로 저장해
// Excel에서 한글이 깨지지 않도록 한다. 값은 RFC 4180 규칙으로 이스케이프한다.

type Row = Record<string, unknown>;

function cell(value: unknown): string {
  if (value == null) return "";
  const text = typeof value === "number" ? String(value) : String(value);
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function toCsv(rows: Row[], columns: string[], labels: Record<string, string> = {}): string {
  const header = columns.map(column => cell(labels[column] ?? column)).join(",");
  const body = rows.map(row => columns.map(column => cell(row[column])).join(","));
  return [header, ...body].join("\r\n");
}

export function downloadCsv(
  filename: string,
  rows: Row[],
  columns: string[],
  labels: Record<string, string> = {},
) {
  if (typeof window === "undefined" || rows.length === 0) return;
  // BOM(\uFEFF) → Excel이 UTF-8로 인식 (legacy utf-8-sig 동등)
  const blob = new Blob(["\uFEFF" + toCsv(rows, columns, labels)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename.endsWith(".csv") ? filename : `${filename}.csv`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
