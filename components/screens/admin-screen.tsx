"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BrainCircuit, CloudSun, Database, FolderSync, History, Mail, Pencil, Play, RefreshCw, Save, ShieldAlert, Target, Trash2, Upload } from "lucide-react";
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
        <label className="field"><span>{metric === "mix_prod" ? "증가율(%) · 전년 대비 생산 증가 목표" : "절감률(%) · 전년 대비 원단위 절감 목표"}</span><input type="number" step="0.1" min="-100" max="100" value={targetPct} disabled={!isAdmin} onChange={event => setTargetPct(event.target.value)}/></label>
        <label className="field full"><span>메모</span><textarea rows={3} value={note} disabled={!isAdmin} onChange={event => setNote(event.target.value)}/></label>
      </div>
      {isAdmin ? <button className="primary-button" type="submit" disabled={saving}><Save size={16}/>{saving ? "저장 중..." : "목표 저장"}</button> : <div className="permission-note">조회 사용자는 목표를 확인만 할 수 있습니다.</div>}
      {error && <div className="form-message error">{error}</div>}{notice && <div className="form-message success">{notice}</div>}
    </form>
    <article className="card admin-list">
      <header className="panel-header"><div><span className="eyebrow">TARGET MATRIX</span><h3>등록된 목표</h3></div><button type="button" className="secondary-button" onClick={() => void load()}><RefreshCw size={15}/>새로고침</button></header>
      {loading ? <div className="loading inline-loading"><RefreshCw className="spin"/>불러오는 중입니다.</div> : <div className="table-wrap"><table><thead><tr><th>공장</th><th>지표</th><th>목표율(%)</th><th>메모</th><th>갱신</th></tr></thead><tbody>{rows.map((row, index) => <tr key={`${row.factory}-${row.metric}-${index}`}><td>{row.factory === "ALL" ? "전사" : display(row.factory)}</td><td>{targetMetrics.find(item => item.value === row.metric)?.label ?? display(row.metric)}</td><td>{row.target_pct == null ? "-" : `${display(row.target_pct)}% ${row.metric === "mix_prod" ? "증가" : "절감"}`}</td><td>{display(row.note)}</td><td>{display(row.updated_at)}</td></tr>)}</tbody></table></div>}
    </article>
  </div>;
}

const mailPeriods = [
  { id: "daily", label: "일간" },
  { id: "weekly", label: "주간" },
  { id: "monthly", label: "월간" },
] as const;
type MailPeriod = (typeof mailPeriods)[number]["id"];

// legacy 대시보드 '📧 메일 송부'의 이식 — 관리자 전용 탭에만 배치되어 viewer에게는
// 노출되지 않으며, 서버(/mail/send)에서도 관리자 IP를 재검사한다.
function MailCard({ date }: { date: string }) {
  const [period, setPeriod] = useState<MailPeriod>("daily");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const help: Record<MailPeriod, string> = {
    daily: `기준일 ${date} 원단위 상세 · 즉시 점검 대상`,
    weekly: "직전 완결 주 (월~일, 전주비)",
    monthly: "직전 완결 월 (전년 동월비·YTD)",
  };
  async function send() {
    if (sending) return;
    const label = mailPeriods.find(item => item.id === period)?.label ?? period;
    if (!window.confirm(`${label} 에너지 리포트를 .env에 설정된 수신자에게 즉시 발송합니다. 계속하시겠습니까?`)) return;
    setSending(true); setError(""); setNotice("");
    try {
      const result = await apiRequest<{ label: string; refDate: string; recordCount: number; to: string[] }>("/mail/send", {
        method: "POST",
        body: JSON.stringify({ period, ...(period === "daily" ? { date } : {}) }),
      });
      setNotice(`${result.label} 메일 발송 완료 · 기준 ${result.refDate} · 공장 ${result.recordCount}개 · 수신 ${result.to.join(", ")}`);
    } catch (requestError) {
      setError(messageOf(requestError));
    } finally {
      setSending(false);
    }
  }
  return <article className="card admin-form">
    <header><div><span className="eyebrow">MAIL REPORT</span><h3>에너지 리포트 메일 발송</h3></div><Mail size={22}/></header>
    <p className="panel-copy">tools/mail 파이프라인으로 HTML 리포트를 생성해 .env의 MAIL_RECIPIENTS에게 즉시 발송합니다. {help[period]}</p>
    <div className="segmented" role="group" aria-label="메일 발송 주기">{mailPeriods.map(item => <button type="button" key={item.id} className={period === item.id ? "active" : ""} aria-pressed={period === item.id} onClick={() => setPeriod(item.id)}>{item.label}</button>)}</div>
    {error && <div className="form-message error">{error}</div>}
    {notice && <div className="form-message success">{notice}</div>}
    <button type="button" className="primary-button" disabled={sending} onClick={() => void send()}><Mail size={16}/>{sending ? "발송 중..." : "발송"}</button>
  </article>;
}

type UploadPreview = {
  success: boolean;
  message: string;
  errors: AnyRow[];
  summary: AnyRow[];
  total_new: number;
  total_overwrite: number;
};

type SyncStatus = {
  scheduler: { enabled: boolean; intervalSeconds: number; lastRunAt: string | null; lastError: string | null };
  energy: AnyRow;
  production: AnyRow;
};

function DataPanel({ date }: { date: string }) {
  const [audit, setAudit] = useState<{ changes: AnyRow[]; uploads: AnyRow[] }>({ changes: [], uploads: [] });
  const [sync, setSync] = useState<SyncStatus | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<UploadPreview | null>(null);
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
      const [auditResult, syncResult] = await Promise.allSettled([
        apiRequest<{ changes: AnyRow[]; uploads: AnyRow[] }>("/audit", { signal: controller.signal }),
        apiRequest<SyncStatus>("/sync/status", { signal: controller.signal }),
      ]);
      if (controller.signal.aborted) return;
      if (auditResult.status === "fulfilled") setAudit(auditResult.value);
      else setError(messageOf(auditResult.reason));
      if (syncResult.status === "fulfilled") setSync(syncResult.value);
    } catch (requestError) {
      if (isAbortError(requestError)) return;
      setError(messageOf(requestError));
    } finally {
      if (loadController.current === controller) setLoading(false);
    }
  }, []);

  async function runSyncNow() {
    if (syncing) return;
    setSyncing(true);
    setError("");
    setNotice("");
    try {
      const result = await apiRequest<{ energy: AnyRow; production: AnyRow }>("/sync/run", { method: "POST", body: JSON.stringify({ force: true }) });
      const inserted = Number(result.energy?.inserted ?? 0);
      const updated = Number(result.energy?.updated ?? 0);
      setNotice(`동기화 완료 — 에너지 신규 ${inserted}·갱신 ${updated}행, 생산실적 ${display(result.production?.status)}`);
      await load();
    } catch (requestError) {
      setError(messageOf(requestError));
    } finally {
      setSyncing(false);
    }
  }
  useEffect(() => {
    void load();
    return () => loadController.current?.abort();
  }, [load]);

  function validFile(): File | null {
    if (!file) return null;
    if (!/\.(xlsx|xls)$/i.test(file.name)) {
      setError("xlsx 또는 xls 파일만 업로드할 수 있습니다.");
      return null;
    }
    if (file.size > 50 * 1024 * 1024) {
      setError("파일 크기는 50MB 이하여야 합니다.");
      return null;
    }
    return file;
  }

  async function previewFile(event: React.FormEvent) {
    event.preventDefault();
    const target = validFile();
    if (!target || uploading) return;
    const body = new FormData();
    body.append("file", target);
    setUploading(true);
    setError("");
    setNotice("");
    setPreview(null);
    try {
      setPreview(await apiRequest<UploadPreview>("/upload/preview", { method: "POST", body }));
    } catch (requestError) {
      setError(messageOf(requestError));
    } finally {
      setUploading(false);
    }
  }

  async function applyUpload() {
    const target = validFile();
    if (!target || !preview?.success || uploading) return;
    if (!window.confirm(`신규 ${preview.total_new}건 · 덮어쓰기 ${preview.total_overwrite}건을 MySQL에 UPSERT합니다. 계속하시겠습니까?`)) return;
    const body = new FormData();
    body.append("file", target);
    setUploading(true);
    setError("");
    setNotice("");
    try {
      const result = await apiRequest<{ rows: number; message: string }>("/upload", { method: "POST", body });
      setNotice(`${result.rows.toLocaleString("ko-KR")}행을 반영했습니다. ${result.message ?? ""}`);
      setFile(null);
      setPreview(null);
      if (fileInput.current) fileInput.current.value = "";
      await load();
    } catch (requestError) {
      setError(messageOf(requestError));
    } finally {
      setUploading(false);
    }
  }

  return <div className="screen-stack">
    <article className="card admin-form">
      <header><div><span className="eyebrow">AUTO SYNC</span><h3>엑셀 → DB 자동 동기화</h3></div><button type="button" className="primary-button" disabled={syncing} onClick={() => void runSyncNow()}><FolderSync size={16}/>{syncing ? "동기화 중..." : "지금 동기화"}</button></header>
      {sync ? <div className="sync-grid">
        <div><b>스케줄러</b><span>{sync.scheduler.enabled ? `${sync.scheduler.intervalSeconds}초 주기 실행 중` : "꺼짐"} · 최근 {display(sync.scheduler.lastRunAt).slice(0, 19).replace("T", " ")}</span>{sync.scheduler.lastError && <em className="bad">{sync.scheduler.lastError}</em>}</div>
        <div><b>에너지 원본 {sync.energy.is_up_to_date ? <i className="sync-ok">최신</i> : <i className="sync-stale">지연</i>}</b><span>파일 {display(sync.energy.file_mtime).slice(0, 19).replace("T", " ")} · 마지막 동기화 {display(sync.energy.last_sync_at)} (신규 {display(sync.energy.last_inserted)}·갱신 {display(sync.energy.last_updated)})</span></div>
        <div><b>생산실적 원본</b><span>마지막 동기화 {display(sync.production.last_sync_at)} · {display(sync.production.last_rows)}행 · {display(sync.production.last_duration_sec)}초</span></div>
      </div> : <p className="panel-copy">동기화 상태를 불러오는 중이거나 확인할 수 없습니다.</p>}
    </article>
    <form className="card upload-panel" onSubmit={previewFile}>
      <div><span className="eyebrow">EXCEL UPSERT</span><h3>에너지 실적 업로드</h3><p>1단계 미리보기로 신규·덮어쓰기 영향 범위를 확인한 뒤, 2단계에서 MySQL에 UPSERT합니다. 최대 50MB의 xlsx·xls만 허용됩니다.</p></div>
      <label className="file-picker"><Upload size={22}/><span>{file?.name ?? "Excel 파일 선택"}</span><input ref={fileInput} type="file" accept=".xlsx,.xls" onChange={event => { setFile(event.target.files?.[0] ?? null); setPreview(null); setNotice(""); setError(""); }}/></label>
      <button type="submit" className="primary-button" disabled={!file || uploading}><Play size={16}/>{uploading && !preview ? "검증 중..." : "1단계 · 검증·미리보기"}</button>
    </form>
    {error && <div className="form-message error">{error}</div>}{notice && <div className="form-message success">{notice}</div>}
    {preview && <article className="card admin-list">
      <header className="panel-header"><div><span className="eyebrow">UPLOAD PREVIEW</span><h3>{preview.success ? "미리보기 — DB 미반영" : "검증 실패"}</h3></div>
        {preview.success && <button type="button" className="primary-button" disabled={uploading} onClick={() => void applyUpload()}><Upload size={16}/>{uploading ? "반영 중..." : `2단계 · DB 반영 (신규 ${preview.total_new} · 덮어쓰기 ${preview.total_overwrite})`}</button>}
      </header>
      {!preview.success && <div className="form-message error">{preview.message}</div>}
      {preview.success && <div className="table-wrap"><table><thead><tr><th>공장</th><th>기간</th><th>일자 수</th><th>신규</th><th>덮어쓰기</th></tr></thead><tbody>{preview.summary.map((row, index) => <tr key={index}><td>{display(row["공장"])}</td><td>{display(row["기간"])}</td><td>{display(row["일자 수"])}</td><td>{display(row["신규"])}</td><td>{display(row["덮어쓰기"])}</td></tr>)}</tbody></table></div>}
      {preview.errors.length > 0 && <div className="table-wrap"><table><thead><tr><th>시트</th><th>행</th><th>컬럼</th><th>사유</th><th>값</th></tr></thead><tbody>{preview.errors.slice(0, 50).map((row, index) => <tr key={index}><td>{display(row["시트"])}</td><td>{display(row["행"])}</td><td>{display(row["컬럼"])}</td><td>{display(row["사유"])}</td><td>{display(row["값"])}</td></tr>)}</tbody></table>{preview.errors.length > 50 && <div className="empty-row">외 {preview.errors.length - 50}건의 오류가 더 있습니다.</div>}</div>}
    </article>}
    <MailCard date={date}/>
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

  return <div className="admin-grid equal">
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
    <WeatherCard/>
    <RetrainCard/>
    {error && <div className="form-message error admin-span">{error}</div>}
    {result != null && <article className="card operation-result admin-span"><strong>작업 결과</strong><pre>{JSON.stringify(result, null, 2)}</pre></article>}
  </div>;
}

type WeatherStationStatus = { last_date: string; missing_days: number | null; is_up_to_date: boolean };
type WeatherSyncResult = { station: string; added_days: number; last_date: string | null; error: string | null };

function WeatherCard() {
  const [status, setStatus] = useState<Record<string, WeatherStationStatus>>({});
  const [results, setResults] = useState<WeatherSyncResult[] | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setStatus(await apiRequest<Record<string, WeatherStationStatus>>("/weather/status"));
    } catch (requestError) {
      if (!isAbortError(requestError)) setError(messageOf(requestError));
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  async function syncNow() {
    if (running) return;
    setRunning(true);
    setError("");
    setResults(null);
    try {
      const response = await apiRequest<{ stations: WeatherSyncResult[] }>("/weather/sync", { method: "POST" });
      setResults(response.stations ?? []);
      await load();
    } catch (requestError) {
      setError(messageOf(requestError));
    } finally {
      setRunning(false);
    }
  }

  return <article className="card admin-form">
    <header><div><span className="eyebrow">WEATHER SYNC</span><h3>기상청 데이터 동기화</h3></div><CloudSun size={22}/></header>
    <div className="sync-grid">{Object.entries(status).map(([name, row]) => <div key={name}><b>{name} {row.is_up_to_date ? <i className="sync-ok">최신</i> : <i className="sync-stale">{row.missing_days ?? "-"}일 누락</i>}</b><span>보유 {row.last_date}</span></div>)}</div>
    {results && <p className="panel-copy">{results.map(row => `${row.station} ${row.error ? `오류: ${row.error}` : `+${row.added_days}일`}`).join(" · ")}</p>}
    {error && <div className="form-message error">{error}</div>}
    <button type="button" className="primary-button" disabled={running} onClick={() => void syncNow()}><CloudSun size={16}/>{running ? "동기화 중..." : "기상 데이터 동기화"}</button>
  </article>;
}

type TrainingStatus = {
  status?: string;
  message?: string;
  error?: string | null;
  progress_pct?: number;
  current_step?: string | null;
  current_factory?: string | null;
  current_target?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  data_end_date?: string | null;
};

const trainingLabels: Record<string, string> = { running: "학습 진행 중", success: "마지막 학습 성공", fail: "마지막 학습 실패", interrupted: "학습 중단됨", unknown: "이력 없음" };

function RetrainCard() {
  const [status, setStatus] = useState<TrainingStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setStatus(await apiRequest<TrainingStatus>("/model/training-status"));
    } catch (requestError) {
      if (!isAbortError(requestError)) setError(messageOf(requestError));
    }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (status?.status !== "running") return;
    const timer = window.setInterval(() => void load(), 10_000);
    return () => window.clearInterval(timer);
  }, [status?.status, load]);

  async function start() {
    if (starting || status?.status === "running") return;
    if (!window.confirm("v5 모델 재학습을 시작합니다. 서버 자원이 장시간 사용됩니다. 계속하시겠습니까?")) return;
    setStarting(true);
    setError("");
    try {
      await apiRequest("/model/retrain", { method: "POST" });
      await load();
    } catch (requestError) {
      setError(messageOf(requestError));
    } finally {
      setStarting(false);
    }
  }

  const running = status?.status === "running";
  const pct = Math.min(Math.max(Number(status?.progress_pct ?? 0), 0), 100);
  return <article className="card admin-form">
    <header><div><span className="eyebrow">MODEL RETRAIN</span><h3>v5 모델 재학습</h3></div><BrainCircuit size={22}/></header>
    <div className="sync-grid">
      <div><b>{trainingLabels[status?.status ?? "unknown"] ?? display(status?.status)}</b><span>{running ? `${status?.current_step ?? ""} ${status?.current_factory ?? ""} ${status?.current_target ?? ""}`.trim() || "준비 중" : `데이터 기준일 ${display(status?.data_end_date)} · 종료 ${display(status?.ended_at).slice(0, 19).replace("T", " ")}`}</span>{status?.error && <em className="bad">{status.error}</em>}</div>
    </div>
    {running && <div className="progress"><div><span>진행률</span><b>{pct.toFixed(0)}%</b></div><i><em style={{ width: `${pct}%` }}/></i></div>}
    {error && <div className="form-message error">{error}</div>}
    <button type="button" className="primary-button" disabled={starting || running} onClick={() => void start()}><Play size={16}/>{running ? "학습 진행 중..." : starting ? "시작 중..." : "재학습 시작"}</button>
  </article>;
}

export function AdminScreen({ factory, date, isAdmin }: { factory: string; date: string; isAdmin: boolean }) {
  const allowedTabs = useMemo<AdminTab[]>(() => isAdmin ? ["events", "targets", "data", "predictions"] : ["events", "targets"], [isAdmin]);
  const [tab, setTab] = useState<AdminTab>("events");
  useEffect(() => { if (!allowedTabs.includes(tab)) setTab("events"); }, [allowedTabs, tab]);
  const labels: Record<AdminTab, string> = { events: "이벤트 메모", targets: "절감 목표", data: "데이터·동기화", predictions: "예측·모델 운영" };

  return <section className="screen-stack">
    {!isAdmin && <div className="permission-banner"><ShieldAlert size={21}/><div><strong>조회 사용자 모드</strong><p>이벤트와 절감 목표는 열람만 가능하며 모든 변경 작업은 서버에서 차단됩니다.</p></div></div>}
    <div className="admin-tabs" role="tablist">{allowedTabs.map(item => <button type="button" role="tab" aria-selected={tab === item} className={tab === item ? "active" : ""} key={item} onClick={() => setTab(item)}>{labels[item]}</button>)}</div>
    {tab === "events" && <EventsPanel factory={factory} date={date} isAdmin={isAdmin}/>}
    {tab === "targets" && <TargetsPanel factory={factory} date={date} isAdmin={isAdmin}/>}
    {tab === "data" && isAdmin && <DataPanel date={date}/>}
    {tab === "predictions" && isAdmin && <PredictionOpsPanel factory={factory} date={date}/>}
  </section>;
}
