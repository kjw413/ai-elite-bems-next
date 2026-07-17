"use client";

import { useEffect, useRef, useState } from "react";
import { BrainCircuit, RefreshCw } from "lucide-react";
import { apiRequest, isAbortError, query } from "@/lib/bems-api";

type ImportanceItem = { feature: string; label: string; importance: number; rank: number };
type ImportanceResult = { items: ImportanceItem[]; summary: string };

const targets = ["전력", "연료", "용수"] as const;

// legacy 예측 화면 '모델 변수 영향도 — 어떤 변수가 이 예측을 좌우하는가?'의 이식.
// 활성 v5 모델의 가중 feature importance Top 5를 한국어 라벨로 보여준다.
export function FeatureImportance({ factory }: { factory: string }) {
  const [target, setTarget] = useState<(typeof targets)[number]>("전력");
  const [result, setResult] = useState<ImportanceResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setLoading(true);
    setError("");
    setResult(null);
    apiRequest<ImportanceResult>(`/model/feature-importance?${query({ factory, target })}`, { signal: abort.signal })
      .then(response => { if (!abort.signal.aborted) setResult(response); })
      .catch(requestError => {
        if (isAbortError(requestError)) return;
        setError(requestError instanceof Error ? requestError.message : "변수 영향도를 불러오지 못했습니다.");
      })
      .finally(() => { if (controller.current === abort) setLoading(false); });
    return () => abort.abort();
  }, [factory, target]);

  const items = result?.items ?? [];
  const maxImportance = items.length ? Math.max(...items.map(item => item.importance)) : 1;
  return <article className="card list feature-importance">
    <header className="card-title">
      <h3>모델 변수 영향도 Top 5</h3>
      <div className="card-title-side"><BrainCircuit size={16}/><span>{factory} · 활성 v5 모델</span></div>
    </header>
    <div className="segmented" role="group" aria-label="영향도 대상 지표">
      {targets.map(item => <button type="button" key={item} className={target === item ? "active" : ""} aria-pressed={target === item} onClick={() => setTarget(item)}>{item}</button>)}
    </div>
    {loading && <div className="loading inline-loading"><RefreshCw className="spin"/>영향도를 계산 중입니다.</div>}
    {!loading && error && <div className="form-message error">{error}</div>}
    {!loading && !error && items.length === 0 && <p className="panel-copy">이 공장·지표의 활성 모델 영향도 데이터가 없습니다.</p>}
    {!loading && !error && items.map(item => <div className="progress" key={item.feature}>
      <div><span>{item.rank}. {item.label}</span><b>{(item.importance * 100).toFixed(1)}%</b></div>
      <i><em style={{ width: `${Math.max(4, (item.importance / maxImportance) * 100)}%` }}/></i>
    </div>)}
    {!loading && !error && result?.summary && <p className="fi-summary">{result.summary}</p>}
  </article>;
}
