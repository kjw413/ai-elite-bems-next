const API_BASE = process.env.NEXT_PUBLIC_BEMS_API_URL ?? "http://localhost:8000/api/v1";

export async function apiGet<T>(path: string, fallback: T, signal?: AbortSignal): Promise<{ data: T; live: boolean }> {
  try {
    const response = await fetch(`${API_BASE}${path}`, { cache: "no-store", signal });
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
