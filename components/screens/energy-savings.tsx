"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown, Coins, ListChecks, RefreshCw, Target, TrendingDown } from "lucide-react";
import { Bar, BarChart, CartesianGrid, Cell, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { DataToggle } from "@/components/data-toggle";
import { PivotTable } from "@/components/pivot-table";
import { ToggleLegend, useSeriesToggle } from "@/components/toggle-legend";
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
  verdict: "정합" | "과대" | "역행" | "해당없음" | "비교불가" | "등록미완료";
  currentUsage: NullableNumber;
  previousUsage: NullableNumber;
  currentProductionTon: NullableNumber;
  previousProductionTon: NullableNumber;
  currentMeasuredMonths: number;
  previousMeasuredMonths: number;
  coverageMatched: boolean;
  coverage: {
    status: "complete" | "incomplete" | "no-complete-month";
    comparedMonths: number[];
    missing: { factory: string; month: number; period: string; metric: string }[];
  };
  coverageNote: string | null;
  usageChange: NullableNumber;
  usageChangePct: NullableNumber;
  productionChangePct: NullableNumber;
  previousIntensity: NullableNumber;
  currentIntensity: NullableNumber;
  intensityChangePct: NullableNumber;
  expectedUsage: NullableNumber;
  expectedAfterRegistered: NullableNumber;
  normalizedUsageChange: NullableNumber;
  avoidedUsage: NullableNumber;
  residualQty: NullableNumber;
  registeredQty: NullableNumber;
  registeredEnteredCount: number;
  registeredCoverageComplete: boolean;
  registeredMissingCount: number;
  registeredCoverageNote: string | null;
  explainPct: NullableNumber;
  registeredExceedsBaseline: boolean | null;
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
  annualPlannedQty?: number;
  plannedYtdQty?: number;
  actualYtdQty?: NullableNumber;
  annualProgressRate?: NullableNumber;
  ytdRate?: NullableNumber;
  actualEnteredMonths?: number;
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
  annualPlannedQty?: number;
  plannedYtdQty?: number;
  actualYtdQty?: NullableNumber;
  annualProgressRate?: NullableNumber;
  ytdRate?: NullableNumber;
  actualEnteredMonths?: number;
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
    plannedYtdAmount?: NullableNumber; actualYtdAmount?: NullableNumber; ytdRate?: NullableNumber;
    themeCount: number; verified: number; review: number; unverified: number; pending: number; duplicate: number;
  };
  monthly: MonthlyTotal[];
  themes: ThemeRow[];
  reconciliation: ReconciliationRow[];
  reconciliationPeriod: {
    basis: "completed-months";
    currentFrom: string | null;
    currentTo: string | null;
    previousFrom: string | null;
    previousTo: string | null;
    excludedPartialMonth: boolean;
  };
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
  reconciliationPeriod: {
    basis: "completed-months",
    currentFrom: null,
    currentTo: null,
    previousFrom: null,
    previousTo: null,
    excludedPartialMonth: false,
  },
  byFactory: [],
  options: { energyTypes: [], statuses: [], categories: [], factories: [] },
  scopeNote: null,
  integratedNote: null,
});

const fmt = (value: unknown, digits = 1) =>
  typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("ko-KR", { maximumFractionDigits: digits })
    : "-";

const explicitOrFallback = <T,>(value: T | undefined, fallback: T): T =>
  value === undefined ? fallback : value;

const tooltipStyle = {
  contentStyle: { borderRadius: 10, border: "1px solid var(--line)", background: "var(--card)", boxShadow: "0 6px 18px #12201814", fontSize: 12 },
  labelStyle: { color: "var(--text)" },
};

type ThemeSortColumn = "title" | "factory" | "plannedQty" | "actualQty" | "actualAmount" | "rate";
type ThemeSort = { column: ThemeSortColumn; direction: "asc" | "desc" } | null;
const themeColumns: { key: ThemeSortColumn; label: string }[] = [
  { key: "title", label: "절감 과제" },
  { key: "factory", label: "공장" },
  { key: "plannedQty", label: "연간 계획량" },
  { key: "actualQty", label: "실적 누계 (YTD)" },
  { key: "actualAmount", label: "실적 누계금액 (YTD)" },
  { key: "rate", label: "연간 계획 진행률" },
];

const verificationHints: Record<VerificationStatus, string> = {
  verified: "등록한 절감 실적이 실제 에너지 원단위 개선으로 확인됩니다.",
  review: "효과가 일부 확인됐지만 관측 기간이나 개선 폭을 더 확인해야 합니다.",
  unverified: "시행 후 실제 에너지 원단위에서 절감 효과가 확인되지 않았습니다.",
  pending: "시행월·등록 실적·생산량 등 비교에 필요한 데이터가 부족합니다.",
  duplicate: "같은 공장·에너지원에서 여러 과제가 함께 시행돼 과제별 효과를 나누기 어렵습니다.",
};

const verificationLabels: Record<VerificationStatus, string> = {
  verified: "효과 확인",
  review: "추가 확인 필요",
  unverified: "효과 미확인",
  pending: "데이터 부족",
  duplicate: "과제별 구분 어려움",
};

function VerificationChip({ status, title }: { status: VerificationStatus; title?: string }) {
  return <span className={`savings-verify ${status}`} title={title ?? verificationHints[status]}>{verificationLabels[status]}</span>;
}

function TrendPct({ value, lowerIsBetter = false }: { value: NullableNumber | undefined; lowerIsBetter?: boolean }) {
  if (value == null) return <span className="savings-trend flat">-</span>;
  const direction = value > 0 ? "증가" : value < 0 ? "감소" : "변동 없음";
  const tone = value === 0
    ? "flat"
    : lowerIsBetter
      ? value < 0 ? "improved" : "worsened"
      : value > 0 ? "increase" : "decrease";
  const arrow = value > 0 ? "↑" : value < 0 ? "↓" : "→";
  return <span className={"savings-trend " + tone} aria-label={"전년 대비 " + direction + " " + fmt(Math.abs(value)) + "%"}>{arrow} {fmt(Math.abs(value))}%</span>;
}

const verificationOrder: { key: VerificationStatus; label: string }[] = [
  { key: "verified", label: verificationLabels.verified },
  { key: "review", label: verificationLabels.review },
  { key: "unverified", label: verificationLabels.unverified },
  { key: "duplicate", label: verificationLabels.duplicate },
  { key: "pending", label: verificationLabels.pending },
];

function VerificationSummary({ summary }: { summary: SavingsData["summary"] }) {
  const active = verificationOrder.filter(item => summary[item.key] > 0);
  return <article className="kpi card savings-verification-kpi">
    <div className="kpi-icon"><TrendingDown size={20}/></div>
    <div>
      <p>실제 사용량 확인</p>
      <div className="savings-verify-chips">
        {active.length > 0
          ? active.map(item => <span key={item.key} className={`savings-verify ${item.key}`}>{item.label} {summary[item.key]}</span>)
          : <span className="savings-verify">조회된 절감 과제 없음</span>}
      </div>
    </div>
  </article>;
}

const verdictClass: Record<ReconciliationRow["verdict"], string> = {
  "정합": "ok", "과대": "warn", "역행": "bad", "해당없음": "neutral", "비교불가": "neutral", "등록미완료": "warn",
};

const verdictLabels: Record<ReconciliationRow["verdict"], string> = {
  "정합": "절감 흐름 확인",
  "과대": "등록 실적 재확인",
  "역행": "절감 효과 미확인",
  "해당없음": "등록 실적 없음",
  "비교불가": "비교 데이터 부족",
  "등록미완료": "등록 실적 입력 필요",
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

function ThemeDetailPanel({ themeId, requestedDate, panelId, onClose }: { themeId: number; requestedDate: string; panelId: string; onClose: () => void }) {
  const [detail, setDetail] = useState<ThemeDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const detailLegend = useSeriesToggle();

  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    setLoading(true);
    setError(false);
    apiGet<ThemeDetail | null>("/savings/themes/" + themeId + "?" + query({ date: requestedDate }), null, controller.signal).then(result => {
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
  }, [themeId, requestedDate]);

  if (loading) return <article id={panelId} className="card chart-card span-all" role="region" aria-live="polite" aria-label="선택한 절감 과제 상세">
    <header className="card-title"><h3>과제 상세</h3><button type="button" className="text-button" onClick={onClose}>닫기</button></header>
    <div className="loading inline-loading" role="status"><RefreshCw className="spin"/>과제 상세를 불러오는 중입니다.</div>
  </article>;
  if (error || !detail) return <article id={panelId} className="card chart-card span-all" role="region" aria-live="polite" aria-label="선택한 절감 과제 상세">
    <header className="card-title"><h3>과제 상세</h3><button type="button" className="text-button" onClick={onClose}>닫기</button></header>
    <div className="cost-empty savings-empty"><p>과제 상세를 불러오지 못했습니다. 닫은 뒤 과제를 다시 선택해 주세요.</p></div>
  </article>;

  const chartData = detail.months;
  const hasChartData = chartData.some(month => month.plannedQty != null || month.actualQty != null);
  const annualPlannedQty = explicitOrFallback(detail.annualPlannedQty, detail.plannedQty);
  const actualYtdQty = explicitOrFallback(detail.actualYtdQty, detail.actualQty);
  const annualProgressRate = explicitOrFallback(detail.annualProgressRate, detail.rate);
  const legendItems = [
    { key: "plannedQty", label: "월 계획 (연초 확정)", color: "var(--chart-previous)" },
    { key: "actualQty", label: "월 실적 (MTD)", color: "var(--chart-target)" },
  ];
  const detailPct = (value: NullableNumber | undefined) => value == null ? "-" : fmt(value) + "%";

  return <article id={panelId} className="card chart-card span-all savings-detail" role="region" aria-live="polite" aria-label="선택한 절감 과제 상세">
    <header className="card-title savings-section-title">
      <div>
        <h3>{detail.title}</h3>
        <p>{detail.factory} · {detail.energyLabel} · {detail.statusLabel}{detail.startYm && <> · 시행 {detail.startYm}</>}</p>
      </div>
      <button type="button" className="text-button" onClick={onClose}>상세 닫기</button>
    </header>
    <div className="savings-detail-grid">
      <div>
        {hasChartData ? <>
          <div className="chart"><ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={chartData}>
              <CartesianGrid vertical={false}/><XAxis dataKey="month"/><YAxis/>
              <Tooltip {...tooltipStyle} formatter={(value: unknown, name: unknown) => [fmt(value) + " " + detail.unit, String(name ?? "")]}/>
              {!detailLegend.isHidden("plannedQty") && <Bar dataKey="plannedQty" name="월 계획 (연초 확정)" fill="var(--chart-previous)" opacity={0.5} radius={[3,3,0,0]} maxBarSize={26}/>}
              {!detailLegend.isHidden("actualQty") && <Bar dataKey="actualQty" name="월 실적 (MTD)" fill="var(--chart-target)" radius={[3,3,0,0]} maxBarSize={26}/>}
            </ComposedChart>
          </ResponsiveContainer></div>
          <ToggleLegend items={legendItems} hidden={detailLegend.hidden} onToggle={detailLegend.toggle}/>
          <DataToggle label="월 계획·실적 데이터 보기"><PivotTable
            periods={chartData.map(month => month.month)}
            periodLabel="월"
            totalLabel="합계"
            rows={[
              { key: "plannedQty", label: "월 계획·연간 합계 (" + detail.unit + ")", values: chartData.map(month => month.plannedQty), total: annualPlannedQty, format: value => value == null ? "-" : fmt(Number(value)) },
              { key: "actualQty", label: "월 실적(MTD)·누계(YTD) (" + detail.unit + ")", values: chartData.map(month => month.actualQty), total: actualYtdQty, format: value => value == null ? "-" : fmt(Number(value)) },
            ]}
          /></DataToggle>
        </> : <div className="cost-empty savings-empty"><p>월 계획·실적이 없습니다.</p></div>}
      </div>
      <div className="savings-detail-side">
        <dl className="savings-detail-facts">
          <div><dt>연간 계획 절감량</dt><dd>{fmt(annualPlannedQty)} {detail.unit}</dd></div>
          <div><dt>실적 누계 (YTD)</dt><dd>{actualYtdQty == null ? "-" : fmt(actualYtdQty) + " " + detail.unit}</dd></div>
          <div><dt>연간 계획 진행률</dt><dd className={annualProgressRate != null && annualProgressRate >= 100 ? "good" : undefined}>{annualProgressRate == null ? "-" : fmt(annualProgressRate) + "%"}</dd></div>
          {detail.priced
            ? <div><dt>실적 누계금액 (YTD)</dt><dd>{detail.actualAmount == null ? "단가 미반영 구간 포함" : fmt(detail.actualAmount, 1) + "백만원"}</dd></div>
            : <div><dt>절감금액</dt><dd>{detail.energyLabel} 단가를 관리하지 않아 미산출</dd></div>}
          {detail.investAmount != null && <div><dt>투자비</dt><dd>{fmt(detail.investAmount / 1_000_000, 1)}백만원</dd></div>}
          {detail.owner && <div><dt>담당자</dt><dd>{detail.owner}</dd></div>}
        </dl>
        <div className={"savings-verify-block " + detail.verification.status}>
          <div className="savings-verify-head">
            <span>실제 사용량 확인</span>
            <VerificationChip status={detail.verification.status}/>
          </div>
          <p className="cost-note">{detail.verification.reason}</p>
          {detail.verification.avoidedQty != null && (() => {
            const avoided = detail.verification.avoidedQty ?? 0;
            const worsened = avoided < 0;
            return <dl className="savings-detail-facts savings-verify-facts">
              <div><dt>시행 전 에너지 원단위 · 전년 대비</dt><dd>{fmt(detail.verification.beforeIntensity)} · <TrendPct value={detail.verification.rBeforePct} lowerIsBetter/></dd></div>
              <div><dt>시행 후 에너지 원단위 · 전년 대비</dt><dd>{fmt(detail.verification.afterIntensity)} · <TrendPct value={detail.verification.rAfterPct} lowerIsBetter/></dd></div>
              <div><dt>{worsened ? "생산량 보정 추정 초과 사용량" : "생산량 보정 추정 절감량"}</dt><dd className={worsened ? "bad" : "good"}>{fmt(Math.abs(avoided))} {detail.unit}</dd></div>
              {!worsened && <div><dt>등록 실적 대비 추정 절감량</dt><dd>{detailPct(detail.verification.explainPct)}</dd></div>}
              <div><dt>확인에 사용한 기간</dt><dd>{detail.verification.afterMonths == null ? "-" : detail.verification.afterMonths + "개월"}</dd></div>
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
  const themeRowRefs = useRef(new Map<number, HTMLButtonElement>());
  const monthlyLegend = useSeriesToggle();
  const factoryLegend = useSeriesToggle();

  useEffect(() => {
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    setLoading(true);
    apiGet<SavingsData>(
      "/savings?" + query({ factory, date: requestedDate, ...(energyType ? { energy_type: energyType } : {}), status }),
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

  useEffect(() => {
    if (selectedThemeId != null && !data.themes.some(theme => theme.id === selectedThemeId)) {
      setSelectedThemeId(null);
    }
  }, [data.themes, selectedThemeId]);

  const closeThemeDetail = () => {
    const themeId = selectedThemeId;
    setSelectedThemeId(null);
    if (themeId != null) {
      requestAnimationFrame(() => themeRowRefs.current.get(themeId)?.focus());
    }
  };

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

  const activeMonthly = useMemo(
    () => data.monthly.filter(row => row.planned != null || row.actual != null),
    [data.monthly],
  );
  const selectedEnergyOption = data.options.energyTypes.find(item => item.value === energyType);
  const showAmountSections = energyType === "" || (
    selectedEnergyOption ? selectedEnergyOption.priced : energyType !== "water"
  );
  const showFactoryPerformance = (
    showAmountSections && factory === "전사" && data.byFactory.length > 0
  );
  const reconciliationPeriodLabel = data.reconciliationPeriod.currentTo
    ? data.reconciliationPeriod.currentFrom + "~" + data.reconciliationPeriod.currentTo + " · 완료월 기준"
    : "비교할 완료 월 없음";
  const factoryLegendItems = useMemo(
    () => data.byFactory.map(row => ({
      key: row.factory,
      label: row.factory === "남양주" ? "남양주 통합" : row.factory,
      color: factoryColors[row.factory] ?? "var(--chart-target)",
    })),
    [data.byFactory],
  );
  const visibleFactoryRows = useMemo(
    () => data.byFactory
      .filter(row => !factoryLegend.hidden.has(row.factory))
      .map(row => ({ ...row, label: row.factory === "남양주" ? "남양주 통합" : row.factory })),
    [data.byFactory, factoryLegend.hidden],
  );
  const factoryLabel = (value: string) => value === "남양주" ? "남양주 통합" : value;
  const plannedYtdAmount = explicitOrFallback(data.summary.plannedYtdAmount, data.summary.plannedAmount);
  const actualYtdAmount = explicitOrFallback(data.summary.actualYtdAmount, data.summary.actualAmount);
  const summaryYtdRate = explicitOrFallback(data.summary.ytdRate, data.summary.rate);
  const monthlyLegendItems = [
    { key: "planned", label: "월 계획 (연초 확정)", color: "var(--chart-previous)" },
    { key: "actual", label: "월 실적 (MTD)", color: "var(--chart-target)" },
    { key: "cumulativeRate", label: "누계 달성률 (YTD)", color: "var(--chart-actual)" },
  ];

  if (loading) return <div className="loading" role="status" aria-live="polite"><RefreshCw className="spin"/>절감 데이터를 불러오는 중입니다.</div>;

  return <div className="savings-screen">
    {!live && <section className="data-warning" role="alert"><TrendingDown size={20}/><div><strong>절감 데이터를 불러오지 못했습니다</strong><p>이 화면은 예시값을 표시하지 않습니다. API와 데이터베이스 연결을 확인하세요.</p></div></section>}

    <div className="mode-row cost-toolbar savings-toolbar">
      <div className="savings-filter-group">
        <span className="savings-filter-label">에너지원</span>
        <div className="segmented" role="group" aria-label="조회할 절감 에너지원">
          <button type="button" className={energyType === "" ? "active" : ""} aria-pressed={energyType === ""} onClick={() => setEnergyType("")}>모든 에너지원</button>
          {data.options.energyTypes.map(item => <button type="button" key={item.value} className={energyType === item.value ? "active" : ""} aria-pressed={energyType === item.value} onClick={() => setEnergyType(item.value as EnergyTypeId)}>{item.label}</button>)}
        </div>
      </div>
      <div className="savings-filter-group">
        <span className="savings-filter-label">과제 상태</span>
        <div className="segmented" role="group" aria-label="조회할 절감 과제 상태">
          <button type="button" className={status === "active" ? "active" : ""} aria-pressed={status === "active"} onClick={() => setStatus("active")}>진행·완료</button>
          <button type="button" className={status === "all" ? "active" : ""} aria-pressed={status === "all"} onClick={() => setStatus("all")}>모든 상태</button>
        </div>
      </div>
      <span className="period-chip">기준 {data.baseDate || requestedDate} · 계획 연초 확정 · 실적 MTD / YTD · {data.summary.themeCount}건</span>
    </div>

    {data.scopeNote && <section className="alert warning cost-scope-note"><Coins size={19}/><div><strong>금액 합계에 포함되지 않는 항목</strong><p>{data.scopeNote}</p></div></section>}
    {data.integratedNote && <section className="alert cost-scope-note"><ListChecks size={19}/><div><strong>남양주 통합 과제 표시 범위</strong><p>{data.integratedNote}</p></div></section>}

    <section className={"kpi-grid compact savings-kpis" + (showAmountSections ? "" : " unpriced")}>
      {showAmountSections ? <>
        <SavingsKpi label="계획 누계금액 (YTD)" value={plannedYtdAmount} unit="백만원" note="연초 확정 월 계획의 누계" icon={Target}/>
        <SavingsKpi label="실적 누계금액 (YTD)" value={actualYtdAmount} unit="백만원" note={summaryYtdRate == null ? undefined : "YTD 계획의 " + fmt(summaryYtdRate) + "%"} tone={summaryYtdRate != null && summaryYtdRate >= 100 ? "good" : "bad"} icon={Coins}/>
      </> : <SavingsKpi label="조회한 절감 과제" value={data.summary.themeCount} unit="건" note="용수 단가는 관리하지 않아 금액은 미산출" icon={ListChecks}/>}
      <VerificationSummary summary={data.summary}/>
    </section>

    <section className="content-grid savings-content-grid">
      {showAmountSections && <article className={"card chart-card savings-monthly-card" + (showFactoryPerformance ? "" : " span-all")}>
        <header className="card-title savings-section-title">
          <div>
            <h3>월 계획·실적</h3>
          </div>
          <span>계획: 연초 확정 · 실적: 해당 월(MTD) · 선: 누계(YTD)</span>
        </header>
        {activeMonthly.length > 0 ? <>
          <div className="chart savings-monthly-chart"><ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={activeMonthly}>
              <CartesianGrid vertical={false}/><XAxis dataKey="month"/><YAxis yAxisId="amount"/><YAxis yAxisId="rate" orientation="right" unit="%"/>
              <Tooltip {...tooltipStyle} formatter={(value: unknown, name: unknown) => [String(name).includes("달성률") ? fmt(value, 1) + "%" : fmt(value, 1) + " 백만원", String(name ?? "")]}/>
              {!monthlyLegend.isHidden("planned") && <Bar yAxisId="amount" dataKey="planned" name="월 계획 (연초 확정)" fill="var(--chart-previous)" opacity={0.5} radius={[3,3,0,0]} maxBarSize={24}/>}
              {!monthlyLegend.isHidden("actual") && <Bar yAxisId="amount" dataKey="actual" name="월 실적 (MTD)" fill="var(--chart-target)" radius={[3,3,0,0]} maxBarSize={24}/>}
              {!monthlyLegend.isHidden("cumulativeRate") && <Line yAxisId="rate" type="linear" dataKey="cumulativeRate" name="누계 달성률 (YTD)" stroke="var(--chart-actual)" strokeWidth={2} dot={{ r: 3, fill: "var(--chart-actual)", stroke: "var(--card)", strokeWidth: 2 }} connectNulls={false}/>}
            </ComposedChart>
          </ResponsiveContainer></div>
          <ToggleLegend items={monthlyLegendItems} hidden={monthlyLegend.hidden} onToggle={monthlyLegend.toggle}/>
          <DataToggle label="월 계획·실적 데이터 보기"><PivotTable
            periods={activeMonthly.map(row => row.month)}
            periodLabel="월"
            totalLabel="누계 (YTD)"
            rows={[
              { key: "planned", label: "월 계획(연초 확정) (백만원)", values: activeMonthly.map(row => row.planned), total: plannedYtdAmount, format: value => value == null ? "-" : fmt(Number(value), 1) },
              { key: "actual", label: "월 실적(MTD) (백만원)", values: activeMonthly.map(row => row.actual), total: actualYtdAmount, format: value => value == null ? "-" : fmt(Number(value), 1) },
              { key: "cumulativeRate", label: "누계 달성률(YTD) (%)", values: activeMonthly.map(row => row.cumulativeRate), total: summaryYtdRate, format: value => value == null ? "-" : fmt(Number(value), 1) + "%" },
            ]}
          /></DataToggle>
        </> : <div className="cost-empty savings-empty"><p>이 조건에서 금액으로 환산할 수 있는 계획·등록 실적이 없습니다.</p></div>}
      </article>}

      {showFactoryPerformance && <article className="card chart-card savings-factory-card">
        <header className="card-title savings-section-title">
          <div>
            <h3>공장별 실적 누계금액 (YTD)</h3>
          </div>
          <span>백만원</span>
        </header>
        {data.byFactory.length > 0 ? <>
          {visibleFactoryRows.length > 0
            ? <div className="chart savings-factory-chart"><ResponsiveContainer width="100%" height="100%">
                <BarChart data={visibleFactoryRows}>
                  <CartesianGrid vertical={false}/><XAxis dataKey="label" interval={0} angle={-25} textAnchor="end" height={58}/><YAxis/>
                  <Tooltip {...tooltipStyle} formatter={(value: unknown, name: unknown) => [fmt(value, 1) + " 백만원", String(name ?? "")]}/>
                  <Bar dataKey="actualAmount" name="실적 누계금액 (YTD)" radius={[4,4,0,0]} maxBarSize={40}>
                    {visibleFactoryRows.map(row => <Cell key={row.factory} fill={factoryColors[row.factory] ?? "var(--chart-target)"}/>)}
                  </Bar>
                </BarChart>
              </ResponsiveContainer></div>
            : <div className="cost-empty savings-empty"><p>범례에서 표시할 공장을 선택하세요.</p></div>}
          <ToggleLegend items={factoryLegendItems} hidden={factoryLegend.hidden} onToggle={factoryLegend.toggle}/>
          {visibleFactoryRows.length > 0 && <DataToggle label="공장별 누계 데이터 보기"><PivotTable
            periods={visibleFactoryRows.map(row => row.label)}
            periodLabel="공장"
            totalLabel="표시 합계"
            rows={[
              { key: "actualAmount", label: "실적 누계금액(YTD) (백만원)", values: visibleFactoryRows.map(row => row.actualAmount), total: visibleFactoryRows.reduce((sum, row) => sum + row.actualAmount, 0), format: value => value == null ? "-" : fmt(Number(value), 1) },
            ]}
          /></DataToggle>}
        </> : <div className="cost-empty savings-empty"><p>금액으로 환산된 공장별 등록 실적이 없습니다.</p></div>}
      </article>}

      <article className="card table-card span-all savings-impact">
        <header className="card-title savings-section-title">
          <div>
            <h3>생산량을 고려한 실제 절감 확인</h3>
            <p>전년 동월 생산 원단위로 예상 사용량을 계산합니다.</p>
          </div>
          <span>{reconciliationPeriodLabel}</span>
        </header>
        <details className="savings-method-note">
          <summary>계산 기준 보기</summary>
          <p>전년 동월의 공장별 원단위(사용량 ÷ 생산량)에 올해 생산량을 곱한 예상 사용량과 실제 사용량을 비교합니다.</p>
          <small>직전 완료월까지만 계산하는 근사치입니다. 품목 구성·날씨·가동 조건은 별도로 제거하지 않으며, 완료월 실적이 비어 있으면 판정을 보류합니다.</small>
        </details>
        {data.reconciliation.length > 0 ? <div className="table-wrap">
          <table className="savings-impact-table">
            <colgroup><col className="impact-scope"/><col className="impact-production"/><col/><col/><col/><col/><col className="impact-verdict"/></colgroup>
            <thead><tr>
              <th>공장·에너지원</th>
              <th>생산량 (전년 → 올해)</th>
              <th>실제 사용량</th>
              <th>생산량 보정 예상</th>
              <th>예상 대비 실제</th>
              <th>완료월 실적 누계 (YTD)</th>
              <th>해석</th>
            </tr></thead>
            <tbody>{data.reconciliation.map(row => {
              const canEstimate = (
                row.coverageMatched && row.expectedUsage != null
                && row.verdict !== "비교불가"
              );
              const estimatedSaving = canEstimate ? row.avoidedUsage : null;
              const excessUsage = canEstimate && row.normalizedUsageChange != null && row.normalizedUsageChange > 0
                ? row.normalizedUsageChange
                : null;
              const estimateText = !canEstimate
                ? "-"
                : estimatedSaving != null && estimatedSaving > 0
                  ? fmt(estimatedSaving) + " " + row.unit + " 절감 추정"
                  : estimatedSaving === 0
                    ? "예상과 같음 (차이 0 " + row.unit + ")"
                    : excessUsage != null
                      ? fmt(excessUsage) + " " + row.unit + " 초과 추정"
                      : "-";
              const registrationClass = !canEstimate
                ? undefined
                : !row.registeredCoverageComplete || row.registeredExceedsBaseline || row.verdict === "과대"
                  ? "bad"
                  : row.explainPct != null && row.explainPct >= 70
                    ? "good"
                    : row.explainPct != null
                      ? "bad"
                      : undefined;
              return <tr key={row.factory + "-" + row.energyType}>
                <td>
                  <strong>{factoryLabel(row.factory)}</strong>
                  <small>{row.energyLabel} · {canEstimate ? row.currentMeasuredMonths + "개월 비교" : "비교자료 부족"}</small>
                </td>
                <td>
                  <strong>{canEstimate ? fmt(row.previousProductionTon) + " → " + fmt(row.currentProductionTon) + " ton" : "-"}</strong>
                  <small>{canEstimate ? <>전년 대비 <TrendPct value={row.productionChangePct}/></> : "완료월 기준 비교 제외"}</small>
                </td>
                <td>
                  <strong>{canEstimate ? fmt(row.currentUsage) + " " + row.unit : "-"}</strong>
                  <small>{canEstimate ? <>전년 {fmt(row.previousUsage)} · <TrendPct value={row.usageChangePct} lowerIsBetter/></> : "같은 기간의 사용량·생산량 필요"}</small>
                </td>
                <td>
                  <strong>{row.expectedUsage == null ? "-" : fmt(row.expectedUsage) + " " + row.unit}</strong>
                  <small>{!row.registeredCoverageComplete
                    ? "등록 실적 입력 완료 후 반영값 계산"
                    : "등록 실적 반영 후 " + (row.expectedAfterRegistered == null ? "-" : fmt(row.expectedAfterRegistered) + " " + row.unit)}</small>
                </td>
                <td className={estimatedSaving != null && estimatedSaving > 0 ? "good" : excessUsage != null ? "bad" : undefined}>
                  <strong>{estimateText}</strong>
                  <small>{canEstimate ? <>동월·공장 보정 기준 <TrendPct value={row.intensityChangePct} lowerIsBetter/></> : "동일 기간 자료 필요"}</small>
                </td>
                <td className={registrationClass}>
                  <strong>{row.registeredQty == null ? "미입력" : fmt(row.registeredQty) + " " + row.unit}</strong>
                  <small>{!canEstimate
                    ? "비교자료 부족으로 판정 제외"
                    : !row.registeredCoverageComplete
                      ? row.registeredCoverageNote || "계획 월의 등록 실적 입력 필요"
                      : row.registeredQty == null || row.registeredQty <= 0
                        ? "완료월까지 등록 실적 없음"
                        : row.registeredExceedsBaseline
                          ? "등록량이 보정 예상 사용량보다 큼"
                          : row.explainPct == null
                            ? "등록 실적 대비 추정 절감량 -"
                            : "등록 실적 대비 추정 절감량 " + fmt(row.explainPct) + "%"}</small>
                </td>
                <td>
                  <span className={"savings-verdict " + verdictClass[row.verdict]}>{verdictLabels[row.verdict]}</span>
                  {(row.coverageNote || row.registeredCoverageNote) && <small>{row.coverageNote || row.registeredCoverageNote}</small>}
                </td>
              </tr>;
            })}</tbody>
          </table>
        </div> : <div className="cost-empty savings-empty"><p>진행·완료 과제와 생산·사용량 데이터가 함께 있는 비교 대상이 없습니다.</p></div>}
      </article>

      <article className="card table-card span-all savings-theme-list">
        <header className="card-title savings-section-title">
          <div>
            <h3>과제별 연간 계획·누계 실적</h3>
          </div>
          <span>{sortedThemes.length}건</span>
        </header>
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
            <th>상태</th><th>실제 사용량 확인</th>
          </tr></thead>
          <tbody>
            {sortedThemes.map(theme => {
              const annualProgressRate = explicitOrFallback(theme.annualProgressRate, theme.rate);
              const annualPlannedQty = explicitOrFallback(theme.annualPlannedQty, theme.plannedQty);
              const actualYtdQty = explicitOrFallback(theme.actualYtdQty, theme.actualQty);
              return <tr
              key={theme.id}
              className={theme.id === selectedThemeId ? "selected" : ""}
              onClick={() => setSelectedThemeId(theme.id)}
            >
              <td title={theme.title}><button
                type="button"
                className="savings-theme-open"
                ref={node => {
                  if (node) themeRowRefs.current.set(theme.id, node);
                  else themeRowRefs.current.delete(theme.id);
                }}
                aria-controls="savings-theme-detail"
                aria-expanded={theme.id === selectedThemeId}
                onClick={event => {
                  event.stopPropagation();
                  setSelectedThemeId(theme.id);
                }}
              >{theme.title}</button></td>
              <td>{factoryLabel(theme.factory)}</td>
              <td>{fmt(annualPlannedQty)} {theme.unit}</td>
              <td>{actualYtdQty == null ? "-" : fmt(actualYtdQty) + " " + theme.unit}</td>
              <td>{theme.actualAmount == null ? "-" : fmt(theme.actualAmount, 1) + "백만원"}</td>
              <td className={annualProgressRate != null && annualProgressRate >= 100 ? "good" : annualProgressRate != null ? "bad" : undefined}>{annualProgressRate == null ? "-" : fmt(annualProgressRate) + "%"}</td>
              <td><span className={"savings-status " + theme.status}>{theme.statusLabel}</span></td>
              <td><VerificationChip status={theme.verification.status}/></td>
            </tr>;
            })}
            {sortedThemes.length === 0 && <tr><td colSpan={themeColumns.length + 2}>조회 조건에 맞는 절감 과제가 없습니다.</td></tr>}
          </tbody>
        </table></div>
      </article>

      {selectedThemeId != null && <ThemeDetailPanel themeId={selectedThemeId} requestedDate={requestedDate} panelId="savings-theme-detail" onClose={closeThemeDetail}/>}
    </section>
  </div>;
}
