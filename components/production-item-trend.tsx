"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, RefreshCw, TrendingUp, X } from "lucide-react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { apiRequest, isAbortError, query } from "@/lib/bems-api";
import { downloadCsv } from "@/lib/bems-csv";
import { ToggleLegend, useSeriesToggle, type LegendItem } from "@/components/toggle-legend";
import { PivotTable, type PivotRow } from "@/components/pivot-table";
import { DataToggle } from "@/components/data-toggle";

type AnyData = Record<string, any>;
type ProductionMode = "month" | "range" | "year";
type ItemOption = { code: string; name: string; category: string; actual: number };
type TrendPoint = { period: string; actual: number | null; prevYear: number | null };
type TrendItem = {
  code: string; name: string;
  series: TrendPoint[];
  latest: { period: string; actual: number; prevChange: number | null; yoyChange: number | null } | null;
};
type ModeProps = { factory: string; date: string; mode: ProductionMode; rangeFrom: string; rangeTo: string };

const MAX_COMPARE = 5;
const CATEGORY_ORDER = ["IC", "MY", "FM", "SN", "ETC"];
const CATEGORY_LABELS: Record<string, string> = { IC: "IC (아이스크림)", MY: "MY (유음료)", FM: "FM (발효유)", SN: "SN (스낵)", ETC: "기타" };
// 품목 비교 라인 색 — 최대 5개가 서로 구분되도록 시스템 카테고리 색을 재사용한다.
const compareColors = ["var(--chart-actual)", "var(--chart-predicted)", "var(--chart-power)", "var(--chart-water)", "var(--chart-production)"];

const numberText = (value: unknown) => typeof value === "number" ? value.toLocaleString("ko-KR", { maximumFractionDigits: 1 }) : "-";
const changeText = (value: number | null | undefined) => value == null ? "-" : `${value > 0 ? "+" : ""}${value.toFixed(1)}%`;
const tooltipStyle = { contentStyle: { borderRadius: 10, border: "1px solid var(--line)", background: "var(--card)", fontSize: 12 } };
const changeOf = (current: number | null, previous: number | null) =>
  current == null || previous == null || previous === 0 ? null : Math.round((current / previous - 1) * 1000) / 10;
// x축이 그 탭의 시간 범위·단위를 따르므로, 표 머리글·배지 문구도 같은 단위로 맞춘다.
const axisLabel = (mode: ProductionMode) => mode === "year" ? "월" : "일자";
const prevLabel = (mode: ProductionMode) => mode === "year" ? "전월 대비" : "전일 대비";
const latestLabel = (mode: ProductionMode) => mode === "year" ? "최근월" : "최근 일자";
const rangeMeta = (mode: ProductionMode) =>
  mode === "year" ? "선택 연도 1~12월 · ton" : mode === "range" ? "선택 기간 일별 · ton" : "선택 월 일별 · ton";

function messageOf(error: unknown) {
  return error instanceof Error ? error.message : "품목 추이를 불러오지 못했습니다.";
}

// 선택 공장·기준일의 품목 후보(최근 12개월 실적 상위) — 두 섹션이 각자 호출한다.
function useItemOptions(factory: string, date: string) {
  const [options, setOptions] = useState<ItemOption[]>([]);
  useEffect(() => {
    const abort = new AbortController();
    apiRequest<{ items: ItemOption[] }>(`/production/items?${query({ factory, date })}`, { signal: abort.signal })
      .then(response => { if (!abort.signal.aborted) setOptions(response.items ?? []); })
      // 선택지 조회 실패(예: 예시 데이터 모드)는 조용히 빈 상태로 둔다 — 상단 배너가 이미 안내.
      .catch(requestError => { if (!isAbortError(requestError)) setOptions([]); });
    return () => abort.abort();
  }, [factory, date]);
  return options;
}

function useItemTrend(factory: string, date: string, mode: ProductionMode, rangeFrom: string, rangeTo: string, codes: string[]) {
  const [result, setResult] = useState<TrendItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const codesKey = codes.join(",");
  useEffect(() => {
    if (!codesKey) { setResult([]); setError(""); return; }
    const abort = new AbortController();
    setLoading(true);
    setError("");
    apiRequest<{ items: TrendItem[] }>(`/production/item-trend?${query({
      factory, date, items: codesKey, mode,
      ...(mode === "range" ? { date_from: rangeFrom, date_to: rangeTo } : {}),
    })}`, { signal: abort.signal })
      .then(response => { if (!abort.signal.aborted) setResult(response.items ?? []); })
      .catch(requestError => { if (!isAbortError(requestError)) { setError(messageOf(requestError)); setResult([]); } })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [factory, date, mode, rangeFrom, rangeTo, codesKey]);
  return { result, loading, error };
}

// 제품유형 선행 필터 — 선택 공장에 실제로 존재하는 유형만 노출한다.
function CategoryFilter({ options, value, onChange }: { options: ItemOption[]; value: string; onChange: (value: string) => void }) {
  const present = useMemo(() => new Set(options.map(item => item.category)), [options]);
  return <label><span>제품유형</span>
    <select value={value} onChange={event => onChange(event.target.value)}>
      <option value="ALL">전체</option>
      {CATEGORY_ORDER.filter(key => present.has(key)).map(key => <option key={key} value={key}>{CATEGORY_LABELS[key]}</option>)}
    </select>
  </label>;
}

// 타이핑 검색 + 드롭다운 콤보박스 — 선택하면 입력창은 비고, 선택 결과는 칩으로만 보여
// 목록이 상시 펼쳐져 좌측 공간을 잡아먹지 않게 한다.
function ItemCombobox({ options, onSelect, placeholder, disabled = false }: {
  options: ItemOption[]; onSelect: (code: string) => void; placeholder: string; disabled?: boolean;
}) {
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const closeOnOutside = (event: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", closeOnOutside);
    return () => document.removeEventListener("mousedown", closeOnOutside);
  }, [open]);

  const filtered = useMemo(() => {
    const keyword = text.trim().toLowerCase();
    const matched = keyword
      ? options.filter(item => item.name.toLowerCase().includes(keyword) || item.code.includes(keyword))
      : options;
    return matched.slice(0, 50);
  }, [options, text]);

  return <div className="item-combobox" ref={boxRef}>
    <div className="item-combobox-input">
      <input type="text" role="combobox" aria-expanded={open} aria-autocomplete="list" placeholder={placeholder}
        disabled={disabled} value={text}
        onFocus={() => setOpen(true)}
        onChange={event => { setText(event.target.value); setOpen(true); }}
        onKeyDown={event => { if (event.key === "Escape") setOpen(false); }}/>
      <ChevronDown size={14}/>
    </div>
    {open && !disabled && <ul className="item-combobox-list" role="listbox">
      {filtered.map(item => <li key={item.code}>
        <button type="button" onClick={() => { onSelect(item.code); setText(""); setOpen(false); }}>
          <span>{item.name}</span><em>{item.category}</em>
        </button>
      </li>)}
      {filtered.length === 0 && <li className="item-combobox-empty">검색 결과가 없습니다.</li>}
    </ul>}
  </div>;
}

// ── 품목간 비교 (최대 5개) — 전년비 없이 선택 품목의 실적만 그린다 ──────────────
export function ProductionItemTrend({ factory, date, mode, rangeFrom, rangeTo }: ModeProps) {
  const options = useItemOptions(factory, date);
  const [category, setCategory] = useState("ALL");
  const [selectedCodes, setSelectedCodes] = useState<string[]>([]);
  const seriesLegend = useSeriesToggle();

  // 공장·기준일이 바뀌어 후보가 갈리면 유효한 선택만 남기고, 비면 실적 1위를 채운다.
  useEffect(() => {
    setSelectedCodes(current => {
      const kept = current.filter(code => options.some(item => item.code === code));
      if (kept.length) return kept;
      return options[0] ? [options[0].code] : [];
    });
  }, [options]);

  const { result, loading, error } = useItemTrend(factory, date, mode, rangeFrom, rangeTo, selectedCodes);
  const nameOf = (code: string) => options.find(item => item.code === code)?.name ?? code;
  const pickable = useMemo(
    () => options.filter(item => (category === "ALL" || item.category === category) && !selectedCodes.includes(item.code)),
    [options, category, selectedCodes],
  );

  const periods = result[0]?.series.map(point => point.period) ?? [];
  const merged: AnyData[] = periods.map((period, index) => {
    const row: AnyData = { period };
    result.forEach(item => { row[item.code] = item.series[index]?.actual ?? null; });
    return row;
  });
  const legendItems: LegendItem[] = result.map((item, index) => ({ key: item.code, label: item.name, color: compareColors[index] }));
  const pivotRows: PivotRow[] = result.map(item => {
    const values = item.series.map(point => point.actual);
    return { key: item.code, label: `${item.name}(ton)`, values, total: values.reduce<number>((acc, value) => acc + (value ?? 0), 0) };
  });

  function toggleCode(code: string) {
    setSelectedCodes(current => current.includes(code)
      ? current.filter(item => item !== code)
      : current.length >= MAX_COMPARE ? current : [...current, code]);
  }

  return <article className="card chart-card span-all">
    <header className="card-title"><h3>품목간 실적 비교</h3><div className="card-title-side">
      {merged.length > 0 && <button type="button" className="csv-button"
        onClick={() => downloadCsv(`item_compare_${mode}_${selectedCodes.join("-")}`, merged, ["period", ...selectedCodes],
          { period: axisLabel(mode), ...Object.fromEntries(selectedCodes.map(code => [code, `${nameOf(code)}(ton)`])) })}>
        <TrendingUp size={13}/>CSV</button>}
      <span>{rangeMeta(mode)}</span>
    </div></header>
    <div className="item-filter-row">
      <CategoryFilter options={options} value={category} onChange={setCategory}/>
      <label><span>품목 검색 (최대 {MAX_COMPARE}개)</span>
        <ItemCombobox options={pickable} onSelect={toggleCode} disabled={selectedCodes.length >= MAX_COMPARE}
          placeholder={selectedCodes.length >= MAX_COMPARE ? `최대 ${MAX_COMPARE}개까지 선택` : "품목명 입력"}/>
      </label>
    </div>
    {selectedCodes.length > 0 && <div className="chart-legend">
      {selectedCodes.map((code, index) => <button type="button" key={code} className="legend-chip"
        onClick={() => toggleCode(code)} title={`${nameOf(code)} 선택 해제`}>
        <i style={{ background: compareColors[index] }}/>{nameOf(code)}<X size={11}/>
      </button>)}
    </div>}
    {error && <div className="form-message error">{error}</div>}
    {loading && <div className="loading inline-loading"><RefreshCw className="spin"/>불러오는 중입니다.</div>}
    {!loading && !error && merged.length === 0 && <p className="panel-copy">비교할 품목을 선택하세요.</p>}
    {!loading && !error && merged.length > 0 && <>
      <div className="chart"><ResponsiveContainer width="100%" height="100%">
        <LineChart data={merged}><CartesianGrid vertical={false}/><XAxis dataKey="period" tick={{ fontSize: 11 }} interval="preserveStartEnd" minTickGap={18}/><YAxis tick={{ fontSize: 11 }}/>
          <Tooltip {...tooltipStyle} formatter={(value: unknown) => numberText(value)}/>
          {result.map((item, index) => !seriesLegend.isHidden(item.code) && <Line key={item.code} type="linear" dataKey={item.code} name={item.name}
            stroke={compareColors[index]} strokeWidth={2} dot={{ r: 3, fill: compareColors[index], stroke: "var(--card)", strokeWidth: 2 }}
            activeDot={{ r: 5 }} connectNulls/>)}
        </LineChart>
      </ResponsiveContainer></div>
      <ToggleLegend items={legendItems} hidden={seriesLegend.hidden} onToggle={seriesLegend.toggle}/>
      <DataToggle><PivotTable periods={periods} periodLabel={axisLabel(mode)} rows={pivotRows} totalLabel="누계(ton)"/></DataToggle>
    </>}
  </article>;
}

// ── 단일 품목 전년비 — 금년 vs 전년 동기 + 증감률 표 ──────────────────────────
export function ProductionItemYoy({ factory, date, mode, rangeFrom, rangeTo }: ModeProps) {
  const options = useItemOptions(factory, date);
  const [category, setCategory] = useState("ALL");
  const [code, setCode] = useState("");
  const legend = useSeriesToggle();

  useEffect(() => {
    setCode(current => options.some(item => item.code === current) ? current : (options[0]?.code ?? ""));
  }, [options]);

  const { result, loading, error } = useItemTrend(factory, date, mode, rangeFrom, rangeTo, code ? [code] : []);
  const item = result[0];
  const pickable = useMemo(
    () => options.filter(option => (category === "ALL" || option.category === category) && option.code !== code),
    [options, category, code],
  );

  const series = item?.series ?? [];
  const periods = series.map(point => point.period);
  const actualTotal = series.reduce((acc, point) => acc + (point.actual ?? 0), 0);
  const prevTotal = series.reduce((acc, point) => acc + (point.prevYear ?? 0), 0);
  const pivotRows: PivotRow[] = item ? [
    { key: "actual", label: "금년 실적(ton)", values: series.map(point => point.actual), total: actualTotal },
    { key: "prevYear", label: "전년 동기(ton)", values: series.map(point => point.prevYear), total: prevTotal },
    {
      key: "change", label: "증감률(%)",
      values: series.map(point => changeOf(point.actual, point.prevYear)),
      total: changeOf(actualTotal, prevTotal),
      format: value => value == null ? "-" : `${Number(value) > 0 ? "+" : ""}${Number(value).toFixed(1)}`,
      className: value => value == null ? undefined : Number(value) >= 0 ? "good" : "bad",
    },
  ] : [];
  const csvRows = series.map(point => ({
    period: point.period, actual: point.actual, prevYear: point.prevYear, change: changeOf(point.actual, point.prevYear),
  }));

  return <article className="card chart-card span-all">
    <header className="card-title"><h3>단일품목 전년비 비교</h3><div className="card-title-side">
      {series.length > 0 && <button type="button" className="csv-button"
        onClick={() => downloadCsv(`item_yoy_${mode}_${code}`, csvRows, ["period", "actual", "prevYear", "change"],
          { period: axisLabel(mode), actual: "금년(ton)", prevYear: "전년 동기(ton)", change: "증감률(%)" })}>
        <TrendingUp size={13}/>CSV</button>}
      <span>{rangeMeta(mode)}</span>
    </div></header>
    <div className="item-filter-row">
      <CategoryFilter options={options} value={category} onChange={setCategory}/>
      <label><span>품목 검색 (1개)</span>
        <ItemCombobox options={pickable} onSelect={setCode} placeholder="품목명 입력"/>
      </label>
      {item && <span className="period-chip">{item.name}</span>}
    </div>
    {error && <div className="form-message error">{error}</div>}
    {loading && <div className="loading inline-loading"><RefreshCw className="spin"/>불러오는 중입니다.</div>}
    {!loading && !error && series.length === 0 && <p className="panel-copy">전년비를 볼 품목을 선택하세요.</p>}
    {!loading && !error && series.length > 0 && <>
      {item?.latest && <div className="item-latest">
        <div><span>{latestLabel(mode)}({item.latest.period})</span><b>{numberText(item.latest.actual)} ton</b></div>
        <div><span>{prevLabel(mode)}</span><b className={(item.latest.prevChange ?? 0) >= 0 ? "good" : "bad"}>{changeText(item.latest.prevChange)}</b></div>
        <div><span>전년 동기 대비</span><b className={(item.latest.yoyChange ?? 0) >= 0 ? "good" : "bad"}>{changeText(item.latest.yoyChange)}</b></div>
      </div>}
      <div className="chart"><ResponsiveContainer width="100%" height="100%">
        <LineChart data={series}><CartesianGrid vertical={false}/><XAxis dataKey="period" tick={{ fontSize: 11 }} interval="preserveStartEnd" minTickGap={18}/><YAxis tick={{ fontSize: 11 }}/>
          <Tooltip {...tooltipStyle} formatter={(value: unknown) => numberText(value)}/>
          {!legend.isHidden("prevYear") && <Line type="linear" dataKey="prevYear" name="전년 동기" stroke="var(--chart-previous)" strokeWidth={2} strokeDasharray="5 4" dot={false} connectNulls/>}
          {!legend.isHidden("actual") && <Line type="linear" dataKey="actual" name="금년 실적" stroke="var(--chart-production)" strokeWidth={2} dot={{ r: 3, fill: "var(--chart-production)", stroke: "var(--card)", strokeWidth: 2 }} activeDot={{ r: 5 }} connectNulls/>}
        </LineChart>
      </ResponsiveContainer></div>
      <ToggleLegend items={[
        { key: "prevYear", label: "전년 동기", color: "var(--chart-previous)" },
        { key: "actual", label: "금년 실적", color: "var(--chart-production)" },
      ]} hidden={legend.hidden} onToggle={legend.toggle}/>
      <DataToggle><PivotTable periods={periods} periodLabel={axisLabel(mode)} rows={pivotRows} totalLabel="누계"/></DataToggle>
    </>}
  </article>;
}
