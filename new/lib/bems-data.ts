export const factories = ["전사", "남양주", "김해", "광주", "대전", "경산"];

export const demo = {
  session: { role: "viewer", clientIp: "demo", serverName: "BEMS-DEMO" },
  dashboard: {
    baseDate: "2026-07-15", factory: "전사", updatedAt: "2026-07-15T17:30:00",
    alert: { level: "normal", title: "AI 이상 신호 없음", description: "최근 7일 예측 밴드 기준입니다.", count: 0 },
    metrics: [
      { id: "power", label: "전력 원단위", value: 412.8, unit: "kWh/ton", change: -3.2, tone: "blue" },
      { id: "fuel", label: "연료 원단위", value: 28.4, unit: "Nm³/ton", change: -1.1, tone: "violet" },
      { id: "water", label: "용수 원단위", value: 3.17, unit: "ton/ton", change: 0.8, tone: "cyan" },
      { id: "production", label: "누계 생산량", value: 18420, unit: "ton", change: 4.6, tone: "emerald" },
    ],
    trend: [
      { date: "07.09", actual: 191, predicted: 186, lower: 174, upper: 199 }, { date: "07.10", actual: 184, predicted: 188, lower: 175, upper: 201 },
      { date: "07.11", actual: 201, predicted: 196, lower: 183, upper: 209 }, { date: "07.12", actual: 173, predicted: 178, lower: 166, upper: 190 },
      { date: "07.13", actual: 169, predicted: 171, lower: 160, upper: 184 }, { date: "07.14", actual: 205, predicted: 199, lower: 186, upper: 213 },
      { date: "07.15", actual: 198, predicted: 202, lower: 189, upper: 216 },
    ],
    yoy: [{ month: "2월", current: 432, previous: 448 }, { month: "3월", current: 426, previous: 441 }, { month: "4월", current: 421, previous: 436 }, { month: "5월", current: 417, previous: 429 }, { month: "6월", current: 414, previous: 425 }, { month: "7월", current: 413, previous: 426 }],
    factoryComparison: [{ factory: "남양주", value: 398, change: -4.1 }, { factory: "김해", value: 424, change: -2.2 }, { factory: "광주", value: 437, change: 1.3 }, { factory: "대전", value: 409, change: -3.4 }],
    events: [{ id: 1, date: "07.14", factory: "남양주", tag: "정비", note: "냉동기 정기점검 완료" }, { id: 2, date: "07.12", factory: "김해", tag: "생산", note: "주말 증산 대응" }],
  },
  energy: {
    daily: Array.from({ length: 14 }, (_, i) => ({ date: `07.${String(i + 2).padStart(2, "0")}`, power: 170 + ((i * 17) % 42), fuel: 18 + ((i * 3) % 9), water: 12 + ((i * 5) % 8), wastewater: 7 + ((i * 2) % 5) })),
    equipment: [{ name: "냉동", value: 38 }, { name: "공압", value: 17 }, { name: "생산설비·기타", value: 45 }],
    factories: [{ factory: "남양주", power: 920, fuel: 132, water: 86 }, { factory: "김해", power: 710, fuel: 104, water: 64 }, { factory: "광주", power: 540, fuel: 82, water: 51 }, { factory: "대전", power: 610, fuel: 91, water: 57 }],
  },
  intensity: {
    metric: "power", unit: "kWh/ton", year: 2026, targetPct: 3,
    summary: { mtd: { current: 412.8, previous: 426.4, change: -3.2 }, ytd: { current: 419.6, previous: 433.1, change: -3.1 } },
    monthly: Array.from({ length: 12 }, (_, i) => ({ month: `${i + 1}월`, current: i < 7 ? 438 - i * 4 : null, previous: 451 - i * 4, target: 437 - i * 4 })),
    matrix: [{ factory: "남양주", current: 398, previous: 415, change: -4.1 }, { factory: "김해", current: 424, previous: 434, change: -2.2 }, { factory: "광주", current: 437, previous: 431, change: 1.3 }, { factory: "대전", current: 409, previous: 424, change: -3.4 }],
  },
  production: {
    summary: { plan: 20500, actual: 18420, progress: 89.9, pace: 104.5, forecast: 21400, items: 82 },
    daily: Array.from({ length: 14 }, (_, i) => ({ date: `07.${String(i + 2).padStart(2, "0")}`, IC: 380 + (i % 3) * 30, MY: 300 + (i % 4) * 22, FM: 210 + (i % 2) * 35, SN: 160 + (i % 5) * 18 })),
    mix: [{ name: "IC", value: 35 }, { name: "MY", value: 29 }, { name: "FM", value: 21 }, { name: "SN", value: 15 }],
    topItems: [{ name: "바나나맛우유", plan: 2400, actual: 2310, rate: 96.3 }, { name: "요플레", plan: 2100, actual: 1980, rate: 94.3 }, { name: "메로나", plan: 1800, actual: 1735, rate: 96.4 }],
  },
  predictions: {
    status: { normal: 31, warning: 0, alert: 2, label: "주의" }, model: { version: "v5.3", trainedAt: "2026-06-25", mape: 0, coverage: 0, state: "운영 중" },
    latest: [{ date: "2026-07-15", target: "전력", predicted: 202, lower: 189, upper: 216, actual: 198, status: "inside" }, { date: "2026-07-15", target: "연료", predicted: 26.4, lower: 23.8, upper: 29.1, actual: 27.0, status: "inside" }, { date: "2026-07-14", target: "전력", predicted: 199, lower: 186, upper: 213, actual: 218, status: "over" }],
  },
};
