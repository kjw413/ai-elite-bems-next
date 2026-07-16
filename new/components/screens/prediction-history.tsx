"use client";

import { useEffect, useRef, useState } from "react";
import { RefreshCw, Sparkles, Stethoscope, X } from "lucide-react";
import { apiRequest, isAbortError } from "@/lib/bems-api";
import { MarkdownReport } from "@/components/markdown-report";

type HistoryRow = {
  date: string;
  target: string;
  predicted?: number | null;
  lower?: number | null;
  upper?: number | null;
  actual?: number | null;
  status?: string;
};

type Diagnosis = {
  diagnosis: string;
  from_cache: boolean;
  model_used?: string | null;
  updated_at?: string | null;
};

const statusLabels: Record<string, string> = { inside: "정상", over: "상단 이탈", under: "하단 이탈", unknown: "미확정" };

function number(value: number | null | undefined) {
  return value == null ? "-" : value.toLocaleString("ko-KR", { maximumFractionDigits: 2 });
}

function messageOf(error: unknown) {
  return error instanceof Error ? error.message : "요청을 처리하지 못했습니다.";
}

export function PredictionHistory({ rows, factory, isAdmin, diagnosable }: { rows: HistoryRow[]; factory: string; isAdmin: boolean; diagnosable: boolean }) {
  const [selected, setSelected] = useState<HistoryRow | null>(null);
  const [diagnosis, setDiagnosis] = useState<Diagnosis | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    controller.current?.abort();
    setSelected(null);
    setDiagnosis(null);
    setError("");
    setLoading(false);
    return () => controller.current?.abort();
  }, [factory]);

  async function diagnose(row: HistoryRow, forceRefresh = false) {
    controller.current?.abort();
    const aborter = new AbortController();
    controller.current = aborter;
    setSelected(row);
    setDiagnosis(null);
    setError("");
    setLoading(true);
    try {
      const result = await apiRequest<Diagnosis>("/predictions/diagnose", {
        method: "POST",
        body: JSON.stringify({ factory, date: row.date, target: row.target, force_refresh: forceRefresh }),
        signal: aborter.signal,
      });
      if (!aborter.signal.aborted) setDiagnosis(result);
    } catch (requestError) {
      if (isAbortError(requestError)) return;
      setError(messageOf(requestError));
    } finally {
      if (controller.current === aborter) setLoading(false);
    }
  }

  return <>
    <article className="card table-card">
      <header className="card-title"><h3>최근 예측 이력</h3><span>P05~P95 정상범주{diagnosable ? " · 이탈 행은 AI 진단 가능" : ""}</span></header>
      <div className="table-wrap"><table>
        <thead><tr><th>일자</th><th>지표</th><th>P50</th><th>P05</th><th>P95</th><th>실측</th><th>판정</th>{diagnosable && <th>진단</th>}</tr></thead>
        <tbody>{rows.map((row, index) => {
          const status = row.status ?? "unknown";
          const anomalous = status === "over" || status === "under";
          return <tr key={`${row.date}-${row.target}-${index}`}>
            <td>{row.date}</td><td>{row.target}</td>
            <td>{number(row.predicted)}</td><td>{number(row.lower)}</td><td>{number(row.upper)}</td><td>{number(row.actual)}</td>
            <td><span className={`band-status ${status}`}>{statusLabels[status] ?? status}</span></td>
            {diagnosable && <td>{anomalous
              ? <button type="button" className="diagnose-button" onClick={() => void diagnose(row)} disabled={loading}><Stethoscope size={14}/>진단</button>
              : "-"}</td>}
          </tr>;
        })}</tbody>
      </table>{rows.length === 0 && <div className="empty-row">조회된 예측 이력이 없습니다.</div>}</div>
      {!diagnosable && <p className="panel-copy">이상 원인 진단은 개별 공장(남양주1·남양주2·김해·광주·논산)을 선택했을 때 사용할 수 있습니다.</p>}
    </article>
    {selected && <article className="card diagnosis-panel">
      <header className="panel-header">
        <div><span className="eyebrow">AI ANOMALY DIAGNOSIS</span><h3>{selected.date} · {factory} · {selected.target} 이상 원인 진단</h3></div>
        <div className="row-actions">
          {isAdmin && diagnosis && <button type="button" className="secondary-button" disabled={loading}
            onClick={() => { if (window.confirm("캐시를 무시하고 LLM에 다시 진단을 요청합니다 (비용 발생). 계속하시겠습니까?")) void diagnose(selected, true); }}>
            <Sparkles size={15}/>재생성</button>}
          <button type="button" className="secondary-button" onClick={() => { controller.current?.abort(); setSelected(null); setDiagnosis(null); setError(""); }} aria-label="진단 닫기"><X size={15}/></button>
        </div>
      </header>
      {loading && <div className="loading inline-loading"><RefreshCw className="spin"/>LLM이 컨텍스트를 분석하는 중입니다. (10~30초)</div>}
      {error && <div className="form-message error">{error}</div>}
      {diagnosis && <>
        <p className="diagnosis-meta">{diagnosis.from_cache ? "저장된 진단" : "신규 생성"} · 모델 {diagnosis.model_used ?? "-"} · 갱신 {diagnosis.updated_at ?? "-"}</p>
        <MarkdownReport content={diagnosis.diagnosis}/>
      </>}
    </article>}
  </>;
}
