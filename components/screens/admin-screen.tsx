"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BrainCircuit, ClipboardPaste, CloudSun, Database, Download, Eye, FolderSync, History, Mail, Pencil, Play, RefreshCw, Save, ShieldAlert, Target, Trash2, Upload } from "lucide-react";
import { apiRequest, apiUrl, isAbortError, query } from "@/lib/bems-api";
import { factories } from "@/lib/bems-data";
import { PAGE_DEFS } from "@/lib/bems-pages";

type AnyRow = Record<string, unknown>;
type AdminTab = "events" | "targets" | "savings" | "monthly" | "data" | "predictions" | "mail" | "visibility";

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
  // savings_target 은 "경산 제외" 전용 목표 행이 없다 — 전사 성격 라벨은 모두
  // 같은 "ALL" 목표를 공유한다(backend server.py의 is_company_wide 판단과 동일).
  const targetFactory = factory === "전사" || factory === "전사(경산 제외)" ? "ALL" : factory === "남양주1" || factory === "남양주2" ? "남양주" : factory;
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

// 'YYYY-MM' ~ 'YYYY-MM' 사이 월 목록. 시작이 종료보다 늦으면 빈 배열(그리드가
// 조용히 비게 되고, 아래 안내로 이유를 알린다).
function monthRange(from: string, to: string): string[] {
  const [fromYear, fromMonth] = from.split("-").map(Number);
  const [toYear, toMonth] = to.split("-").map(Number);
  if (!fromYear || !fromMonth || !toYear || !toMonth) return [];
  const months: string[] = [];
  let year = fromYear, month = fromMonth;
  while (year < toYear || (year === toYear && month <= toMonth)) {
    months.push(`${year}-${String(month).padStart(2, "0")}`);
    month += 1;
    if (month > 12) { month = 1; year += 1; }
    if (months.length > 60) break; // 오입력으로 범위가 폭주하는 것을 막는 상한
  }
  return months;
}

type EnergyMonthlyRow = {
  month_key: string;
  total_power_kwh?: number | null; fuel_nm3?: number | null;
  water_ton?: number | null; wastewater_ton?: number | null;
  power_cost_krw?: number | null; fuel_cost_krw?: number | null;
  hasDailyData?: boolean;
};
type ProductionMonthlyRow = {
  month_key: string; category2: string;
  planned_qty?: number | null; actual_qty?: number | null;
  hasDailyData?: boolean;
};
type MonthlyEnergyForm = Record<string, string>; // "2025-06:total_power_kwh" -> 입력값 문자열

// 그리드 열 정의 — 화면에 보이는 왼→오 순서 그대로다. 엑셀 붙여넣기가 이
// 순서를 기준으로 열을 채우므로, 순서를 바꾸면 붙여넣기 대응도 함께 바뀐다.
const backfillColumns: { key: string; label: string; unit: string; width: number }[] = [
  { key: "total_power_kwh", label: "전력량", unit: "kWh", width: 118 },
  { key: "fuel_nm3", label: "연료량", unit: "Nm³", width: 108 },
  { key: "water_ton", label: "용수량", unit: "ton", width: 100 },
  { key: "wastewater_ton", label: "폐수량", unit: "ton", width: 100 },
  { key: "power_cost_krw", label: "전력비", unit: "원", width: 142 },
  { key: "fuel_cost_krw", label: "연료비", unit: "원", width: 142 },
  { key: "planned_qty", label: "계획 생산량", unit: "ton", width: 112 },
  { key: "actual_qty", label: "실적 생산량", unit: "ton", width: 112 },
];
const energyColumnKeys = backfillColumns.slice(0, 6).map(column => column.key);
const productionCategories = [
  { value: "IC", label: "IC (아이스크림)" }, { value: "MY", label: "MY (유음료)" },
  { value: "FM", label: "FM (발효유)" }, { value: "SN", label: "SN (스낵)" }, { value: "ETC", label: "기타" },
];

// 엑셀에서 복사한 값은 천단위 콤마·통화기호·공백을 달고 온다("206,412,000", "₩1,234").
// type="number" 입력은 그런 문자열을 통째로 거부해 값이 조용히 비므로, 텍스트로
// 받아 여기서 숫자만 남긴다. 빈 문자열은 "입력 안 함"(null)이고, 숫자로 해석되지
// 않는 값은 undefined 로 구분해 화면에서 붉게 표시한다.
function parseNumericCell(value: string): number | null | undefined {
  const cleaned = value.replace(/[,\s₩]/g, "");
  if (cleaned === "") return null;
  const parsed = Number(cleaned);
  return Number.isFinite(parsed) ? parsed : undefined;
}

// 클립보드 TSV(엑셀 복사 형식) → 2차원 배열. 엑셀은 끝에 개행을 붙이므로 마지막
// 빈 줄을 버린다.
function parseClipboardGrid(text: string): string[][] {
  const rows = text.replace(/\r\n?/g, "\n").split("\n");
  while (rows.length > 0 && rows[rows.length - 1].trim() === "") rows.pop();
  return rows.map(row => row.split("\t"));
}

// 경산처럼 일단위 실적이 없는 구간(전사_경산구분_및_월별적재_계획.md B-4)의
// 월별 실적을 관리자가 직접 입력한다. 저장한 값은 monthly_fallback_service 의
// 규칙 1(일별 우선)을 그대로 따른다 — 그 달에 일별 실적이 이미 있으면 화면에
// 반영되지 않으므로, 그런 달은 행을 흐리게 표시하고 이유를 알린다.
function MonthlyBackfillPanel({ isAdmin }: { isAdmin: boolean }) {
  const [factory, setFactory] = useState("경산");
  const [monthFrom, setMonthFrom] = useState("2025-01");
  const [monthTo, setMonthTo] = useState("2026-03");
  const [category, setCategory] = useState("IC");
  const months = useMemo(() => monthRange(monthFrom, monthTo), [monthFrom, monthTo]);

  const [coveredMonths, setCoveredMonths] = useState<Set<string>>(() => new Set());
  const [form, setForm] = useState<MonthlyEnergyForm>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const loadController = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    setLoading(true); setError(""); setNotice("");
    try {
      const [energyResult, productionResult] = await Promise.all([
        apiRequest<{ rows: EnergyMonthlyRow[]; coveredMonths: string[] }>(`/monthly-input/energy?${query({ factory })}`, { signal: controller.signal }),
        apiRequest<{ rows: ProductionMonthlyRow[]; coveredMonths: string[] }>(`/monthly-input/production?${query({ factory })}`, { signal: controller.signal }),
      ]);
      if (controller.signal.aborted) return;
      const nextForm: MonthlyEnergyForm = {};
      for (const row of energyResult.rows) {
        for (const key of energyColumnKeys) {
          const value = row[key as keyof EnergyMonthlyRow];
          if (typeof value === "number" && value !== 0) nextForm[`${row.month_key}:${key}`] = String(value);
        }
      }
      for (const row of productionResult.rows) {
        if (row.category2 !== category) continue;
        // 저장 단위는 kg, 화면 입력 단위는 ton — 여기서 되돌린다.
        if (row.planned_qty) nextForm[`${row.month_key}:planned_qty`] = String(row.planned_qty / 1000);
        if (row.actual_qty) nextForm[`${row.month_key}:actual_qty`] = String(row.actual_qty / 1000);
      }
      setForm(nextForm);
      setCoveredMonths(new Set([...energyResult.coveredMonths, ...productionResult.coveredMonths]));
    } catch (requestError) {
      if (isAbortError(requestError)) return;
      setError(messageOf(requestError));
    } finally {
      if (loadController.current === controller) setLoading(false);
    }
  }, [factory, category]);

  useEffect(() => {
    void load();
    return () => loadController.current?.abort();
  }, [load]);

  const cell = useCallback((month: string, field: string) => form[`${month}:${field}`] ?? "", [form]);
  function setCell(month: string, field: string, value: string) {
    setForm(current => ({ ...current, [`${month}:${field}`]: value }));
  }

  // 엑셀에서 범위를 복사해 붙여넣으면 시작 셀부터 오른쪽·아래로 채운다.
  // 그리드 밖으로 넘치는 부분은 버린다(잘린 개수를 안내로 알린다).
  function handlePaste(event: React.ClipboardEvent<HTMLInputElement>, monthIndex: number, columnIndex: number) {
    const text = event.clipboardData.getData("text/plain");
    if (!text.includes("\t") && !text.includes("\n")) return; // 단일 셀은 기본 동작에 맡긴다
    event.preventDefault();
    const grid = parseClipboardGrid(text);
    if (grid.length === 0) return;
    const updates: MonthlyEnergyForm = {};
    let overflowRows = 0, overflowColumns = 0;
    grid.forEach((rowValues, rowOffset) => {
      const month = months[monthIndex + rowOffset];
      if (month === undefined) { overflowRows += 1; return; }
      rowValues.forEach((rawValue, columnOffset) => {
        const column = backfillColumns[columnIndex + columnOffset];
        if (column === undefined) { overflowColumns += 1; return; }
        const trimmed = rawValue.trim();
        // 콤마·통화기호를 벗겨 저장한다 — 원문 그대로 두면 저장 시 숫자로 안 읽힌다.
        const parsed = parseNumericCell(trimmed);
        updates[`${month}:${column.key}`] = parsed == null ? "" : String(parsed);
      });
    });
    setForm(current => ({ ...current, ...updates }));
    const filled = Object.keys(updates).length;
    const skipped = overflowRows > 0 || overflowColumns > 0
      ? ` (범위를 벗어난 ${overflowRows > 0 ? `${overflowRows}행` : ""}${overflowRows > 0 && overflowColumns > 0 ? " · " : ""}${overflowColumns > 0 ? `${overflowColumns}칸` : ""}은 무시)`
      : "";
    setNotice(`${filled}개 셀을 붙여넣었습니다${skipped}. 저장하지 않으면 반영되지 않습니다.`);
    setError("");
  }

  const invalidCells = useMemo(
    () => Object.entries(form).filter(([, value]) => parseNumericCell(value) === undefined).map(([key]) => key),
    [form],
  );
  const filledMonths = useMemo(
    () => months.filter(month => backfillColumns.some(column => cell(month, column.key) !== "")).length,
    [months, cell],
  );

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (saving) return;
    if (invalidCells.length > 0) {
      setError(`숫자로 읽을 수 없는 값이 ${invalidCells.length}칸 있습니다. 붉게 표시된 칸을 확인하세요.`);
      return;
    }
    setSaving(true); setError(""); setNotice("");
    const numeric = (month: string, field: string) => {
      const parsed = parseNumericCell(cell(month, field));
      return parsed === undefined ? null : parsed;
    };
    try {
      const energyItems = months.map(month => ({
        factory, month_key: month,
        ...Object.fromEntries(energyColumnKeys.map(key => [key, numeric(month, key)])),
      }));
      const productionItems = months.map(month => {
        const planned = numeric(month, "planned_qty");
        const actual = numeric(month, "actual_qty");
        return {
          factory, month_key: month, category2: category,
          // 화면은 ton으로 입력받는다 — production_daily.actual_qty와 단위를 맞추려 저장은 kg(×1000).
          planned_qty: planned == null ? null : planned * 1000,
          actual_qty: actual == null ? null : actual * 1000,
        };
      });
      await apiRequest("/monthly-input/energy", { method: "PUT", body: JSON.stringify({ items: energyItems }) });
      await apiRequest("/monthly-input/production", { method: "PUT", body: JSON.stringify({ items: productionItems }) });
      setNotice("저장했습니다.");
      await load();
    } catch (requestError) {
      if (!isAbortError(requestError)) setError(messageOf(requestError));
    } finally {
      setSaving(false);
    }
  }

  return <div className="admin-grid single">
    <article className="card monthly-backfill">
      <header className="panel-header">
        <div><span className="eyebrow">MONTHLY BACKFILL</span><h3>월별 실적 백필</h3></div>
        <button type="button" className="secondary-button" onClick={() => void load()} disabled={loading}><RefreshCw size={15}/>새로고침</button>
      </header>
      <p className="panel-copy">경산처럼 일단위 실적이 없는 구간의 월 총량을 입력합니다. 이미 일별 실적이 있는 달은 그 값이 우선 적용되므로 여기 입력해도 화면에 반영되지 않습니다.</p>

      <div className="backfill-filters">
        <label className="field"><span>공장</span><select value={factory} onChange={event => setFactory(event.target.value)}>{eventFactories.map(item => <option key={item} value={item}>{item}</option>)}</select></label>
        <label className="field"><span>제품유형</span><select value={category} onChange={event => setCategory(event.target.value)}>{productionCategories.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
        <label className="field"><span>시작월</span><input type="month" value={monthFrom} onChange={event => setMonthFrom(event.target.value)}/></label>
        <label className="field"><span>종료월</span><input type="month" value={monthTo} onChange={event => setMonthTo(event.target.value)}/></label>
      </div>

      <div className="backfill-hint">
        <ClipboardPaste size={15}/>
        <p><b>엑셀에서 여러 셀을 복사해 붙여넣을 수 있습니다.</b> 붙여넣을 위치의 칸을 클릭한 뒤 <b>Ctrl+V</b> 하면 그 칸부터 오른쪽·아래로 채워집니다. 천단위 콤마·통화기호는 자동으로 제거됩니다.</p>
      </div>

      {months.length === 0
        ? <div className="form-message error">시작월이 종료월보다 늦습니다.</div>
        : loading
        ? <div className="loading inline-loading"><RefreshCw className="spin"/>불러오는 중입니다.</div>
        : <form onSubmit={save}>
          <div className="backfill-grid-wrap">
            <table className="backfill-grid">
              <colgroup><col style={{ width: 116 }}/>{backfillColumns.map(column => <col key={column.key} style={{ width: column.width }}/>)}</colgroup>
              <thead><tr>
                <th className="col-month" scope="col">월</th>
                {backfillColumns.map(column => <th key={column.key} scope="col">{column.label}<small>{column.unit}</small></th>)}
              </tr></thead>
              <tbody>
                {months.map((month, monthIndex) => {
                  const covered = coveredMonths.has(month);
                  return <tr key={month} className={covered ? "covered" : ""}>
                    <th className="col-month" scope="row">
                      <div className="month-cell">
                        <span>{month}</span>
                        {covered && <em title="이 달은 이미 일별 실적이 있어 여기 입력한 값이 화면에 반영되지 않습니다.">일별 우선</em>}
                      </div>
                    </th>
                    {backfillColumns.map((column, columnIndex) => {
                      const key = `${month}:${column.key}`;
                      return <td key={column.key}>
                        <input
                          type="text" inputMode="decimal" autoComplete="off"
                          aria-label={`${month} ${column.label}(${column.unit})`}
                          className={invalidCells.includes(key) ? "invalid" : undefined}
                          disabled={!isAdmin}
                          value={cell(month, column.key)}
                          onChange={event => setCell(month, column.key, event.target.value)}
                          onPaste={event => handlePaste(event, monthIndex, columnIndex)}
                          onFocus={event => event.currentTarget.select()}
                        />
                      </td>;
                    })}
                  </tr>;
                })}
              </tbody>
            </table>
          </div>

          <div className="backfill-footer">
            {isAdmin
              ? <button className="primary-button" type="submit" disabled={saving}><Save size={16}/>{saving ? "저장 중..." : "일괄 저장"}</button>
              : <div className="permission-note">조회 사용자는 월별 실적을 확인만 할 수 있습니다.</div>}
            <span className="backfill-count">{months.length}개월 중 <b>{filledMonths}개월</b> 입력됨{invalidCells.length > 0 && <em> · 숫자 아닌 값 {invalidCells.length}칸</em>}</span>
          </div>
          {error && <div className="form-message error">{error}</div>}{notice && <div className="form-message success">{notice}</div>}
        </form>}
    </article>
  </div>;
}

const savingsEnergyTypeFallback = [
  { value: "power", label: "전력", unit: "kWh", priced: true },
  { value: "fuel", label: "연료", unit: "Nm³", priced: true },
  { value: "water", label: "용수", unit: "ton", priced: false },
];
const savingsStatusFallback = [
  { value: "planned", label: "계획" }, { value: "ongoing", label: "진행" },
  { value: "done", label: "완료" }, { value: "dropped", label: "중단" },
];
const savingsCategoryFallback = ["설비교체", "운전개선", "공정개선", "누설저감", "계약변경", "기타"];

type SavingsThemeRow = {
  id: number; factory: string; title: string; energyType: string; energyLabel: string; unit: string;
  category: string | null; status: string; statusLabel: string; startYm: string | null; owner: string | null;
  investAmount: number | null; plannedQty: number; actualQty: number;
  plannedAmount: number | null; actualAmount: number | null; rate: number | null;
};
type SavingsOptions = {
  energyTypes: { value: string; label: string; unit: string; priced: boolean }[];
  statuses: { value: string; label: string }[];
  categories: string[];
};
type SavingsThemeForm = {
  factory: string; title: string; energy_type: string; category: string; status: string;
  start_ym: string; owner: string; invest_amount: string; note: string;
};
type SavingsThemeMonth = { month: string; plannedQty: number | null; actualQty: number | null };
type SavingsServerFile = {
  path: string; filename: string; exists: boolean; sizeBytes: number; modifiedAt: string | null; sha256: string | null;
};
type SavingsImportPreview = {
  success: boolean; year: number; totalThemes: number; newThemes: number; existingThemes: number; recordValues: number;
  byFactory: { factory: string; themes: number }[];
  samples: { factory: string; title: string; action: "new" | "update" }[];
  sourceFile: SavingsServerFile & { sha256: string };
};
type SavingsImportResult = {
  success: boolean; year: number; insertedThemes: number; updatedThemes: number; savedRecords: number; clearedRecords: number;
  sourceFile: SavingsServerFile & { sha256: string };
};

const emptySavingsForm = (factory: string): SavingsThemeForm => ({
  factory, title: "", energy_type: "power", category: "설비교체", status: "planned",
  start_ym: "", owner: "", invest_amount: "", note: "",
});

// 에너지 절감 테마 등록·수정과 테마별 월간 계획/실적(절감'량') 입력.
// 절감금액은 여기서 입력하지 않는다 — energy_daily/energy_monthly의 그 달
// 가중평균 단가를 곱해 조회 시점에 산출한다(서버 savings_theme_amounts 참고).
// 용수는 단가가 시스템 관리 대상이 아니라(2026-07-30 결정) 금액이 나오지 않고
// 절감'량'만 관리된다 — 폼에서 막지 않고 그대로 등록하게 둔다.
function SavingsThemePanel({ isAdmin }: { isAdmin: boolean }) {
  const [factory, setFactory] = useState("남양주1");
  const [year, setYear] = useState(new Date().getFullYear());
  const [themes, setThemes] = useState<SavingsThemeRow[]>([]);
  const [options, setOptions] = useState<SavingsOptions>({ energyTypes: savingsEnergyTypeFallback, statuses: savingsStatusFallback, categories: savingsCategoryFallback });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState<SavingsThemeForm>(() => emptySavingsForm(factory));

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [months, setMonths] = useState<SavingsThemeMonth[]>([]);
  const [monthForm, setMonthForm] = useState<Record<number, { planned: string; actual: string }>>({});
  const [recordsLoading, setRecordsLoading] = useState(false);
  const [recordsSaving, setRecordsSaving] = useState(false);
  const [importFileStatus, setImportFileStatus] = useState<SavingsServerFile | null>(null);
  const [importStatusLoading, setImportStatusLoading] = useState(false);
  const [importPreview, setImportPreview] = useState<SavingsImportPreview | null>(null);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState("");
  const [importNotice, setImportNotice] = useState("");

  const loadController = useRef<AbortController | null>(null);
  const detailController = useRef<AbortController | null>(null);

  const loadImportFileStatus = useCallback(async () => {
    if (!isAdmin) return;
    setImportStatusLoading(true);
    setImportError("");
    try {
      setImportFileStatus(await apiRequest<SavingsServerFile>("/savings/themes/import/server-file"));
    } catch (requestError) {
      setImportError(messageOf(requestError));
    } finally {
      setImportStatusLoading(false);
    }
  }, [isAdmin]);

  const load = useCallback(async () => {
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    setLoading(true);
    setError("");
    try {
      const result = await apiRequest<{ themes: SavingsThemeRow[]; options: SavingsOptions }>(
        `/savings?${query({ factory, date: `${year}-12-31`, status: "all" })}`,
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      setThemes(result.themes ?? []);
      if (result.options) setOptions(result.options);
    } catch (requestError) {
      if (isAbortError(requestError)) return;
      setError(messageOf(requestError));
    } finally {
      if (loadController.current === controller) setLoading(false);
    }
  }, [factory, year]);

  useEffect(() => {
    void load();
    return () => { loadController.current?.abort(); detailController.current?.abort(); };
  }, [load]);

  useEffect(() => {
    void loadImportFileStatus();
  }, [loadImportFileStatus]);

  useEffect(() => {
    if (selectedId != null && !themes.some(theme => theme.id === selectedId)) setSelectedId(null);
  }, [themes, selectedId]);

  const loadRecords = useCallback(async (themeId: number) => {
    detailController.current?.abort();
    const controller = new AbortController();
    detailController.current = controller;
    setRecordsLoading(true);
    try {
      const detail = await apiRequest<{ months: SavingsThemeMonth[] }>(`/savings/themes/${themeId}`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      setMonths(detail.months ?? []);
      setMonthForm(Object.fromEntries((detail.months ?? []).map((month, index) => [
        index + 1,
        { planned: month.plannedQty == null ? "" : String(month.plannedQty), actual: month.actualQty == null ? "" : String(month.actualQty) },
      ])));
    } catch (requestError) {
      if (!isAbortError(requestError)) setError(messageOf(requestError));
    } finally {
      if (detailController.current === controller) setRecordsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (selectedId != null) void loadRecords(selectedId);
    else { setMonths([]); setMonthForm({}); }
  }, [selectedId, loadRecords]);

  function resetForm() {
    setEditingId(null);
    setForm(emptySavingsForm(factory));
  }

  function editTheme(row: SavingsThemeRow) {
    setEditingId(row.id);
    setForm({
      factory: row.factory, title: row.title, energy_type: row.energyType,
      category: row.category ?? "", status: row.status, start_ym: row.startYm ?? "",
      owner: row.owner ?? "", invest_amount: row.investAmount == null ? "" : String(row.investAmount),
      note: "",
    });
  }

  async function submitTheme(event: React.FormEvent) {
    event.preventDefault();
    if (!form.title.trim() || saving) return;
    setSaving(true);
    setError("");
    setNotice("");
    const payload = {
      factory: form.factory,
      year,
      title: form.title.trim(),
      energy_type: form.energy_type,
      status: form.status,
      category: form.category || null,
      start_ym: form.start_ym || null,
      owner: form.owner.trim() || null,
      invest_amount: form.invest_amount === "" ? null : Number(form.invest_amount),
      note: form.note.trim() || null,
    };
    try {
      if (editingId == null) {
        await apiRequest("/savings/themes", { method: "POST", body: JSON.stringify(payload) });
        setNotice("절감 테마를 등록했습니다.");
      } else {
        await apiRequest(`/savings/themes/${editingId}`, { method: "PUT", body: JSON.stringify(payload) });
        setNotice("절감 테마를 수정했습니다.");
      }
      resetForm();
      await load();
    } catch (requestError) {
      setError(messageOf(requestError));
    } finally {
      setSaving(false);
    }
  }

  async function removeTheme(row: SavingsThemeRow) {
    if (!window.confirm(`"${row.title}" 테마를 삭제하시겠습니까? 등록된 월별 계획·실적도 함께 삭제됩니다.`)) return;
    setError("");
    try {
      const result = await apiRequest<{ recordCount: number }>(`/savings/themes/${row.id}`, { method: "DELETE" });
      setNotice(`테마와 딸린 월별 기록 ${result.recordCount}건을 삭제했습니다.`);
      if (editingId === row.id) resetForm();
      if (selectedId === row.id) setSelectedId(null);
      await load();
    } catch (requestError) {
      setError(messageOf(requestError));
    }
  }

  const selectedTheme = themes.find(theme => theme.id === selectedId) ?? null;
  const selectedUnit = selectedTheme ? options.energyTypes.find(item => item.value === selectedTheme.energyType)?.unit ?? "" : "";

  async function saveRecords() {
    if (selectedId == null || recordsSaving) return;
    setRecordsSaving(true);
    setError("");
    setNotice("");
    try {
      const items = Object.entries(monthForm).map(([month, values]) => ({
        month: Number(month),
        planned_qty: values.planned === "" ? 0 : Number(values.planned),
        actual_qty: values.actual === "" ? null : Number(values.actual),
      }));
      await apiRequest(`/savings/themes/${selectedId}/records`, { method: "PUT", body: JSON.stringify({ year, items }) });
      setNotice("월별 계획·실적을 저장했습니다.");
      await Promise.all([loadRecords(selectedId), load()]);
    } catch (requestError) {
      setError(messageOf(requestError));
    } finally {
      setRecordsSaving(false);
    }
  }

  async function previewImport() {
    if (!importFileStatus?.exists || importing) return;
    setImporting(true);
    setImportError("");
    setImportNotice("");
    try {
      const result = await apiRequest<SavingsImportPreview>(
        `/savings/themes/import/preview?${query({ year: String(year) })}`,
        { method: "POST" },
      );
      setImportPreview(result);
      setImportFileStatus(result.sourceFile);
    } catch (requestError) {
      setImportPreview(null);
      setImportError(messageOf(requestError));
    } finally {
      setImporting(false);
    }
  }

  async function applyImport() {
    if (!importPreview?.success || importing) return;
    if (importPreview.existingThemes > 0 && !window.confirm(
      `기존 테마 ${importPreview.existingThemes}건의 에너지원·분류·월별 계획/실적을 양식 값으로 갱신합니다. 계속하시겠습니까?`,
    )) return;
    setImporting(true);
    setImportError("");
    setImportNotice("");
    try {
      const result = await apiRequest<SavingsImportResult>(
        `/savings/themes/import?${query({
          year: String(year),
          expected_sha256: importPreview.sourceFile.sha256,
        })}`,
        { method: "POST" },
      );
      setImportNotice(
        `${result.year}년 절감 테마를 반영했습니다. 신규 ${result.insertedThemes}건 · 갱신 ${result.updatedThemes}건 · 월별 값 ${result.savedRecords}건`,
      );
      setImportFileStatus(result.sourceFile);
      setImportPreview(null);
      setSelectedId(null);
      await load();
    } catch (requestError) {
      setImportError(messageOf(requestError));
    } finally {
      setImporting(false);
    }
  }
  return <div className="admin-grid savings-admin">
    {isAdmin && <article className="card upload-panel admin-span">
      <div>
        <span className="eyebrow">SAVINGS XLSX SERVER IMPORT</span><h3>절감테마 서버 파일 일괄 등록</h3>
        <p>{year}년 기준으로 남양주·김해·광주·논산·경산 시트를 읽습니다. 고정 등록 파일을 Excel에서 저장하고 닫은 뒤 검증하세요. 브라우저 파일 업로드는 사용하지 않습니다.</p>
        <div className="action-row" style={{ marginTop: 12 }}>
          <button type="button" className="secondary-button" onClick={() => { window.location.href = apiUrl("/savings/themes/template"); }}><Download size={16}/>빈 양식 다운로드</button>
          <button type="button" className="secondary-button" disabled={importStatusLoading} onClick={() => void loadImportFileStatus()}><RefreshCw size={16}/>{importStatusLoading ? "확인 중..." : "서버 파일 확인"}</button>
        </div>
      </div>
      {importFileStatus && <div className="operation-result admin-span">
        <strong>{importFileStatus.exists ? `등록 파일 확인됨 · ${(importFileStatus.sizeBytes / 1024).toFixed(1)}KB` : "등록 파일이 없습니다."}</strong>
        <p>고정 경로: <code>{importFileStatus.path}</code></p>
        {importFileStatus.modifiedAt && <p>최종 수정: {new Date(importFileStatus.modifiedAt).toLocaleString("ko-KR")}</p>}
      </div>}
      <button type="button" className="primary-button" disabled={!importFileStatus?.exists || importing} onClick={() => void previewImport()}><Play size={16}/>{importing && !importPreview ? "검증 중..." : "1단계 · 서버 파일 검증"}</button>
      {importPreview && <div className="operation-result admin-span">
        <strong>{importPreview.year}년 · 테마 {importPreview.totalThemes}건 · 월별 입력값 {importPreview.recordValues}건</strong>
        <p>신규 {importPreview.newThemes}건 · 기존 갱신 {importPreview.existingThemes}건 · {importPreview.byFactory.map(item => `${item.factory} ${item.themes}건`).join(" · ")}</p>
        <p>검증 파일: {importPreview.sourceFile.filename} · 해시 {importPreview.sourceFile.sha256.slice(0, 12)}…</p>
        <div className="action-row"><button type="button" className="primary-button" disabled={importing} onClick={() => void applyImport()}><Upload size={16}/>{importing ? "반영 중..." : `2단계 · DB 반영 (${importPreview.totalThemes}건)`}</button></div>
      </div>}
      {importError && <div className="form-message error admin-span">{importError}</div>}
      {importNotice && <div className="form-message success admin-span">{importNotice}</div>}
    </article>}
    {isAdmin && <form className="card admin-form" onSubmit={submitTheme}>
      <header><div><span className="eyebrow">SAVINGS THEME</span><h3>{editingId == null ? "절감 테마 등록" : "절감 테마 수정"}</h3></div>{editingId != null && <button type="button" className="text-button" onClick={resetForm}>취소</button>}</header>
      <div className="form-grid">
        <label className="field full"><span>테마명</span><input value={form.title} onChange={event => setForm({ ...form, title: event.target.value })} placeholder="예: 노후 변압기 고효율 신품 교체" required/></label>
        <label className="field"><span>공장</span><select value={form.factory} disabled={editingId != null} onChange={event => setForm({ ...form, factory: event.target.value })}>{eventFactories.map(item => <option key={item} value={item}>{item}</option>)}</select></label>
        <label className="field"><span>에너지원</span><select value={form.energy_type} onChange={event => setForm({ ...form, energy_type: event.target.value })}>{options.energyTypes.map(item => <option key={item.value} value={item.value}>{item.label}{item.priced ? "" : " (금액 미산출)"}</option>)}</select></label>
        <label className="field"><span>분류</span><select value={form.category} onChange={event => setForm({ ...form, category: event.target.value })}>{options.categories.map(item => <option key={item} value={item}>{item}</option>)}</select></label>
        <label className="field"><span>상태</span><select value={form.status} onChange={event => setForm({ ...form, status: event.target.value })}>{options.statuses.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
        <label className="field"><span>시행월 (검증 분기점)</span><input type="month" value={form.start_ym} onChange={event => setForm({ ...form, start_ym: event.target.value })}/></label>
        <label className="field"><span>담당</span><input value={form.owner} onChange={event => setForm({ ...form, owner: event.target.value })} placeholder="예: 설비팀 홍길동"/></label>
        <label className="field"><span>투자비(원)</span><input type="number" min="0" step="1" value={form.invest_amount} onChange={event => setForm({ ...form, invest_amount: event.target.value })}/></label>
        <label className="field full"><span>메모</span><textarea rows={3} value={form.note} onChange={event => setForm({ ...form, note: event.target.value })}/></label>
      </div>
      <button className="primary-button" type="submit" disabled={saving}><Save size={16}/>{saving ? "저장 중..." : editingId == null ? "등록" : "저장"}</button>
      {error && <div className="form-message error">{error}</div>}{notice && <div className="form-message success">{notice}</div>}
    </form>}

    <article className="card admin-list">
      <header className="panel-header">
        <div><span className="eyebrow">SAVINGS THEMES</span><h3>등록된 테마</h3></div>
        <div className="action-row">
          <label className="field compact-field"><span>연도</span><input type="number" value={year} onChange={event => { setYear(Number(event.target.value) || year); setImportPreview(null); setImportNotice(""); setImportError(""); }}/></label>
          <button type="button" className="secondary-button" onClick={() => void load()} disabled={loading}><RefreshCw size={15}/>새로고침</button>
        </div>
      </header>
      {loading ? <div className="loading inline-loading"><RefreshCw className="spin"/>불러오는 중입니다.</div> : <div className="table-wrap"><table><thead><tr><th>테마명</th><th>공장</th><th>구분</th><th>상태</th><th>달성률</th>{isAdmin && <th>관리</th>}</tr></thead><tbody>
        {themes.map(row => <tr key={row.id} className={row.id === selectedId ? "selected" : ""} onClick={() => setSelectedId(row.id)} style={{ cursor: "pointer" }}>
          <td>{row.title}</td><td>{row.factory}</td><td>{row.energyLabel}</td><td><span className={`savings-status ${row.status}`}>{row.statusLabel}</span></td>
          <td>{row.rate == null ? "-" : `${display(row.rate)}%`}</td>
          {isAdmin && <td><div className="row-actions"><button type="button" aria-label="수정" onClick={event => { event.stopPropagation(); editTheme(row); }}><Pencil size={15}/></button><button type="button" aria-label="삭제" onClick={event => { event.stopPropagation(); void removeTheme(row); }}><Trash2 size={15}/></button></div></td>}
        </tr>)}
      </tbody></table>{themes.length === 0 && <div className="empty-row">{year}년에 등록된 절감 테마가 없습니다.</div>}</div>}
    </article>

    {selectedTheme && <article className="card admin-list admin-span savings-record-panel">
      <header className="panel-header"><div><span className="eyebrow">MONTHLY PLAN · ACTUAL</span><h3>{selectedTheme.title} · {year}년 월별 계획/실적 ({selectedUnit})</h3></div><button type="button" className="secondary-button" onClick={() => void loadRecords(selectedTheme.id)} disabled={recordsLoading}><RefreshCw size={15}/>새로고침</button></header>
      {recordsLoading ? <div className="loading inline-loading"><RefreshCw className="spin"/>불러오는 중입니다.</div> : <>
        <div className="table-wrap"><table className="savings-record-grid"><thead><tr><th>월</th><th>계획량 ({selectedUnit})</th><th>실적량 ({selectedUnit})</th></tr></thead><tbody>
          {months.map((month, index) => {
            const key = index + 1;
            const values = monthForm[key] ?? { planned: "", actual: "" };
            return <tr key={month.month}>
              <th scope="row">{month.month}</th>
              <td><input type="text" inputMode="decimal" disabled={!isAdmin} value={values.planned} onChange={event => setMonthForm(current => ({ ...current, [key]: { ...values, planned: event.target.value } }))} onFocus={event => event.currentTarget.select()}/></td>
              <td><input type="text" inputMode="decimal" placeholder="미입력" disabled={!isAdmin} value={values.actual} onChange={event => setMonthForm(current => ({ ...current, [key]: { ...values, actual: event.target.value } }))} onFocus={event => event.currentTarget.select()}/></td>
            </tr>;
          })}
        </tbody></table></div>
        {isAdmin ? <button className="primary-button" type="button" disabled={recordsSaving} onClick={() => void saveRecords()}><Save size={16}/>{recordsSaving ? "저장 중..." : "월별 계획·실적 저장"}</button> : <div className="permission-note">조회 사용자는 계획·실적을 확인만 할 수 있습니다.</div>}
        <p className="panel-copy">실적량 칸을 비우면 "미입력"으로 저장됩니다 — 0(측정된 실적 없음)과는 다르게 취급되어 달성률 계산에서 빠집니다. 절감금액은 여기서 입력하지 않고 그 달 에너지 단가로 자동 산출됩니다.</p>
      </>}
    </article>}
  </div>;
}

const mailPeriods = [
  { id: "daily", label: "일간" },
  { id: "weekly", label: "주간" },
  { id: "monthly", label: "월간" },
] as const;
type MailPeriod = (typeof mailPeriods)[number]["id"];

type MailPreview = { label: string; subject: string; refDate: string; recordCount: number; html: string };

// legacy 대시보드 '📧 메일 송부'의 이식 — 관리자 전용 '메일 리포트' 탭. viewer에게는
// 탭 자체가 노출되지 않으며, 서버(/mail/preview·/mail/send)도 관리자 IP를 재검사한다.
// 발송 전 실제 HTML 본문을 iframe으로 미리보고 발송할 수 있다.
function MailPanel({ date }: { date: string }) {
  const [period, setPeriod] = useState<MailPeriod>("daily");
  const [preview, setPreview] = useState<MailPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const help: Record<MailPeriod, string> = {
    daily: `기준일 ${date} 원단위 상세 · 즉시 점검 대상`,
    weekly: "직전 완결 주 (월~일, 전주비)",
    monthly: "직전 완결 월 (전년 동월비·YTD)",
  };

  // 주기·기준일이 바뀌면 이전 미리보기를 지워 오래된 본문 발송을 막는다.
  useEffect(() => { setPreview(null); setNotice(""); setError(""); }, [period, date]);

  async function loadPreview() {
    if (loading) return;
    setLoading(true); setError(""); setNotice("");
    try {
      const result = await apiRequest<MailPreview>(`/mail/preview?${query({ period, ...(period === "daily" ? { date } : {}) })}`);
      setPreview(result);
    } catch (requestError) {
      setError(messageOf(requestError));
    } finally {
      setLoading(false);
    }
  }

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

  return <div className="screen-stack">
    <article className="card admin-form">
      <header><div><span className="eyebrow">MAIL REPORT</span><h3>에너지 리포트 메일</h3></div><Mail size={22}/></header>
      <p className="panel-copy">tools/mail 파이프라인으로 HTML 리포트를 생성합니다. 발송 전 본문을 미리보고, .env의 MAIL_RECIPIENTS에게 발송하세요. · {help[period]}</p>
      <div className="mail-controls">
        <div className="segmented" role="group" aria-label="메일 발송 주기">{mailPeriods.map(item => <button type="button" key={item.id} className={period === item.id ? "active" : ""} aria-pressed={period === item.id} onClick={() => setPeriod(item.id)}>{item.label}</button>)}</div>
        <button type="button" className="secondary-button" disabled={loading} onClick={() => void loadPreview()}><RefreshCw size={15} className={loading ? "spin" : ""}/>{loading ? "생성 중..." : preview ? "미리보기 새로고침" : "미리보기 생성"}</button>
        <button type="button" className="primary-button" disabled={sending} onClick={() => void send()}><Mail size={16}/>{sending ? "발송 중..." : "발송"}</button>
      </div>
      {error && <div className="form-message error">{error}</div>}
      {notice && <div className="form-message success">{notice}</div>}
    </article>
    <article className="card mail-preview">
      <header className="panel-header"><div><span className="eyebrow">HTML PREVIEW</span><h3>{preview ? `${preview.label} · ${preview.subject}` : "본문 미리보기"}</h3></div>{preview && <span className="preview-meta">기준 {preview.refDate} · 공장 {preview.recordCount}개</span>}</header>
      {preview
        ? <iframe className="mail-frame" title="메일 본문 미리보기" sandbox="" srcDoc={preview.html}/>
        : <div className="mail-frame-empty">{loading ? "리포트 본문을 생성하는 중입니다." : "‘미리보기 생성’을 눌러 발송될 HTML 본문을 확인하세요."}</div>}
    </article>
  </div>;
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

function DataPanel() {
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

// 조회 사용자 사이드바에 보여줄 페이지를 관리자가 체크박스로 제어 — 예측 모델처럼
// 아직 안정화되지 않은 화면을 숨기거나, 데모·테스트 목적으로 노출 범위를 조정할 때 쓴다.
// 관리자 화면에는 이 설정과 무관하게 항상 모든 메뉴가 보인다(BemsApp 사이드바 로직).
function PageVisibilityPanel() {
  const [visibility, setVisibility] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setVisibility(await apiRequest<Record<string, boolean>>("/settings/page-visibility"));
    } catch (requestError) {
      if (!isAbortError(requestError)) setError(messageOf(requestError));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  function toggle(id: string) {
    setNotice("");
    setVisibility(current => ({ ...current, [id]: !(current[id] ?? true) }));
  }

  async function save() {
    if (saving) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      setVisibility(await apiRequest<Record<string, boolean>>("/settings/page-visibility", {
        method: "PUT",
        body: JSON.stringify({ pages: visibility }),
      }));
      setNotice("페이지 노출 설정을 저장했습니다. 조회 사용자 화면에는 새로고침 후 반영됩니다.");
    } catch (requestError) {
      setError(messageOf(requestError));
    } finally {
      setSaving(false);
    }
  }

  return <div className="screen-stack">
    <article className="card admin-form">
      <header><div><span className="eyebrow">VIEWER ACCESS</span><h3>조회 사용자에게 보여줄 페이지</h3></div><Eye size={22}/></header>
      <p className="panel-copy">체크를 해제하면 조회 사용자(관리자 IP 외) 사이드바에서 해당 메뉴가 사라집니다. 관리자 화면에는 이 설정과 무관하게 항상 모든 메뉴가 보입니다.</p>
      {loading ? <div className="loading inline-loading"><RefreshCw className="spin"/>불러오는 중입니다.</div> : <div className="visibility-grid">
        {PAGE_DEFS.map(item => { const Icon = item.icon; return <label key={item.id} className="visibility-item">
          <input type="checkbox" checked={visibility[item.id] !== false} onChange={() => toggle(item.id)}/>
          <Icon size={16}/>{item.label}
        </label>; })}
      </div>}
      <button type="button" className="primary-button" disabled={saving || loading} onClick={() => void save()}><Save size={16}/>{saving ? "저장 중..." : "저장"}</button>
      {error && <div className="form-message error">{error}</div>}
      {notice && <div className="form-message success">{notice}</div>}
    </article>
  </div>;
}

export function AdminScreen({ factory, date, isAdmin }: { factory: string; date: string; isAdmin: boolean }) {
  const allowedTabs = useMemo<AdminTab[]>(() => isAdmin ? ["events", "targets", "savings", "monthly", "data", "predictions", "mail", "visibility"] : ["events", "targets"], [isAdmin]);
  const [tab, setTab] = useState<AdminTab>("events");
  useEffect(() => { if (!allowedTabs.includes(tab)) setTab("events"); }, [allowedTabs, tab]);
  const labels: Record<AdminTab, string> = { events: "이벤트 메모", targets: "절감 목표", savings: "절감 테마", monthly: "월별 실적 백필", data: "데이터·동기화", predictions: "예측·모델 운영", mail: "메일 리포트", visibility: "페이지 노출 설정" };

  return <section className="screen-stack">
    {!isAdmin && <div className="permission-banner"><ShieldAlert size={21}/><div><strong>조회 사용자 모드</strong><p>이벤트와 절감 목표는 열람만 가능하며 모든 변경 작업은 서버에서 차단됩니다.</p></div></div>}
    <div className="admin-tabs" role="tablist">{allowedTabs.map(item => <button type="button" role="tab" aria-selected={tab === item} className={tab === item ? "active" : ""} key={item} onClick={() => setTab(item)}>{labels[item]}</button>)}</div>
    {tab === "events" && <EventsPanel factory={factory} date={date} isAdmin={isAdmin}/>}
    {tab === "targets" && <TargetsPanel factory={factory} date={date} isAdmin={isAdmin}/>}
    {tab === "savings" && isAdmin && <SavingsThemePanel isAdmin={isAdmin}/>}
    {tab === "monthly" && isAdmin && <MonthlyBackfillPanel isAdmin={isAdmin}/>}
    {tab === "data" && isAdmin && <DataPanel/>}
    {tab === "predictions" && isAdmin && <PredictionOpsPanel factory={factory} date={date}/>}
    {tab === "mail" && isAdmin && <MailPanel date={date}/>}
    {tab === "visibility" && isAdmin && <PageVisibilityPanel/>}
  </section>;
}
