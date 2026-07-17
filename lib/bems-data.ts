export const factories = ["전사", "남양주", "남양주1", "남양주2", "김해", "광주", "논산", "경산"];

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
      { date: "07.09", actual: 191, predicted: 186, lower: 174, upper: 199, production: 512, fuel: 1180, water: 1620, wastewater: 940 },
      { date: "07.10", actual: 184, predicted: 188, lower: 175, upper: 201, production: 498, fuel: 1150, water: 1580, wastewater: 910 },
      { date: "07.11", actual: 201, predicted: 196, lower: 183, upper: 209, production: 545, fuel: 1230, water: 1710, wastewater: 990 },
      { date: "07.12", actual: 173, predicted: 178, lower: 166, upper: 190, production: 471, fuel: 1090, water: 1490, wastewater: 860 },
      { date: "07.13", actual: 169, predicted: 171, lower: 160, upper: 184, production: 448, fuel: 1250, water: 1450, wastewater: 840 },
      { date: "07.14", actual: 205, predicted: 199, lower: 186, upper: 213, production: 553, fuel: 1260, water: 1740, wastewater: 1010 },
      { date: "07.15", actual: 198, predicted: 202, lower: 189, upper: 216, production: 536, fuel: 1210, water: 1690, wastewater: 980 },
    ],
    yoy: [{ month: "2월", current: 432, previous: 448 }, { month: "3월", current: 426, previous: 441 }, { month: "4월", current: 421, previous: 436 }, { month: "5월", current: 417, previous: 429 }, { month: "6월", current: 414, previous: 425 }, { month: "7월", current: 413, previous: 426 }],
    factoryComparison: [{ factory: "남양주", value: 398, change: -4.1 }, { factory: "김해", value: 424, change: -2.2 }, { factory: "광주", value: 437, change: 1.3 }, { factory: "논산", value: 409, change: -3.4 }, { factory: "경산", value: 402, change: -2.7 }],
    yoyPeriod: { currentFrom: "2026-07-01", currentTo: "2026-07-15", previousFrom: "2025-07-01", previousTo: "2025-07-15" },
    yoyFactories: [
      { factory: "남양주", intensity: { power: { current: 398, previous: 415 }, fuel: { current: 27.1, previous: 27.9 }, water: { current: 3.02, previous: 3.1 }, wwratio: { current: 0.58, previous: 0.61 } }, usage: { power: { current: 920, previous: 958 }, fuel: { current: 62800, previous: 64100 }, water: { current: 6980, previous: 7150 }, wastewater: { current: 4050, previous: 4360 } }, production: { current: 2312, previous: 2309 } },
      { factory: "김해", intensity: { power: { current: 424, previous: 434 }, fuel: { current: 29.3, previous: 28.7 }, water: { current: 3.24, previous: 3.31 }, wwratio: { current: 0.62, previous: 0.6 } }, usage: { power: { current: 710, previous: 731 }, fuel: { current: 49100, previous: 48300 }, water: { current: 5420, previous: 5570 }, wastewater: { current: 3360, previous: 3340 } }, production: { current: 1675, previous: 1684 } },
      { factory: "광주", intensity: { power: { current: 437, previous: 431 }, fuel: { current: 30.2, previous: 29.5 }, water: { current: 3.4, previous: 3.28 }, wwratio: { current: 0.66, previous: 0.63 } }, usage: { power: { current: 540, previous: 522 }, fuel: { current: 37300, previous: 35700 }, water: { current: 4200, previous: 3970 }, wastewater: { current: 2770, previous: 2500 } }, production: { current: 1236, previous: 1211 } },
      { factory: "논산", intensity: { power: { current: 409, previous: 424 }, fuel: { current: 28, previous: 28.9 }, water: { current: 3.1, previous: 3.22 }, wwratio: { current: 0.57, previous: 0.59 } }, usage: { power: { current: 610, previous: 645 }, fuel: { current: 41700, previous: 43900 }, water: { current: 4620, previous: 4890 }, wastewater: { current: 2630, previous: 2890 } }, production: { current: 1491, previous: 1521 } },
      { factory: "경산", intensity: { power: { current: 402, previous: 413 }, fuel: { current: 27.6, previous: 28.2 }, water: { current: 3.05, previous: 3.12 }, wwratio: { current: 0.55, previous: 0.57 } }, usage: { power: { current: 430, previous: 452 }, fuel: { current: 29500, previous: 30800 }, water: { current: 3260, previous: 3410 }, wastewater: { current: 1790, previous: 1940 } }, production: { current: 1069, previous: 1094 } },
    ],
    events: [{ id: 1, date: "07.14", factory: "남양주", tag: "정비", note: "냉동기 정기점검 완료" }, { id: 2, date: "07.12", factory: "김해", tag: "생산", note: "주말 증산 대응" }],
  },
  energy: {
    mode: "recent", dateFrom: "2026-06-16", dateTo: "2026-07-15", yoyYear: 2026,
    daily: Array.from({ length: 14 }, (_, i) => {
      const power = 170 + ((i * 17) % 42);
      const freezing = Math.round(power * 0.38 * 10) / 10;
      const compressor = Math.round(power * 0.17 * 10) / 10;
      return { date: `07.${String(i + 2).padStart(2, "0")}`, power, freezing, compressor,
        other: Math.round((power - freezing - compressor) * 10) / 10,
        fuel: 18 + ((i * 3) % 9), water: 12 + ((i * 5) % 8), wastewater: 7 + ((i * 2) % 5) };
    }),
    equipment: [{ name: "냉동", value: 38 }, { name: "공압", value: 17 }, { name: "생산설비·기타", value: 45 }],
    factories: [{ factory: "남양주", power: 920, fuel: 132, water: 86, wastewater: 48 }, { factory: "김해", power: 710, fuel: 104, water: 64, wastewater: 39 }, { factory: "광주", power: 540, fuel: 82, water: 51, wastewater: 31 }, { factory: "논산", power: 610, fuel: 91, water: 57, wastewater: 34 }, { factory: "경산", power: 430, fuel: 68, water: 43, wastewater: 26 }],
    yoy: Array.from({ length: 12 }, (_, i) => {
      const month = i + 1;
      const hasCurrent = month <= 7;
      const scale = 1 + 0.18 * Math.sin((month - 1) / 11 * Math.PI);
      const round1 = (value: number) => Math.round(value * 10) / 10;
      return {
        month: `${month}월`,
        power: { current: hasCurrent ? round1(5200 * scale) : null, previous: round1(5480 * scale) },
        fuel: { current: hasCurrent ? round1(610 * scale) : null, previous: round1(648 * scale) },
        water: { current: hasCurrent ? round1(420 * scale) : null, previous: round1(431 * scale) },
        wastewater: { current: hasCurrent ? round1(245 * scale) : null, previous: round1(252 * scale) },
      };
    }),
  },
  intensity: {
    metric: "power", unit: "kWh/ton", year: 2026, targetPct: 3,
    mode: "recent", dateFrom: "2026-06-16", dateTo: "2026-07-15",
    daily: Array.from({ length: 30 }, (_, i) => {
      const day = new Date(Date.parse("2026-06-16T00:00:00") + i * 86_400_000);
      const weekend = day.getDay() === 0 || day.getDay() === 6;
      return { date: `${String(day.getMonth() + 1).padStart(2, "0")}.${String(day.getDate()).padStart(2, "0")}`,
        value: weekend ? null : Math.round((408 + 22 * Math.sin(i / 4) + (i % 5) * 3) * 100) / 100 };
    }),
    yoyCumulative: { months: 7, lastMonth: 7, current: 419.6, previous: 433.1, change: -3.1 },
    summary: { mtd: { current: 412.8, previous: 426.4, change: -3.2 }, ytd: { current: 419.6, previous: 433.1, change: -3.1 } },
    monthly: Array.from({ length: 12 }, (_, i) => ({ month: `${i + 1}월`, current: i < 7 ? 438 - i * 4 : null, previous: 451 - i * 4, target: 437 - i * 4 })),
    matrix: [{ factory: "남양주", current: 398, previous: 415, change: -4.1 }, { factory: "김해", current: 424, previous: 434, change: -2.2 }, { factory: "광주", current: 437, previous: 431, change: 1.3 }, { factory: "논산", current: 409, previous: 424, change: -3.4 }, { factory: "경산", current: 402, previous: 413, change: -2.7 }],
  },
  production: {
    mode: "month", dateFrom: "2026-07-01", dateTo: "2026-07-15", planAllowed: true,
    summary: { plan: 20500, actual: 18420, progress: 89.9, pace: 104.5, forecast: 21400, items: 82, days: 15 },
    daily: Array.from({ length: 14 }, (_, i) => ({ date: `07.${String(i + 2).padStart(2, "0")}`, IC: 380 + (i % 3) * 30, MY: 300 + (i % 4) * 22, FM: 210 + (i % 2) * 35, SN: 160 + (i % 5) * 18, ETC: 20 + (i % 3) * 4 })),
    burnup: [],
    mix: [{ name: "IC", value: 35 }, { name: "MY", value: 29 }, { name: "FM", value: 21 }, { name: "SN", value: 15 }],
    topItems: [{ name: "바나나맛우유", plan: 2400, actual: 2310, rate: 96.3 }, { name: "요플레", plan: 2100, actual: 1980, rate: 94.3 }, { name: "메로나", plan: 1800, actual: 1735, rate: 96.4 }],
  },
  predictions: {
    status: { normal: 31, warning: 0, alert: 2, label: "주의" }, model: { version: "v5.3", trainedAt: "2026-06-25", mape: 0, coverage: 0, state: "운영 중" },
    latest: [{ date: "2026-07-15", target: "전력", predicted: 202, lower: 189, upper: 216, actual: 198, status: "inside" }, { date: "2026-07-15", target: "연료", predicted: 26.4, lower: 23.8, upper: 29.1, actual: 27.0, status: "inside" }, { date: "2026-07-14", target: "전력", predicted: 199, lower: 186, upper: 213, actual: 218, status: "over" }],
  },
};
