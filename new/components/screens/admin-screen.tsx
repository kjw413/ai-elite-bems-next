"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Database, History, Pencil, Play, Plus, RefreshCw, Save, ShieldAlert, Target, Trash2, Upload } from "lucide-react";
import { apiRequest, isAbortError, query } from "@/lib/bems-api";
import { factories } from "@/lib/bems-data";

type AnyRow = Record<string, unknown>;
type AdminTab = "events" | "targets" | "data" | "predictions";

function messageOf(error: unknown) {
  return error instanceof Error ? error.message : "요청을 처리하지 못했습니다.";
}

function display(value: unknown) {
  if (value == null || value === "") return "-";
  if (typeof value === "number") return value.toLocaleString("ko-KR");
  return String(value);
}

const eventFactories = factories.filter(item => item !== "전사" && item !== "남양주");

function eventFactoryFor(factory: string) {
  return eventFactories.includes(factory) ? factory : "남양주1";
}

function EventsPanel({ factory, date, isAdmin }: { factory: string; date: string; isAdmin: boolean }) {
  const selectedEventFactory = eventFactoryFor(factory);
  const [events, setEvents] = useState<AnyRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState({ event_date: date, factory: selectedEventFactory, target: "overall", tag: "기타", severity: "info", note: "" });
  const loadController = useRef<AbortController | null>(null);

  useEffect(() => {
    if (editingId == null) {
      setForm(current => ({ ...current, event_date: date, factory: selectedEventFactory }));
    }
  }, [date, selectedEventFactory, editingId]);

  const load = useCallback(async () => {
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    setLoading(true);
    setError("");
    try {
      const dateFrom = `${date.slice(0, 7)}-01`;
      const result = await apiRequest<{ events: AnyRow[] }>(`/events?${query({ factory, date_from: dateFrom, date_to: date, limit: "100" })}`, { signal: controller.signal });
      if (!controller.signal.aborted) setEvents(result.events ?? []);
    } catch (requestError) {
      if (isAbortError(requestError)) return;
      setError(messageOf(requestError));
    } finally {
      if (loadController.current === controller) setLoading(false);
    }
  }, [factory, date]);

  useEffect(() => {
    void load();
    return () => loadController.current?.abort();
  }, [load]);

  function resetForm() {
    setEditingId(null);
    setForm({ event_date: date, factory: selectedEventFactory, target: "overall", tag: "기타", severity: "info", note: "" });
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!form.note.trim() || saving) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      if (editingId == null) {
        await apiRequest("/events", { method: "POST", body: JSON.stringify(form) });
        setNotice("현장 이벤트를 등록했습니다.");
      } else {
        await apiRequest(`/events/${editingId}`, {
          method: "PUT",
          body: JSON.stringify({ note: form.note, tag: form.tag, severity: form.severity }),
        });
        setNotice("현장 이벤트를 수정했습니다.");
      }
      resetForm();
      await load();
    } catch (requestError) {
      setError(messageOf(requestError));
    } finally {
      setSaving(false);
    }
  }

  function edit(row: AnyRow) {
    setEditingId(Number(row.id));
    setForm({
      event_date: String(row.event_date ?? date).slice(0, 10),
      factory: String(row.factory ?? factory),
      target: String(row.target ?? "overall"),
      tag: String(row.tag ?? "기타"),
      severity: String(row.severity ?? "info"),
      note: String(row.note ?? ""),
    });
  }

  async function remove(id: number) {
    if (!window.confirm("이 이벤트를 삭제하시겠습니까?")) return;
    setError("");
    try {
      await apiRequest(`/events/${id}`, { method: "DELETE" });
      setNotice("현장 이벤트를 삭제했습니다.");
      await load();
    } catch (requestError) {
      setError(messageOf(requestError));
    }
  }

  return <div className="admin-grid">
    {isAdmin && <form className="card admin-form" onSubmit={submit}>
      <header><div><span className="eyebrow">EVENT MEMO</span><h3>{editingId == null ? "현장 이벤트 등록" : "현장 이벤트 수정"}</h3></div>{editingId != null && <button type="button" className="text-button" onClick={resetForm}>취소</button>}</header>
      <div className="form-grid">
        <label className="field"><span>일자</span><input type="date" disabled={editingId != null} value={form.event_date} onChange={event => setForm({ ...form, event_date: event.target.value })}/></label>
        <label className="field"><span>공장</span><select disabled={editingId != null} value={form.factory} onChange={event => setForm({ ...form, factory: event.target.value })}>{eventFactories.map(item => <option key={item}>{item}</option>)}</select></label>
        <label className="field"><span>대상</span><select disabled={editingId != null} value={form.target} onChange={event => setForm({ ...form, target: event.target.value })}><option value="overall">전체</option><option value="power">전력</option><option value="fuel">연료</option><option value="water">용수</option><option value="wastewater">폐수</option><option value="production">생산</option></select></label>
        <label className="field"><span>태그</span><select value={form.tag} onChange={event => setForm({ ...form, tag: event.target.value })}><option value="센서고장">센서고장</option><option value="설비정비">설비정비</option><option value="생산변경">생산변경</option><option value="외부요인">외부요인</option><option value="기타">기타</option></select></label>
        <label className="field"><span>중요도</span><select value={form.severity} onChange={event => setForm({ ...form, severity: event.target.value })}><option value="info">정보</option><option value="warn">주의</option><option value="critical">긴급</option></select></label>
        <label className="field full"><span>내용</span><textarea rows={4} required value={form.note} onChange={event => setForm({ ...form, note: event.target.value })} placeholder="원인, 조치 내용 또는 현장 상황을 입력하세요."/></label>
      </div>
      <button className="primary-button" type="submit" disabled={saving}><Save size={16}/>{saving ? "저장 중..." : editingId == null ? "등록" : "저장"}</button>
    </form>}
    <article className="card admin-list">
      <header className="panel-header"><div><span className="eyebrow">RECENT EVENTS</span><h3>이벤트 메모</h3></div><button type="button" className="secondary-button" onClick={() => void load()} disabled={loading}><RefreshCw size={15}/>새로고침</button></header>
      {error && <div className="form-message error">{error}</div>}{notice && <div className="form-message success">{notice}</div>}
      {loading ? <div className="loading inline-loading"><RefreshCw className="spin"/>불러오는 중입니다.</div> : <div className="table-wrap"><table><thead><tr><th>일자</th><th>공장</th><th>태그</th><th>중요도</th><th>내용</th>{isAdmin && <th>관리</th>}</tr></thead><tbody>{events.map(row => <tr key={String(row.id)}><td>{display(row.event_date).slice(0, 10)}</td><td>{display(row.factory)}</td><td>{display(row.tag)}</td><td><span className={`severity ${display(row.severity)}`}>{display(row.severity)}</span></td><td>{display(row.note)}</td>{isAdmin && <td><div className="row-actions"><button type="button" aria-label="수정" onClick={() => edit(row)}><Pencil size={15}/></button><button type="button" aria-label="삭제" onClick={() => void remove(Number(row.id))}><Trash2 size={15}/></button></div></td>}</tr>)}</tbody></table>{events.length === 0 && <div className="empty-row">조회된 이벤트가 없습니다.</div>}</div>}
    </article>
  </div>;
}

const targetMetrics = [
  { value: "power_per_ton", label: "전력 원단위" },
  { value: "fuel_per_ton", label: "연료 원단위" },
  { value: "water_per_ton", label: "용수 원단위" },
  { value: "mix_prod", label: "생산량" },
];

function TargetsPanel({ factory, date, isAdmin }: { factory: string; date: string; isAdmin: boolean }) {
  const year = Number(date.slice(0, 4));
  const targetFactory = factory === "전사" ? "ALL" : factory === "남양주1" || factory === "남양주2" ? "남양주" : factory;
  const targetScopeLabel = targetFactory === "ALL" ? "전사" : targetFactory === "남양주" ? "남양주 (1·2 공통)" : targetFactory;
  const [rows, setRows] = useState<AnyRow[]>([]);
  const [metric, setMetric] = useState("power_per_ton");
  const [targetPct, setTargetPct] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const loadController = useRef<AbortController | null>(null);
  const saveController = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    setLoading(true);
    setError("");
    try {
      const result = await apiRequest<{ targets: AnyRow[] }>(`/targets?${query({ year: String(year) })}`, { signal: controller.signal });
      if (!controller.signal.aborted) setRows(result.targets ?? []);
    } catch (requestError) {
      if (isAbortError(requestError)) return;
      setError(messageOf(requestError));
    } finally {
      if (loadController.current === controller) setLoading(false);
    }
  }, [year]);

  useEffect(() => {
    void load();
    return () => {
      loadController.current?.abort();
      saveController.current?.abort();
    };
  }, [load]);
  useEffect(() => {
    const existing = rows.find(row => String(row.factory) === targetFactory && String(row.metric) === metric);
    setTargetPct(existing?.target_pct == null ? "" : String(existing.target_pct));
    setNote(existing?.note == null ? "" : String(existing.note));
  }, [rows, targetFactory, metric]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (saving) return;
    saveController.current?.abort();
    const controller = new AbortController();
    saveController.current = controller;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      await apiRequest("/targets", {
        method: "PUT",
        body: JSON.stringify({
          year,
          items: [{ factory: targetFactory, metric, target_pct: targetPct === "" ? null : Number(targetPct) }],
          note,
        }),
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      setNotice("절감 목표를 저장했습니다.");
      await load();
    } catch (requestError) {
      if (isAbortError(requestError)) return;
      setError(messageOf(requestError));
    } finally {
      if (saveController.current === controller) setSaving(false);
    }
  }

  return <div className="admin-grid">
    <form className="card admin-form" onSubmit={save}>
      <header><div><span className="eyebrow">SAVINGS TARGET</span><h3>{year}년 절감 목표</h3></div><Target size={22}/></header>
      <div className="form-grid">
        <label className="field"><span>적용 범위</span><input value={targetScopeLabel} disabled/></label>
        <label className="field"><span>지표</span><select value={metric} onChange={event => setMetric(event.target.value)}>{targetMetrics.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
        <label className="field"><span>절감률(%)</span><input type="number" step="0.1" min="-100" max="100" value={targetPct} disabled={!isAdmin} onChange={event => setTargetPct(event.target.value)}/></label>
        <label className="field full"><span>메모</span><textarea rows={3} value={note} disabled={!isAdmin} onChange={event => setNote(event.target.value)}/></label>
      </div>
      {isAdmin ? <button className="primary-button" type="submit" disabled={saving}><Save size={16}/>{saving ? "저장 중..." : "목표 저장"}</button> : <div className="permission-note">조회 사용자는 목표를 확인만 할 수 있습니다.</div>}
      {error && <div className="form-message error">{error}</div>}{notice && <div className="form-message success">{notice}</div>}
    </form>
    <article className="card admin-list">
      <header className="panel-header"><div><span className="eyebrow">TARGET MATRIX</span><h3>등록된 목표</h3></div><button type="button" className="secondary-button" onClick={() => void load()}><RefreshCw size={15}/>새로고침</button></header>
      {loading ? <div className="loading inline-loading"><RefreshCw className="spin"/>불러오는 중입니다.</div> : <div className="table-wrap"><table><thead><tr><th>공장</th><th>지표</th><th>절감률</th><th>메모</th><th>갱신</th></tr></thead><tbody>{rows.map((row, index) => <tr key={`${row.factory}-${row.metric}-${index}`}><td>{row.factory === "ALL" ? "전사" : display(row.factory)}</td><td>{targetMetrics.find(item => item.value === row.metric)?.label ?? display(row.metric)}</td><td>{row.target_pct == null ? "-" : `${display(row.target_pct)}%`}</td><td>{display(row.note)}</td><td>{display(row.updated_at)}</td></tr>)}</tbody></table></div>}
    </article>
  </div>;
}

function DataPanel() {
  const [audit, setAudit] = useState<{ changes: AnyRow[]; uploads: AnyRow[] }>({ changes: [], uploads: [] });
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const fileInput = useRef<HTMLInputElement | null>(null);
  const loadController = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    setLoading(true);
    setError("");
    try {
      const result = await apiRequest<{ changes: AnyRow[]; uploads: AnyRow[] }>("/audit", { signal: controller.signal });
      if (!controller.signal.aborted) setAudit(result);
    } catch (requestError) {
      if (isAbortError(requestError)) return;
      setError(messageOf(requestError));
    } finally {
      if (loadController.current === controller) setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
    return () => loadController.current?.abort();
  }, [load]);

  async function uploadFile(event: React.FormEvent) {
    event.preventDefault();
    if (!file) return;
    if (!/\.(xlsx|xls)$/i.test(file.name)) {
      setError("xlsx 또는 xls 파일만 업로드할 수 있습니다.");
      return;
    }
    if (file.size > 50 * 1024 * 1024) {
      setError("파일 크기는 50MB 이하여야 합니다.");
      return;
    }
    if (!window.confirm("검증을 통과한 데이터는 즉시 MySQL에 UPSERT됩니다. 계속하시겠습니까?")) return;
    const body = new FormData();
    body.append("file", file);
    setUploading(true);
    setError("");
    setNotice("");
    try {
      const result = await apiRequest<{ rows: number; message: string }>("/upload", { method: "POST", body });
      setNotice(`${result.rows.toLocaleString("ko-KR")}행을 반영했습니다. ${result.message ?? ""}`);
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      await load();
    } catch (requestError) {
      setError(messageOf(requestError));
    } finally {
      setUploading(false);
    }
  }

  return <div className="screen-stack">
    <form className="card upload-panel" onSubmit={uploadFile}>
      <div><span className="eyebrow">EXCEL UPSERT</span><h3>에너지 실적 업로드</h3><p>검증 후 MySQL에 UPSERT하며 원본과 변경 이력을 남깁니다. 최대 50MB의 xlsx·xls만 허용됩니다.</p></div>
      <label className="file-picker"><Upload size={22}/><span>{file?.name ?? "Excel 파일 선택"}</span><input ref={fileInput} type="file" accept=".xlsx,.xls" onChange={event => setFile(event.target.files?.[0] ?? null)}/></label>
      <button type="submit" className="primary-button" disabled={!file || uploading}><Upload size={16}/>{uploading ? "업로드 중..." : "검증 및 업로드"}</button>
    </form>
    {error && <div className="form-message error">{error}</div>}{notice && <div className="form-message success">{notice}</div>}
    <div className="admin-grid equal">
      <article className="card admin-list"><header className="panel-header"><div><span className="eyebrow">UPLOAD HISTORY</span><h3>최근 업로드</h3></div><button type="button" className="secondary-button" onClick={() => void load()}><RefreshCw size={15}/></button></header>{loading ? <div className="loading inline-loading"><RefreshCw className="spin"/></div> : <div className="table-wrap"><table><thead><tr><th>파일</th><th>일시</th><th>행</th><th>상태</th></tr></thead><tbody>{audit.uploads.map((row, index) => <tr key={String(row.id ?? index)}><td>{display(row.filename)}</td><td>{display(row.uploadedAt)}</td><td>{display(row.rows)}</td><td>{display(row.status)}</td></tr>)}</tbody></table></div>}</article>
      <article className="card admin-list"><header className="panel-header"><div><span className="eyebrow">AUDIT LOG</span><h3>최근 데이터 변경</h3></div><History size={20}/></header>{loading ? <div className="loading inline-loading"><RefreshCw className="spin"/></div> : <div className="table-wrap"><table><thead><tr><th>일시</th><th>공장</th><th>필드</th><th>이전</th><th>변경</th></tr></thead><tbody>{audit.changes.map((row, index) => <tr key={String(row.id ?? index)}><td>{display(row.time)}</td><td>{display(row.factory)}</td><td>{display(row.field)}</td><td>{display(row.before)}</td><td>{display(row.after)}</td></tr>)}</tbody></table></div>}</article>
    </div>
  </div>;
}

function PredictionOpsPanel({ factory, date }: { factory: string; date: string }) {
  const [dateFrom, setDateFrom] = useState(`${date.slice(0, 7)}-01`);
  const [dateTo, setDateTo] = useState(date);
  const [running, setRunning] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState<unknown>(null);
  const runController = useRef<AbortController | null>(null);
  useEffect(() => {
    runController.current?.abort();
    setDateFrom(`${date.slice(0, 7)}-01`);
    setDateTo(date);
    setRunning("");
    setError("");
    setResult(null);
    return () => runController.current?.abort();
  }, [factory, date]);

  async function run(kind: "missing" | "actuals") {
    if (kind === "missing") {
      const start = Date.parse(`${dateFrom}T00:00:00`);
      const end = Date.parse(`${dateTo}T00:00:00`);
      if (!Number.isFinite(start) || !Number.isFinite(end) || start > end) {
        setError("올바른 시작일과 종료일을 입력하세요.");
        return;
      }
      const days = Math.floor((end - start) / 86_400_000) + 1;
      if (days > 93) {
        setError("한 번에 최대 93일까지 생성할 수 있습니다. 기간을 나누어 실행하세요.");
        return;
      }
      if (!window.confirm(`${factory}의 ${days}일 범위를 계산하고 누락 예측을 DB에 저장합니다. 계속하시겠습니까?`)) return;
    }
    if (kind === "actuals" && !window.confirm("전체 prediction_log의 누락 실측값을 역채움합니다. 계속하시겠습니까?")) return;
    runController.current?.abort();
    const controller = new AbortController();
    runController.current = controller;
    setRunning(kind);
    setError("");
    setResult(null);
    try {
      const operationResult = kind === "missing"
        ? await apiRequest("/predictions/generate-missing", { method: "POST", body: JSON.stringify({ factory, date_from: dateFrom, date_to: dateTo }), signal: controller.signal })
        : await apiRequest("/predictions/backfill-actuals", { method: "POST", signal: controller.signal });
      if (!controller.signal.aborted) setResult(operationResult);
    } catch (requestError) {
      if (isAbortError(requestError)) return;
      setError(messageOf(requestError));
    } finally {
      if (runController.current === controller) setRunning("");
    }
  }

  return <div className="admin-grid">
    <article className="card admin-form">
      <header><div><span className="eyebrow">PREDICTION HISTORY</span><h3>예측 누락이력 생성</h3></div><Database size={22}/></header>
      <div className="form-grid"><label className="field"><span>시작일</span><input required type="date" value={dateFrom} onChange={event => setDateFrom(event.target.value)}/></label><label className="field"><span>종료일</span><input required type="date" value={dateTo} onChange={event => setDateTo(event.target.value)}/></label></div>
      <button type="button" className="primary-button" disabled={Boolean(running)} onClick={() => void run("missing")}><Play size={16}/>{running === "missing" ? "실행 중..." : "누락이력 생성"}</button>
    </article>
    <article className="card admin-form">
      <header><div><span className="eyebrow">ACTUAL BACKFILL</span><h3>실측값 역채움</h3></div><History size={22}/></header>
      <p className="panel-copy">prediction_log의 누락된 실측값을 최신 energy_daily 데이터로 보완합니다.</p>
      <button type="button" className="primary-button" disabled={Boolean(running)} onClick={() => void run("actuals")}><Play size={16}/>{running === "actuals" ? "실행 중..." : "실측값 역채움"}</button>
    </article>
    {error && <div className="form-message error admin-span">{error}</div>}
    {result != null && <article className="card operation-result admin-span"><strong>작업 결과</strong><pre>{JSON.stringify(result, null, 2)}</pre></article>}
  </div>;
}

export function AdminScreen({ factory, date, isAdmin }: { factory: string; date: string; isAdmin: boolean }) {
  const allowedTabs = useMemo<AdminTab[]>(() => isAdmin ? ["events", "targets", "data", "predictions"] : ["events", "targets"], [isAdmin]);
  const [tab, setTab] = useState<AdminTab>("events");
  useEffect(() => { if (!allowedTabs.includes(tab)) setTab("events"); }, [allowedTabs, tab]);
  const labels: Record<AdminTab, string> = { events: "이벤트 메모", targets: "절감 목표", data: "업로드·감사", predictions: "예측 이력 관리" };

  return <section className="screen-stack">
    {!isAdmin && <div className="permission-banner"><ShieldAlert size={21}/><div><strong>조회 사용자 모드</strong><p>이벤트와 절감 목표는 열람만 가능하며 모든 변경 작업은 서버에서 차단됩니다.</p></div></div>}
    <div className="admin-tabs" role="tablist">{allowedTabs.map(item => <button type="button" role="tab" aria-selected={tab === item} className={tab === item ? "active" : ""} key={item} onClick={() => setTab(item)}>{labels[item]}</button>)}</div>
    {tab === "events" && <EventsPanel factory={factory} date={date} isAdmin={isAdmin}/>}
    {tab === "targets" && <TargetsPanel factory={factory} date={date} isAdmin={isAdmin}/>}
    {tab === "data" && isAdmin && <DataPanel/>}
    {tab === "predictions" && isAdmin && <PredictionOpsPanel factory={factory} date={date}/>}
  </section>;
}
