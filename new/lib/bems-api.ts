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

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === "AbortError";
}

function errorMessage(payload: unknown, fallback: string) {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail?: unknown }).detail;
    return typeof detail === "string" ? detail : JSON.stringify(detail) || fallback;
  }
  return fallback;
}

export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${getApiBase()}${normalizedPath}`, {
    cache: "no-store",
    ...init,
    headers,
  });
  const contentType = response.headers.get("content-type") ?? "";
  const payload: unknown = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    throw new ApiError(response.status, errorMessage(payload, `API ${response.status}`));
  }
  return payload as T;
}

export async function apiGet<T>(path: string, fallback: T, signal?: AbortSignal): Promise<{ data: T; live: boolean }> {
  try {
    return { data: await apiRequest<T>(path, { signal }), live: true };
  } catch (error) {
    if (isAbortError(error)) throw error;
    return { data: fallback, live: false };
  }
}

export function query(params: Record<string, string>) {
  return new URLSearchParams(params).toString();
}
