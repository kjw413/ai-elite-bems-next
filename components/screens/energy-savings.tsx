"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown, Coins, ListChecks, RefreshCw, Target, TrendingDown } from "lucide-react";
import { Bar, BarChart, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { apiGet, isAbortError, query } from "@/lib/bems-api";
import { factoryColors } from "@/lib/bems-data";

type NullableNumber = number | null;
type EnergyTypeId = "power" | "fuel" | "water";
type StatusFilter = "active" | "all";

type OptionDef = { value: string; label: string };
type EnergyTypeOption = OptionDef & { unit: string; priced: boolean };

type MonthlyTotal = { month: string; planned: NullableNumber; actual: NullableNumber; cumulativeRate: NullableNumber };

type VerificationStatus = "verified" | "review" | "unverified" | "pending" | "duplicate";
type VerificationSummary = { status: VerificationStatus; statusLabel: string };
type VerificationDetail = VerificationSummary & {
  reason: string;
  beforeIntensity?: NullableNumber;
  afterIntensity?: NullableNumber;
  beforePrevIntensity?: NullableNumber;
  afterPrevIntensity?: NullableNumber;
  rBeforePct?: NullableNumber;
  rAfterPct?: NullableNumber;
  deltaPct?: NullableNumber;
  avoidedQty?: NullableNumber;
  explainPct?: NullableNumber;
  afterMonths?: number;
  actualQtyAfter?: NullableNumber;
};

type ReconciliationRow = {
  factory: string;
  energyType: EnergyTypeId;
  energyLabel: string;
  unit: string;
  verdict: "정합" | "과대" | "역행" | "해당없음";
  usageChange: NullableNumber;
  avoidedUsage: NullableNumber;
  registeredQty: number;
  explainPct: NullableNumber;
};

type ThemeRow = {
  id: number;
  factory: string;
  title: string;
  energyType: EnergyTypeId;
  energyLabel: string;
  unit: string;
  category: string | null;
  status: string;
  statusLabel: string;
  startYm: string | null;
  owner: string | null;
  investAmount: NullableNumber;
  plannedQty: number;
  actualQty: number;
  plannedAmount: NullableNumber;
  actualAmount: NullableNumber;
  rate: NullableNumber;
  verification: VerificationSummary;
};

type ThemeMonth = {
  month: string;
  plannedQty: NullableNumber;
  actualQty: NullableNumber;
  price: NullableNumber;
  plannedAmount: NullableNumber;
  actualAmount: NullableNumber;
};

type ThemeDetail = {
  id: number;
  factory: string;
  year: number;
  title: string;
  energyType: EnergyTypeId;
  energyLabel: string;
  unit: string;
  priced: boolean;
  category: string | null;
  status: string;
  statusLabel: string;
  startYm: string | null;
  owner: string | null;
  investAmount: NullableNumber;
  note: string | null;
  months: ThemeMonth[];
  plannedQty: number;
  actualQty: number;
  plannedAmount: NullableNumber;
  actualAmount: NullableNumber;
  rate: NullableNumber;
  verification: VerificationDetail;
};

type SavingsData = {
  baseDate: string;
  year: number;
  factory: string;
  summary: {
    plannedAmount: number; actualAmount: number; rate: NullableNumber;
    themeCount: number; verified: number; review: number; unverified: number; pending: number; duplicate: number;
  };
  monthly: MonthlyTotal[];
  themes: ThemeRow[];
  reconciliation: ReconciliationRow[];
  byFactory: { factory: string; actualAmount: number }[];
  options: { energyTypes: EnergyTypeOption[]; statuses: OptionDef[]; categories: string[]; factories: string[] };
  scopeNote: string | null;
  // 남양주1/남양주2 단독 조회 시, 목록에서 빠진 통합 시공 테마 건수 안내.
  // 통합 건은 공장별 절감량을 나눌 근거가 없어 "남양주" 소속으로만 관리된다.
  integratedNote: string | null;
};

const emptyData = (): SavingsData => ({
  baseDate: "", year: new Date().getFullYear(), factory: "전사",
  summary: { plannedAmount: 0, actualAmount: 0, rate: null, themeCount: 0, verified: 0, review: 0, unverified: 0, pending: 0, duplicate: 0 },
  monthly: [],
  themes: [],
  reconciliation: [],
  byFactory: [],
  options: { energyTypes: [], statuses: [], categories: [], factories: [] },
  scopeNote: null,
  integratedNote: null,
});

const fmt = (value: unknown, digits = 1) =>
  typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("ko-KR", { maximumFractionDigits: digits })
    : "-";

const tooltipStyle = {
  contentStyle: { borderRadius: 10, border: "1px solid var(--line)", background: "var(--card)", boxShadow: "0 6px 18px #12201814", fontSize: 12 },
  labelStyle: { color: "var(--text)" },
};

type ThemeSortColumn = "title" | "factory" | "plannedQty" | "actualQty" | "actualAmount" | "rate";
type ThemeSort = { column: ThemeSortColumn; direction: "asc" | "desc" } | null;
const themeColumns: { key: ThemeSortColumn; label: string }[] = [
  { key: "title", label: "테마명" },
  { key: "factory", label: "공장" },
  { key: "plannedQty", label: "계획량" },
  { key: "actualQty", label: "실적량" },
  { key: "actualAmount", label: "실적금액" },
  { key: "rate", label: "달성률" },
];

const verificationHints: Record<VerificationStatus, string> = {
  verified: "원단위 개선이 등록 실적을 충분히 설명합니다.",
  review: "설명률이 낮거나 관측 기간이 짧아 재확인이 필요합니다.",
  unverified: "시행 후 원단위 변화가 절감을 뒷받침하지 못합니다.",
  pending: "검증에 필요한 데이터(시행월·실적·생산량)가 아직 부족합니다.",
  duplicate: "같은 공장·에너지원의 다른 테마와 검증 구간이 겹쳐 개별 귀속이 어렵습니다.",
};

function VerificationChip({ status, statusLabel, title }: { status: VerificationStatus; statusLabel: string; title?: string }) {
  return <span className={`savings-verify ${status}`} title={title ?? verificationHints[status]}>{statusLabel}</span>;
}

const verificationOrder: { key: VerificationStatus; label: string }[] = [
  { key: "verified", label: "검증됨" }, { key: "review", label: "재확인" },
  { key: "unverified", label: "미확인" }, { key: "duplicate", label: "중복" }, { key: "pending", label: "판정 보류" },
];

function VerificationSummary({ summary }: { summary: SavingsData["summary"] }) {
  const active = verificationOrder.filter(item => summary[item.key] > 0);
  return <article className="kpi card">
    <div className="kpi-icon"><TrendingDown size={20}/></div>
    <div>
      <p>검증 현황</p>
      <div className="savings-verify-chips">
        {active.length > 0
          ? active.map(item => <span key={item.key} className={`savings-verify ${item.key}`}>{item.label} {summary[item.key]}</span>)
          : <span className="savings-verify">등록된 테마 없음</span>}
      </div>
    </div>
  </article>;
}

const verdictClass: Record<ReconciliationRow["verdict"], string> = {
  "정합": "ok", "과대": "warn", "역행": "bad", "해당없음": "neutral",
};

function SavingsKpi({ label, value, unit, note, tone, icon: Icon }: {
  label: string; value: NullableNumber; unit?: string; note?: string; tone?: "good" | "bad"; icon: typeof Coins;
}) {
  return <article className="kpi card">
    <div className="kpi-icon"><Icon size={20}/></div>
    <div>
      <p>{label}</p>
      <strong>{fmt(value, unit === "%" ? 1 : unit ? 1 : 0)} <small>{unit}</small></strong>
      {note && <span className={tone ?? ""}>{note}</span>}
    </div>
  </article>;
}

function ThemeDetailPanel({ themeId, onClose }: { themeId: number; onClose: () => void }) {
  const [detail, setDetail] = useState<ThemeDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    setLoading(true);
    setError(false);
    apiGet<ThemeDetail | null>(`/savings/themes/${themeId}`, null, controller.signal).then(result => {
      if (!current) return;
      setDetail(result.data);
      setError(!result.live);
      setLoading(false);
    }).catch(requestError => {
      if (!current || isAbortError(requestError)) return;
      setError(true);
      setLoading(false);
    });
    return () => { current = false; controller.abort(); };
  }, [themeId]);

  if (loading) return <article className="card chart-card span-all"><div className="loading inline-loading" role="status"><RefreshCw className="spin"/>테마 상세를 불러오는 중입니다.</div></article>;
  if (error || !detail) return <article className="card chart-card span-all"><div className="cost-empty"><p>테마 상세를 불러오지 못했습니다.</p></div></article>;

  const chartData = detail.months.map(month => ({ ...month, plannedQty: month.plannedQty ?? 0 }));

  return <article className="card chart-card span-all savings-detail">
    <header className="card-title">
      <h3>{detail.title}</h3>
      <div className="card-title-side">
        <span>{detail.factory} · {detail.energyLabel} · {detail.statusLabel}{detail.startYm ? ` · 시행 ${detail.startYm}` : ""}</span>
        <button type="button" className="text-button" onClick={onClose}>닫기</button>
      </div>
    </header>
    <div className="savings-detail-grid">
      <div className="chart"><ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={chartData}>
          <CartesianGrid vertical={false}/><XAxis dataKey="month"/><YAxis/>
          <Tooltip {...tooltipStyle} formatter={(value: unknown) => [fmt(value), detail.unit]}/>
          <Bar dataKey="plannedQty" name={`계획(${detail.unit})`} fill="var(--chart-previous)" opacity={0.5} radius={[3,3,0,0]} maxBarSize={26}/>
          <Bar dataKey="actualQty" name={`실적(${detail.unit})`} fill="var(--chart-target)" radius={[3,3,0,0]} maxBarSize={26}/>
        </ComposedChart>
      </ResponsiveContainer></div>
      <div className="savings-detail-side">
        <dl className="savings-detail-facts">
          <div><dt>연 누계 계획</dt><dd>{fmt(detail.plannedQty)} {detail.unit}</dd></div>
          <div><dt>연 누계 실적</dt><dd>{fmt(detail.actualQty)} {detail.unit}</dd></div>
          <div><dt>달성률</dt><dd className={detail.rate != null && detail.rate >= 100 ? "good" : undefined}>{detail.rate == null ? "-" : `${fmt(detail.rate)}%`}</dd></div>
          {detail.priced
            ? <div><dt>연 누계 절감금액</dt><dd>{detail.actualAmount == null ? "단가 미반영 구간 포함" : `${fmt(detail.actualAmount, 1)}백만원`}</dd></div>
            : <div><dt>절감금액</dt><dd>단가 관리 대상 아님 ({detail.energyLabel})</dd></div>}
          {detail.investAmount != null && <div><dt>투자비</dt><dd>{fmt(detail.investAmount / 1_000_000, 1)}백만원</dd></div>}
          {detail.owner && <div><dt>담당</dt><dd>{detail.owner}</dd></div>}
        </dl>
        <div className={`savings-verify-block ${detail.verification.status}`}>
          <div className="savings-verify-head">
            <span>원단위 전후 비교 검증</span>
            <VerificationChip status={detail.verification.status} statusLabel={detail.verification.statusLabel}/>
          </div>
          <p className="cost-note">{detail.verification.reason}</p>
          {detail.verification.avoidedQty != null && (() => {
            // Δ>0(원단위 악화)이면 회피량이 음수로 나온다 — 그대로 "회피량 −58,944"로
            // 찍으면 이중부정이라 읽히지 않고, "설명률 −28%"는 백분율로서 뜻이 없다.
            // 악화 구간에서는 같은 값을 '초과 사용량'으로 이름만 바꿔 양수로 보이고,
            // 설명률은 감춘다(등록 실적을 설명한 몫이 애초에 없으므로).
            const avoided = detail.verification.avoidedQty ?? 0;
            const worsened = avoided < 0;
            return <dl className="savings-detail-facts savings-verify-facts">
              <div><dt>시행 전 원단위 · 전년비</dt><dd>{fmt(detail.verification.beforeIntensity)} · {fmt(detail.verification.rBeforePct)}%</dd></div>
              <div><dt>시행 후 원단위 · 전년비</dt><dd>{fmt(detail.verification.afterIntensity)} · {fmt(detail.verification.rAfterPct)}%</dd></div>
              <div><dt>{worsened ? "추정 초과 사용량" : "추정 회피량"}</dt><dd className={worsened ? "bad" : "good"}>{fmt(Math.abs(avoided))} {detail.unit}</dd></div>
              {!worsened && <div><dt>설명률</dt><dd>{fmt(detail.verification.explainPct)}%</dd></div>}
              <div><dt>관측 기간</dt><dd>{detail.verification.afterMonths}개월</dd></div>
            </dl>;
          })()}
        </div>
        {detail.note && <p className="cost-note">{detail.note}</p>}
      </div>
    </div>
  </article>;
}

export function EnergySavings({ factory, requestedDate }: { factory: string; requestedDate: string }) {
  const [energyType, setEnergyType] = useState<EnergyTypeId | "">("");
  const [status, setStatus] = useState<StatusFilter>("active");
  const [data, setData] = useState<SavingsData>(() => emptyData());
  const [loading, setLoading] = useState(true);
  const [live, setLive] = useState(false);
  const [selectedThemeId, setSelectedThemeId] = useState<number | null>(null);
  const [sort, setSort] = useState<ThemeSort>(null);
  const loadController = useRef<AbortController | null>(null);

  useEffect(() => {
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    setLoading(true);
    apiGet<SavingsData>(
      `/savings?${query({ factory, date: requestedDate, ...(energyType ? { energy_type: energyType } : {}), status })}`,
      emptyData(),
      controller.signal,
    ).then(result => {
      if (loadController.current !== controller) return;
      setData(result.data);
      setLive(result.live);
      setLoading(false);
    }).catch(requestError => {
      if (loadController.current === controller && !isAbortError(requestError)) setLoading(false);
    });
    return () => controller.abort();
  }, [factory, requestedDate, energyType, status]);

  // 필터가 바뀌어 선택된 테마가 목록에서 사라지면 상세 패널도 함께 닫는다.
  useEffect(() => {
    if (selectedThemeId != null && !data.themes.some(theme => theme.id === selectedThemeId)) {
      setSelectedThemeId(null);
    }
  }, [data.themes, selectedThemeId]);

  const toggleSort = (column: ThemeSortColumn) => setSort(current => current?.column === column
    ? { column, direction: current.direction === "asc" ? "desc" : "asc" }
    : { column, direction: column === "title" ? "asc" : "desc" });

  const sortedThemes = useMemo(() => {
    if (!sort) return data.themes;
    const collator = new Intl.Collator("ko", { numeric: true, sensitivity: "base" });
    const direction = sort.direction === "asc" ? 1 : -1;
    return [...data.themes].sort((left, right) => {
      const leftValue = left[sort.column];
      const rightValue = right[sort.column];
      const leftMissing = leftValue == null;
      const rightMissing = rightValue == null;
      if (leftMissing !== rightMissing) return leftMissing ? 1 : -1;
      if (leftMissing && rightMissing) return 0;
      const result = sort.column === "title" || sort.column === "factory"
        ? collator.compare(String(leftValue), String(rightValue))
        : Number(leftValue) - Number(rightValue);
      return result * direction;
    });
  }, [data.themes, sort]);

  const activeMonthly = useMemo(() => data.monthly.filter(row => row.planned != null || row.actual != null), [data.monthly]);

  if (loading) return <div className="loading" role="status" aria-live="polite"><RefreshCw className="spin"/>절감 데이터를 불러오는 중입니다.</div>;

  return <div className="savings-screen">
    {!live && <section className="data-warning" role="alert"><TrendingDown size={20}/><div><strong>API 연결 실패 · 예시 데이터 없음</strong><p>절감 실적은 예시값으로 대체하지 않습니다. API와 DB 연결을 확인하세요.</p></div></section>}

    <div className="mode-row cost-toolbar">
      <div className="segmented" role="group" aria-label="절감 에너지원 선택">
        <button type="button" className={energyType === "" ? "active" : ""} aria-pressed={energyType === ""} onClick={() => setEnergyType("")}>전체</button>
        {data.options.energyTypes.map(item => <button type="button" key={item.value} className={energyType === item.value ? "active" : ""} aria-pressed={energyType === item.value} onClick={() => setEnergyType(item.value as EnergyTypeId)}>{item.label}</button>)}
      </div>
      <div className="segmented" role="group" aria-label="절감 테마 상태 범위">
        <button type="button" className={status === "active" ? "active" : ""} aria-pressed={status === "active"} onClick={() => setStatus("active")}>진행+완료</button>
        <button type="button" className={status === "all" ? "active" : ""} aria-pressed={status === "all"} onClick={() => setStatus("all")}>전체</button>
      </div>
      <span className="period-chip">{data.year}년 · 등록 테마 {data.summary.themeCount}건</span>
    </div>

    {data.scopeNote && <section className="alert warning cost-scope-note"><Coins size={19}/><div><strong>금액 범위 안내</strong><p>{data.scopeNote}</p></div></section>}
    {data.integratedNote && <section className="alert cost-scope-note"><ListChecks size={19}/><div><strong>통합 시공 테마 안내</strong><p>{data.integratedNote}</p></div></section>}

    <section className="kpi-grid">
      <SavingsKpi label="연간 계획 절감금액" value={data.summary.plannedAmount} unit="백만원" icon={Target}/>
      <SavingsKpi label="누계 실적 절감금액" value={data.summary.actualAmount} unit="백만원" note={data.summary.rate == null ? undefined : `달성률 ${fmt(data.summary.rate)}%`} tone={data.summary.rate != null && data.summary.rate >= 100 ? "good" : "bad"} icon={Coins}/>
      <SavingsKpi label="등록 테마" value={data.summary.themeCount} unit="건" icon={ListChecks}/>
      <VerificationSummary summary={data.summary}/>
    </section>

    <section className="content-grid">
      <article className="card chart-card span-all">
        <header className="card-title"><h3>월별 계획 대비 실적 절감금액</h3><div className="card-title-side"><span>{data.year}년 · 백만원 · 누계 달성률(%)</span></div></header>
        <div className="chart cost-monthly-chart"><ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={activeMonthly}>
            <CartesianGrid vertical={false}/><XAxis dataKey="month"/><YAxis yAxisId="amount"/><YAxis yAxisId="rate" orientation="right" unit="%"/>
            <Tooltip {...tooltipStyle} formatter={(value: unknown) => fmt(value, 1)}/>
            <Bar yAxisId="amount" dataKey="planned" name="계획(백만원)" fill="var(--chart-previous)" opacity={0.5} radius={[3,3,0,0]} maxBarSize={24}/>
            <Bar yAxisId="amount" dataKey="actual" name="실적(백만원)" fill="var(--chart-target)" radius={[3,3,0,0]} maxBarSize={24}/>
            <Line yAxisId="rate" type="linear" dataKey="cumulativeRate" name="누계 달성률(%)" stroke="var(--chart-actual)" strokeWidth={2} dot={{ r: 3 }} connectNulls={false}/>
          </ComposedChart>
        </ResponsiveContainer></div>
        {activeMonthly.length === 0 && <p className="cost-note">해당 조건의 실적이 아직 없습니다.</p>}
      </article>

      <article className="card table-card span-all savings-theme-list">
        <header className="card-title"><h3>절감 테마 목록</h3><div className="card-title-side"><span>행을 선택하면 아래에 상세가 표시됩니다</span></div></header>
        <div className="table-wrap production-ranking-scroll"><table className="production-ranking-table has-variance savings-table">
          <thead><tr>
            {themeColumns.map(column => {
              const direction = sort?.column === column.key ? sort.direction : null;
              return <th key={column.key} aria-sort={direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "none"}>
                <button type="button" className={direction ? "ranking-sort-button active" : "ranking-sort-button"} onClick={() => toggleSort(column.key)}>
                  {column.label}{direction === "asc" ? <ArrowUp size={12}/> : direction === "desc" ? <ArrowDown size={12}/> : <ArrowUpDown size={12}/>}
                </button>
              </th>;
            })}
            <th>상태</th><th>검증</th>
          </tr></thead>
          <tbody>
            {sortedThemes.map(theme => <tr key={theme.id} className={theme.id === selectedThemeId ? "selected" : ""} onClick={() => setSelectedThemeId(theme.id)} style={{ cursor: "pointer" }}>
              <td title={theme.title}>{theme.title}</td>
              <td>{theme.factory}</td>
              <td>{fmt(theme.plannedQty)} {theme.unit}</td>
              <td>{fmt(theme.actualQty)} {theme.unit}</td>
              <td>{theme.actualAmount == null ? "-" : `${fmt(theme.actualAmount, 1)}백만`}</td>
              <td className={theme.rate != null && theme.rate >= 100 ? "good" : theme.rate != null ? "bad" : undefined}>{theme.rate == null ? "-" : `${fmt(theme.rate)}%`}</td>
              <td><span className={`savings-status ${theme.status}`}>{theme.statusLabel}</span></td>
              <td><VerificationChip status={theme.verification.status} statusLabel={theme.verification.statusLabel}/></td>
            </tr>)}
            {sortedThemes.length === 0 && <tr><td colSpan={themeColumns.length + 2}>조건에 맞는 절감 테마가 없습니다.</td></tr>}
          </tbody>
        </table></div>
      </article>

      {selectedThemeId != null && <ThemeDetailPanel themeId={selectedThemeId} onClose={() => setSelectedThemeId(null)}/>}

      <article className="card chart-card span-all">
        <header className="card-title"><h3>공장별 절감 성과</h3><div className="card-title-side"><span>누계 백만원</span></div></header>
        <div className="chart"><ResponsiveContainer width="100%" height="100%">
          <BarChart data={data.byFactory}>
            <CartesianGrid vertical={false}/><XAxis dataKey="factory"/><YAxis/>
            <Tooltip {...tooltipStyle} formatter={(value: unknown) => [fmt(value, 1), "백만원"]}/>
            <Bar dataKey="actualAmount" name="절감금액" radius={[4,4,0,0]} maxBarSize={40}>
              {data.byFactory.map(row => <Bar key={row.factory} dataKey="actualAmount" fill={factoryColors[row.factory] ?? "var(--chart-target)"}/>)}
            </Bar>
          </BarChart>
        </ResponsiveContainer></div>
        {data.byFactory.length === 0 && <p className="cost-note">절감금액이 산출된 공장이 없습니다.</p>}
      </article>

      {data.reconciliation.length > 0 && <article className="card table-card span-all savings-reconcile">
        <header className="card-title"><h3>공장별 총량 대사</h3><div className="card-title-side"><span>YTD 실제 사용량 변화 vs 등록 절감 실적 — 개별 테마 검증이 겹칠 때의 최종 확인선</span></div></header>
        <div className="table-wrap"><table className="production-ranking-table savings-table">
          <thead><tr><th>공장</th><th>에너지원</th><th>YTD 사용량 변화</th><th>추정 회피량</th><th>등록 실적</th><th>설명률</th><th>판정</th></tr></thead>
          <tbody>
            {data.reconciliation.map(row => <tr key={`${row.factory}-${row.energyType}`}>
              <td>{row.factory}</td>
              <td>{row.energyLabel}</td>
              <td className={row.usageChange != null && row.usageChange < 0 ? "good" : "bad"}>{row.usageChange == null ? "-" : `${fmt(row.usageChange)} ${row.unit}`}</td>
              <td>{row.avoidedUsage == null ? "-" : `${fmt(row.avoidedUsage)} ${row.unit}`}</td>
              <td>{fmt(row.registeredQty)} {row.unit}</td>
              <td>{row.explainPct == null ? "-" : `${fmt(row.explainPct)}%`}</td>
              <td><span className={`savings-verdict ${verdictClass[row.verdict]}`}>{row.verdict}</span></td>
            </tr>)}
          </tbody>
        </table></div>
      </article>}
    </section>
  </div>;
}
