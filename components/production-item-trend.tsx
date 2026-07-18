"use client";

import { useEffect, useRef, useState } from "react";
import { RefreshCw, TrendingUp } from "lucide-react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { apiRequest, isAbortError, query } from "@/lib/bems-api";
import { downloadCsv } from "@/lib/bems-csv";

type AnyData = Record<string, any>;
type ItemOption = { code: string; name: string; actual: number };
type TrendItem = {
  code: string; name: string;
  series: { month: string; actual: number | null; prevYear: number | null }[];
  latest: { month: string; actual: number; mom: number | null; yoy: number | null } | null;
};

const numberText = (value: unknown) => typeof value === "number" ? value.toLocaleString("ko-KR", { maximumFractionDigits: 1 }) : "-";
const changeText = (value: number | null | undefined) => value == null ? "-" : `${value > 0 ? "+" : ""}${value.toFixed(1)}%`;
const tooltipStyle = { contentStyle: { borderRadius: 10, border: "1px solid var(--line)", background: "var(--card)", fontSize: 12 } };
// 두 품목 비교 시 라인 색 — 시스템 카테고리 색 재사용(파랑/보라)
const compareColors = ["var(--chart-actual)", "var(--chart-predicted)"];

// 신규 섹션 — 단일 품목의 월별 추이(실적 + 전년 동월, 전월비·전년동월비 배지) 또는
// 두 품목의 월별 실적 비교. 백엔드 /production/item-trend에 위임한다.
export function ProductionItemTrend({ factory, date }: { factory: string; date: string }) {
  const [compareMode, setCompareMode] = useState(false);
  const [options, setOptions] = useState<ItemOption[]>([]);
  const [primary, setPrimary] = useState("");
  const [secondary, setSecondary] = useState("");
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
        setPrimary(current => items.some(item => item.code === current) ? current : (items[0]?.code ?? ""));
        setSecondary(current => items.some(item => item.code === current) ? current : (items[1]?.code ?? ""));
      })
      // 선택지 조회 실패(예: 예시 데이터 모드)는 조용히 빈 상태로 둔다 — 상단 배너가 이미 안내.
      .catch(requestError => { if (!isAbortError(requestError)) setOptions([]); });
    return () => abort.abort();
  }, [factory, date]);

  const codes = compareMode ? [primary, secondary].filter(Boolean) : [primary].filter(Boolean);
  const codesKey = codes.join(",");
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

  // 단일 모드: 실적 + 전년 동월 2개 라인. 비교 모드: 각 품목 실적 라인.
  const merged: AnyData[] = [];
  if (result.length) {
    const months = result[0].series.map(point => point.month);
    months.forEach((month, index) => {
      const row: AnyData = { month };
      if (compareMode) {
        result.forEach(item => { row[item.code] = item.series[index]?.actual ?? null; });
      } else {
        row.actual = result[0].series[index]?.actual ?? null;
        row.prevYear = result[0].series[index]?.prevYear ?? null;
      }
      merged.push(row);
    });
  }
  const nameOf = (code: string) => options.find(item => item.code === code)?.name ?? code;

  return <details className="card chart-card span-all collapsible" open>
    <summary className="card-title"><h3>품목 실적 추이 · 비교</h3><div className="card-title-side" onClick={event => event.preventDefault()}>
      <label className="check-toggle"><input type="checkbox" checked={compareMode} onChange={event => setCompareMode(event.target.checked)}/>두 품목 비교</label>
      {merged.length > 0 && <button type="button" className="csv-button" onClick={() => downloadCsv(`item_trend_${codesKey}`, merged, compareMode ? ["month", ...codes] : ["month", "actual", "prevYear"], compareMode ? { month: "월", ...Object.fromEntries(codes.map(code => [code, nameOf(code)])) } : { month: "월", actual: "실적(ton)", prevYear: "전년 동월(ton)" })}><TrendingUp size={13}/>CSV</button>}
      <span>최근 13개월 · ton</span>
    </div></summary>
    <div className="collapsible-body">
      <div className="item-picker">
        <label><span>{compareMode ? "품목 A" : "품목"}</span><select value={primary} onChange={event => setPrimary(event.target.value)}>{options.map(item => <option key={item.code} value={item.code}>{item.name}</option>)}</select></label>
        {compareMode && <label><span>품목 B</span><select value={secondary} onChange={event => setSecondary(event.target.value)}>{options.map(item => <option key={item.code} value={item.code}>{item.name}</option>)}</select></label>}
      </div>
      {error && <div className="form-message error">{error}</div>}
      {loading && <div className="loading inline-loading"><RefreshCw className="spin"/>불러오는 중입니다.</div>}
      {!loading && !error && merged.length === 0 && <p className="panel-copy">표시할 품목을 선택하세요.</p>}
      {!loading && !error && merged.length > 0 && <>
        {!compareMode && result[0]?.latest && <div className="item-latest">
          <div><span>최근월({result[0].latest.month})</span><b>{numberText(result[0].latest.actual)} ton</b></div>
          <div><span>전월 대비</span><b className={(result[0].latest.mom ?? 0) >= 0 ? "good" : "bad"}>{changeText(result[0].latest.mom)}</b></div>
          <div><span>전년 동월 대비</span><b className={(result[0].latest.yoy ?? 0) >= 0 ? "good" : "bad"}>{changeText(result[0].latest.yoy)}</b></div>
        </div>}
        <div className="chart"><ResponsiveContainer width="100%" height="100%">
          <LineChart data={merged}><CartesianGrid strokeDasharray="3 3"/><XAxis dataKey="month" tick={{ fontSize: 11 }}/><YAxis tick={{ fontSize: 11 }}/><Tooltip {...tooltipStyle} formatter={(value: unknown) => numberText(value)}/><Legend/>
            {compareMode
              ? codes.map((code, index) => <Line key={code} type="linear" dataKey={code} name={nameOf(code)} stroke={compareColors[index]} strokeWidth={2.5} dot={{ r: 2.5 }} connectNulls/>)
              : <><Line type="linear" dataKey="prevYear" name="전년 동월" stroke="var(--chart-previous)" strokeWidth={2} strokeDasharray="5 4" dot={false} connectNulls/><Line type="linear" dataKey="actual" name="실적" stroke="var(--chart-production)" strokeWidth={3} dot={{ r: 2.5 }} connectNulls/></>}
          </LineChart>
        </ResponsiveContainer></div>
      </>}
    </div>
  </details>;
}

function messageOf(error: unknown) {
  return error instanceof Error ? error.message : "품목 추이를 불러오지 못했습니다.";
}
