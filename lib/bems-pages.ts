import { BarChart3, Bolt, BrainCircuit, FileText, Gauge, PackageCheck, Settings, type LucideIcon } from "lucide-react";

// 사이드바 메뉴 = 노출 설정 가능한 전체 페이지 목록의 단일 소스.
// 관리자 전용 메뉴의 "페이지 노출 설정" 탭과 BemsApp 사이드바가 이 정의를 함께 쓴다 —
// 라벨이 두 곳에서 따로 관리되며 어긋나는 것을 막기 위함(2026-07).
export type PageId = "dashboard" | "energy" | "intensity" | "production" | "prediction" | "report" | "admin";

export const PAGE_DEFS: { id: PageId; label: string; icon: LucideIcon }[] = [
  { id: "dashboard", label: "통합 대시보드", icon: BarChart3 },
  { id: "energy", label: "에너지 사용량", icon: Bolt },
  { id: "intensity", label: "에너지 원단위", icon: Gauge },
  { id: "production", label: "생산실적 분석", icon: PackageCheck },
  { id: "prediction", label: "AI 에너지 예측", icon: BrainCircuit },
  { id: "report", label: "AI 실적 보고서", icon: FileText },
  { id: "admin", label: "관리자 전용 메뉴", icon: Settings },
];

// API 미연결 시 폴백 — 전부 노출(fail-open). 페이지 숨김 설정을 못 불러온 상태에서
// 조회 사용자 메뉴가 함부로 사라지는 것보다는, 모두 보이는 편이 안전하다.
export const DEFAULT_PAGE_VISIBILITY: Record<PageId, boolean> = PAGE_DEFS.reduce(
  (acc, item) => ({ ...acc, [item.id]: true }),
  {} as Record<PageId, boolean>,
);
