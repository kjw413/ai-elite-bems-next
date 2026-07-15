const normalizeBase = (value: string) => value.trim().replace(/\/+$/, "");

function getApiBase() {
  const configured = process.env.NEXT_PUBLIC_BEMS_API_BASE?.trim();
  if (configured) return normalizeBase(configured);

  // 이전 환경변수로 배포한 환경도 계속 동작하게 유지합니다.
  const legacyConfigured = process.env.NEXT_PUBLIC_BEMS_API_URL?.trim();
  if (legacyConfigured) return normalizeBase(legacyConfigured);

  if (typeof window !== "undefined") {
    const protocol = window.location.protocol === "https:" ? "https:" : "http:";
    const hostname = window.location.hostname;
    const host = hostname.includes(":") && !hostname.startsWith("[") ? `[${hostname}]` : hostname;
    return `${protocol}//${host}:8000/api/v1`;
  }

  return "http://localhost:8000/api/v1";
}

export async function apiGet<T>(path: string, fallback: T, signal?: AbortSignal): Promise<{ data: T; live: boolean }> {
  try {
    const normalizedPath = path.startsWith("/") ? path : `/${path}`;
    const response = await fetch(`${getApiBase()}${normalizedPath}`, { cache: "no-store", signal });
    if (!response.ok) throw new Error(`API ${response.status}`);
    return { data: (await response.json()) as T, live: true };
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    return { data: fallback, live: false };
  }
}

export function query(params: Record<string, string>) {
  return new URLSearchParams(params).toString();
}
