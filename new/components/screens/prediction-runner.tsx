"use client";

import { useEffect, useRef, useState } from "react";
import { BrainCircuit, Play } from "lucide-react";
import { apiRequest, isAbortError } from "@/lib/bems-api";

type PredictionResult = {
  date: string;
  target: string;
  predicted?: number | null;
  lower?: number | null;
  upper?: number | null;
  actual?: number | null;
  status?: string;
  error?: string;
};

function number(value: number | null | undefined) {
  return value == null ? "-" : value.toLocaleString("ko-KR", { maximumFractionDigits: 2 });
}

function messageOf(error: unknown) {
  return error instanceof Error ? error.message : "예측 요청을 처리하지 못했습니다.";
}

export function PredictionRunner({ factory, date, isAdmin }: { factory: string; date: string; isAdmin: boolean }) {
  const aggregate = factory === "전사" || factory === "남양주";
  const [productionKg, setProductionKg] = useState("");
  const [results, setResults] = useState<PredictionResult[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const runController = useRef<AbortController | null>(null);
  useEffect(() => {
    runController.current?.abort();
    setResults([]);
    setError("");
    setRunning(false);
    return () => runController.current?.abort();
  }, [factory, date]);

  if (!isAdmin) {
    return <div className="permission-note">조회 사용자는 저장된 예측 이력만 열람할 수 있습니다.</div>;
  }

  async function run(event: React.FormEvent) {
    event.preventDefault();
    const mixProdKg = productionKg === "" ? 0 : Number(productionKg);
    if (!aggregate && (!Number.isFinite(mixProdKg) || mixProdKg <= 0)) {
      setError("개별 공장 예측에는 0보다 큰 생산계획(kg)이 필요합니다.");
      return;
    }
    runController.current?.abort();
    const controller = new AbortController();
    runController.current = controller;
    setRunning(true);
    setError("");
    setResults([]);
    try {
      const response = await apiRequest<{ results: PredictionResult[] }>("/predictions/run", {
        method: "POST",
        body: JSON.stringify({ factory, date, mix_prod_kg: mixProdKg }),
        signal: controller.signal,
      });
      if (!controller.signal.aborted) setResults(response.results ?? []);
    } catch (requestError) {
      if (isAbortError(requestError)) return;
      setError(messageOf(requestError));
    } finally {
      if (runController.current === controller) setRunning(false);
    }
  }

  return <article className="card prediction-runner">
    <form onSubmit={run}>
      <div><span className="eyebrow">ON-DEMAND PREDICTION</span><h3>생산계획 기반 예측 미리보기</h3><p>결과를 DB에 저장하지 않는 단일 실행입니다. 전력은 MWh, 연료는 천 Nm³, 용수는 천 ton 단위입니다.</p></div>
      <label className="field"><span>생산계획(kg){aggregate ? " · 집계 공장은 자동 계산" : ""}</span><input type="number" min="0" step="1" disabled={aggregate} value={aggregate ? "" : productionKg} placeholder={aggregate ? "자동" : "예: 125000"} onChange={event => setProductionKg(event.target.value)}/></label>
      <button type="submit" className="primary-button" disabled={running}><Play size={16}/>{running ? "예측 중..." : "예측 실행"}</button>
    </form>
    {error && <div className="form-message error">{error}</div>}
    {results.length > 0 && <div className="table-wrap prediction-preview"><table><thead><tr><th>일자</th><th>지표</th><th>P50</th><th>P05</th><th>P95</th><th>실측</th><th>판정</th></tr></thead><tbody>{results.map((row, index) => <tr key={`${row.target}-${index}`}><td>{row.date}</td><td>{row.target}</td>{row.error ? <td colSpan={5} className="bad">{row.error}</td> : <><td>{number(row.predicted)}</td><td>{number(row.lower)}</td><td>{number(row.upper)}</td><td>{number(row.actual)}</td><td>{row.status ?? "unknown"}</td></>}</tr>)}</tbody></table></div>}
  </article>;
}
