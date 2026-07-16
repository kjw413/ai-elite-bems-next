"use client";

import { Fragment, type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FileText, Printer, RefreshCw, Sparkles } from "lucide-react";
import { apiRequest, isAbortError, query } from "@/lib/bems-api";

type ReportData = {
  content: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

type AvailableReports = {
  months: { year: number; month: number }[];
};

function messageOf(error: unknown) {
  return error instanceof Error ? error.message : "요청을 처리하지 못했습니다.";
}

function inlineMarkdown(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean).map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={index}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("`") && part.endsWith("`")) {
      return <code key={index}>{part.slice(1, -1)}</code>;
    }
    return <Fragment key={index}>{part}</Fragment>;
  });
}

function tableCells(line: string) {
  return line.trim().replace(/^\||\|$/g, "").split("|").map(cell => cell.trim());
}

function isDivider(row: string[]) {
  return row.every(cell => /^:?-{3,}:?$/.test(cell));
}

function MarkdownReport({ content }: { content: string }) {
  const blocks: ReactNode[] = [];
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  let index = 0;

  while (index < lines.length) {
    const line = lines[index].trim();
    if (!line) {
      index += 1;
      continue;
    }
    if (line.startsWith("|")) {
      const rows: string[][] = [];
      while (index < lines.length && lines[index].trim().startsWith("|")) {
        rows.push(tableCells(lines[index]));
        index += 1;
      }
      const header = rows[0] ?? [];
      const body = rows.slice(1).filter(row => !isDivider(row));
      blocks.push(
        <div className="markdown-table-wrap" key={`table-${index}`}>
          <table>
            <thead><tr>{header.map((cell, cellIndex) => <th key={cellIndex}>{inlineMarkdown(cell)}</th>)}</tr></thead>
            <tbody>{body.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{inlineMarkdown(cell)}</td>)}</tr>)}</tbody>
          </table>
        </div>,
      );
      continue;
    }
    if (/^[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^[-*]\s+/.test(lines[index].trim())) {
        items.push(lines[index].trim().replace(/^[-*]\s+/, ""));
        index += 1;
      }
      blocks.push(<ul key={`ul-${index}`}>{items.map((item, itemIndex) => <li key={itemIndex}>{inlineMarkdown(item)}</li>)}</ul>);
      continue;
    }
    if (/^\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^\d+\.\s+/.test(lines[index].trim())) {
        items.push(lines[index].trim().replace(/^\d+\.\s+/, ""));
        index += 1;
      }
      blocks.push(<ol key={`ol-${index}`}>{items.map((item, itemIndex) => <li key={itemIndex}>{inlineMarkdown(item)}</li>)}</ol>);
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      const text = inlineMarkdown(heading[2]);
      blocks.push(level === 1 ? <h1 key={index}>{text}</h1> : level === 2 ? <h2 key={index}>{text}</h2> : level === 3 ? <h3 key={index}>{text}</h3> : <h4 key={index}>{text}</h4>);
      index += 1;
      continue;
    }
    if (/^(-{3,}|\*{3,})$/.test(line)) {
      blocks.push(<hr key={index}/>);
      index += 1;
      continue;
    }
    if (line.startsWith(">")) {
      blocks.push(<blockquote key={index}>{inlineMarkdown(line.replace(/^>\s?/, ""))}</blockquote>);
      index += 1;
      continue;
    }
    blocks.push(<p key={index}>{inlineMarkdown(line)}</p>);
    index += 1;
  }

  return <article className="markdown-report">{blocks}</article>;
}

export function ReportScreen({ factory, date, isAdmin }: { factory: string; date: string; isAdmin: boolean }) {
  const initialPeriod = date.slice(0, 7);
  const [period, setPeriod] = useState(initialPeriod);
  const [report, setReport] = useState<ReportData>({ content: null });
  const [available, setAvailable] = useState<AvailableReports["months"]>([]);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const loadController = useRef<AbortController | null>(null);
  const generateController = useRef<AbortController | null>(null);
  const [year, month] = useMemo(() => period.split("-").map(Number), [period]);

  useEffect(() => setPeriod(date.slice(0, 7)), [date]);

  const load = useCallback(async () => {
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    setLoading(true);
    setError("");
    setAvailable([]);
    try {
      const [reportResult, availableResult] = await Promise.allSettled([
        apiRequest<ReportData>(`/reports?${query({ factory, year: String(year), month: String(month) })}`, { signal: controller.signal }),
        apiRequest<AvailableReports>(`/reports/available?${query({ factory })}`, { signal: controller.signal }),
      ]);
      if (controller.signal.aborted) return;
      if (reportResult.status === "fulfilled") {
        setReport(reportResult.value);
      } else {
        setReport({ content: null });
        setError(messageOf(reportResult.reason));
      }
      if (availableResult.status === "fulfilled") {
        setAvailable(availableResult.value.months ?? []);
      } else if (reportResult.status === "fulfilled") {
        setError(`보고서는 불러왔지만 보유 월 목록을 확인하지 못했습니다. ${messageOf(availableResult.reason)}`);
      }
    } catch (requestError) {
      if (isAbortError(requestError)) return;
      setReport({ content: null });
      setError(messageOf(requestError));
    } finally {
      if (loadController.current === controller) setLoading(false);
    }
  }, [factory, year, month]);

  useEffect(() => {
    void load();
    return () => {
      loadController.current?.abort();
      generateController.current?.abort();
    };
  }, [load]);

  async function generate() {
    generateController.current?.abort();
    const controller = new AbortController();
    generateController.current = controller;
    setGenerating(true);
    setError("");
    setNotice("");
    try {
      const result = await apiRequest<ReportData>("/reports/generate", {
        method: "POST",
        body: JSON.stringify({ factory, year, month }),
        signal: controller.signal,
      });
      if (result.content?.includes("AI Agent 분석 중 오류")) {
        throw new Error(result.content);
      }
      if (controller.signal.aborted) return;
      setReport(result);
      setNotice("AI 보고서를 생성하고 저장했습니다.");
      try {
        const months = await apiRequest<AvailableReports>(`/reports/available?${query({ factory })}`, { signal: controller.signal });
        if (!controller.signal.aborted) setAvailable(months.months ?? []);
      } catch (monthsError) {
        if (!isAbortError(monthsError)) {
          setError(`보고서는 저장했지만 보유 월 목록을 갱신하지 못했습니다. ${messageOf(monthsError)}`);
        }
      }
    } catch (requestError) {
      if (isAbortError(requestError)) return;
      setError(messageOf(requestError));
    } finally {
      if (generateController.current === controller) setGenerating(false);
    }
  }

  const periods = Array.from(new Set([
    period,
    ...available.map(item => `${item.year}-${String(item.month).padStart(2, "0")}`),
  ]));

  return <section className="screen-stack">
    <article className="card report-toolbar">
      <div>
        <span className="eyebrow">AI MONTHLY REPORT</span>
        <h2>{factory} 에너지 실적 보고서</h2>
        <p>저장된 보고서를 조회하거나 관리자 권한으로 새 보고서를 생성합니다.</p>
      </div>
      <div className="action-row">
        <label className="field compact-field"><span>기준 월</span><select value={period} onChange={event => setPeriod(event.target.value)}>{periods.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
        <button type="button" className="secondary-button" onClick={() => void load()} disabled={loading || generating}><RefreshCw size={16}/>새로고침</button>
        <button type="button" className="secondary-button" onClick={() => window.print()} disabled={!report.content}><Printer size={16}/>인쇄·PDF</button>
        {isAdmin && <button type="button" className="primary-button" onClick={() => void generate()} disabled={generating}><Sparkles size={16}/>{generating ? "생성 중..." : report.content ? "재생성" : "보고서 생성"}</button>}
      </div>
    </article>
    {error && <div className="form-message error" role="alert">{error}</div>}
    {notice && <div className="form-message success" role="status">{notice}</div>}
    {!isAdmin && <div className="permission-note">조회 사용자는 저장된 보고서만 열람할 수 있습니다.</div>}
    <article className="card report-paper">
      {loading ? <div className="loading inline-loading"><RefreshCw className="spin"/>보고서를 불러오는 중입니다.</div>
        : report.content ? <>
          <header className="report-meta"><div><FileText size={20}/><strong>{year}년 {month}월 보고서</strong></div><span>최종 갱신 {report.updated_at ?? report.created_at ?? "-"}</span></header>
          <MarkdownReport content={report.content}/>
        </> : <div className="empty-state"><FileText size={38}/><strong>저장된 보고서가 없습니다.</strong><p>{isAdmin ? "보고서 생성 버튼으로 새 보고서를 작성할 수 있습니다." : "관리자가 보고서를 생성한 뒤 열람할 수 있습니다."}</p></div>}
    </article>
  </section>;
}
