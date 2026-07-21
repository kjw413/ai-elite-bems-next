"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { RefreshCw, TrendingUp, X } from "lucide-react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { apiRequest, isAbortError, query } from "@/lib/bems-api";
import { downloadCsv } from "@/lib/bems-csv";
import { ToggleLegend, useSeriesToggle, type LegendItem } from "@/components/toggle-legend";

type AnyData = Record<string, any>;
type ItemOption = { code: string; name: string; actual: number };
type TrendItem = {
  code: string; name: string;
  series: { month: string; actual: number | null; prevYear: number | null }[];
  latest: { month: string; actual: number; mom: number | null; yoy: number | null } | null;
};

const MAX_COMPARE = 5;
const numberText = (value: unknown) => typeof value === "number" ? value.toLocaleString("ko-KR", { maximumFractionDigits: 1 }) : "-";
const changeText = (value: number | null | undefined) => value == null ? "-" : `${value > 0 ? "+" : ""}${value.toFixed(1)}%`;
const tooltipStyle = { contentStyle: { borderRadius: 10, border: "1px solid var(--line)", background: "var(--card)", fontSize: 12 } };
// 품목 비교 시 라인 색 — 시스템 카테고리 색 재사용, 최대 5개까지 서로 구분되는 색.
const compareColors = ["var(--chart-actual)", "var(--chart-predicted)", "var(--chart-power)", "var(--chart-water)", "var(--chart-production)"];

// 신규 섹션 — 단일 품목의 월별 추이(실적 + 전년 동월, 전월비·전년동월비 배지) 또는
// 여러(최대 5개) 품목의 월별 실적 비교. 백엔드 /production/item-trend에 위임한다.
export function ProductionItemTrend({ factory, date }: { factory: string; date: string }) {
  const [options, setOptions] = useState<ItemOption[]>([]);
  const [selectedCodes, setSelectedCodes] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [result, setResult] = useState<TrendItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const optionsController = useRef<AbortController | null>(null);
  const trendController = useRef<AbortController | null>(null);

  // 공장·기준일이 바뀌면 선택지(최근 12개월 실적 상위 100개)를 다시 불러온다.
  useEffect(() => {
    optionsController.current?.abort();
    const abort = new AbortController();
    optionsController.current = abort;
    apiRequest<{ items: ItemOption[] }>(`/production/items?${query({ factory, date })}`, { signal: abort.signal })
      .then(response => {
        if (abort.signal.aborted) return;
        const items = response.items ?? [];
        setOptions(items);
        setSelectedCodes(current => {
          const kept = current.filter(code => items.some(item => item.code === code));
          return kept.length ? kept : (items[0] ? [items[0].code] : []);
        });
      })
      // 선택지 조회 실패(예: 예시 데이터 모드)는 조용히 빈 상태로 둔다 — 상단 배너가 이미 안내.
      .catch(requestError => { if (!isAbortError(requestError)) setOptions([]); });
    return () => abort.abort();
  }, [factory, date]);

  const codesKey = selectedCodes.join(",");
  useEffect(() => {
    if (!codesKey) { setResult([]); return; }
    trendController.current?.abort();
    const abort = new AbortController();
    trendController.current = abort;
    setLoading(true);
    setError("");
    apiRequest<{ items: TrendItem[] }>(`/production/item-trend?${query({ factory, date, items: codesKey })}`, { signal: abort.signal })
      .then(response => { if (!abort.signal.aborted) setResult(response.items ?? []); })
      .catch(requestError => { if (!isAbortError(requestError)) setError(messageOf(requestError)); })
      .finally(() => { if (trendController.current === abort) setLoading(false); });
    return () => abort.abort();
  }, [factory, date, codesKey]);

  const isComparing = selectedCodes.length > 1;
  // 단일 모드: 실적 + 전년 동월 2개 라인. 비교 모드: 각 품목 실적 라인(최대 5개).
  const merged: AnyData[] = [];
  if (result.length) {
    const months = result[0].series.map(point => point.month);
    months.forEach((month, index) => {
      const row: AnyData = { month };
      if (isComparing) {
        result.forEach(item => { row[item.code] = item.series[index]?.actual ?? null; });
      } else {
        row.actual = result[0].series[index]?.actual ?? null;
        row.prevYear = result[0].series[index]?.prevYear ?? null;
      }
      merged.push(row);
    });
  }
  const nameOf = (code: string) => options.find(item => item.code === code)?.name ?? code;
  const seriesLegend = useSeriesToggle();
  const seriesItems: LegendItem[] = isComparing
    ? selectedCodes.map((code, index) => ({ key: code, label: nameOf(code), color: compareColors[index] }))
    : [{ key: "prevYear", label: "전년 동월", color: "var(--chart-previous)" }, { key: "actual", label: "실적", color: "var(--chart-production)" }];

  const filteredOptions = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    if (!keyword) return options;
    return options.filter(item => item.name.toLowerCase().includes(keyword) || item.code.includes(keyword));
  }, [options, search]);

  function toggleCode(code: string) {
    setSelectedCodes(current => {
      if (current.includes(code)) return current.filter(item => item !== code);
      if (current.length >= MAX_COMPARE) return current;
      return [...current, code];
    });
  }

  return <article className="card chart-card span-all">
    <header className="card-title"><h3>품목 실적 추이 · 비교</h3><div className="card-title-side">
      {merged.length > 0 && <button type="button" className="csv-button" onClick={() => downloadCsv(`item_trend_${codesKey}`, merged, isComparing ? ["month", ...selectedCodes] : ["month", "actual", "prevYear"], isComparing ? { month: "월", ...Object.fromEntries(selectedCodes.map(code => [code, nameOf(code)])) } : { month: "월", actual: "실적(ton)", prevYear: "전년 동월(ton)" })}><TrendingUp size={13}/>CSV</button>}
      <span>최근 13개월 · ton</span>
    </div></header>
    <div>
      <div className="item-multiselect">
        <div className="item-multiselect-head">
          <input type="text" placeholder="품목명으로 검색" value={search} onChange={event => setSearch(event.target.value)}/>
          <span>{selectedCodes.length}/{MAX_COMPARE} 선택</span>
        </div>
        <div className="item-option-list" role="listbox" aria-multiselectable="true" aria-label="비교할 품목 선택 (최대 5개)">
          {filteredOptions.map(item => {
            const checked = selectedCodes.includes(item.code);
            const disabled = !checked && selectedCodes.length >= MAX_COMPARE;
            return <label key={item.code} className={`item-option${disabled ? " disabled" : ""}`}>
              <input type="checkbox" checked={checked} disabled={disabled} onChange={() => toggleCode(item.code)}/>
              <span>{item.name}</span>
            </label>;
          })}
          {filteredOptions.length === 0 && <p className="panel-copy">검색 결과가 없습니다.</p>}
        </div>
      </div>
      {selectedCodes.length > 0 && <div className="chart-legend">
        {selectedCodes.map((code, index) => <button type="button" key={code} className="legend-chip" onClick={() => toggleCode(code)} title={`${nameOf(code)} 선택 해제`}>
          <i style={{ background: compareColors[index] }}/>{nameOf(code)}<X size={11}/>
        </button>)}
      </div>}
      {error && <div className="form-message error">{error}</div>}
      {loading && <div className="loading inline-loading"><RefreshCw className="spin"/>불러오는 중입니다.</div>}
      {!loading && !error && merged.length === 0 && <p className="panel-copy">표시할 품목을 선택하세요.</p>}
      {!loading && !error && merged.length > 0 && <>
        {!isComparing && result[0]?.latest && <div className="item-latest">
          <div><span>최근월({result[0].latest.month})</span><b>{numberText(result[0].latest.actual)} ton</b></div>
          <div><span>전월 대비</span><b className={(result[0].latest.mom ?? 0) >= 0 ? "good" : "bad"}>{changeText(result[0].latest.mom)}</b></div>
          <div><span>전년 동월 대비</span><b className={(result[0].latest.yoy ?? 0) >= 0 ? "good" : "bad"}>{changeText(result[0].latest.yoy)}</b></div>
        </div>}
        <div className="chart"><ResponsiveContainer width="100%" height="100%">
          <LineChart data={merged}><CartesianGrid vertical={false}/><XAxis dataKey="month" tick={{ fontSize: 11 }}/><YAxis tick={{ fontSize: 11 }}/><Tooltip {...tooltipStyle} formatter={(value: unknown) => numberText(value)}/>
            {isComparing
              ? selectedCodes.map((code, index) => !seriesLegend.isHidden(code) && <Line key={code} type="linear" dataKey={code} name={nameOf(code)} stroke={compareColors[index]} strokeWidth={2} dot={{ r: 3, fill: compareColors[index], stroke: "var(--card)", strokeWidth: 2 }} activeDot={{ r: 5 }} connectNulls/>)
              : <>{!seriesLegend.isHidden("prevYear") && <Line type="linear" dataKey="prevYear" name="전년 동월" stroke="var(--chart-previous)" strokeWidth={2} strokeDasharray="5 4" dot={false} connectNulls/>}{!seriesLegend.isHidden("actual") && <Line type="linear" dataKey="actual" name="실적" stroke="var(--chart-production)" strokeWidth={2} dot={{ r: 3, fill: "var(--chart-production)", stroke: "var(--card)", strokeWidth: 2 }} activeDot={{ r: 5 }} connectNulls/>}</>}
          </LineChart>
        </ResponsiveContainer></div>
        <ToggleLegend items={seriesItems} hidden={seriesLegend.hidden} onToggle={seriesLegend.toggle}/>
      </>}
    </div>
  </article>;
}

function messageOf(error: unknown) {
  return error instanceof Error ? error.message : "품목 추이를 불러오지 못했습니다.";
}
