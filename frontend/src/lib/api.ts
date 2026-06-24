function normalizeApiBaseUrl(rawUrl: string | undefined) {
  if (!rawUrl) {
    return null;
  }

  const trimmed = rawUrl.trim();
  if (!trimmed) {
    return null;
  }

  const normalized = trimmed.replace(/\/+$/, "");
  return normalized.endsWith("/api") ? normalized : `${normalized}/api`;
}

function inferApiBaseUrl() {
  if (typeof window === "undefined") {
    return "http://localhost:8000/api";
  }

  const protocol = window.location.protocol || "http:";
  const hostname = window.location.hostname || "localhost";
  return `${protocol}//${hostname}:8000/api`;
}

const API_URL =
  normalizeApiBaseUrl(import.meta.env.VITE_API_BASE_URL) ??
  normalizeApiBaseUrl(import.meta.env.VITE_API_URL) ??
  inferApiBaseUrl();

const API_ORIGIN = API_URL.replace(/\/api\/?$/, "");


type RequestOptions = {
  method?: string;
  token?: string | null;
  body?: unknown;
  formData?: FormData;
};


export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers();
  if (options.token) {
    headers.set("Authorization", `Bearer ${options.token}`);
  }

  const isForm = Boolean(options.formData);
  if (!isForm) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_URL}${path}`, {
    method: options.method ?? "GET",
    headers,
    body: isForm ? options.formData : options.body ? JSON.stringify(options.body) : undefined,
  });

  if (!response.ok) {
    const detail = await response.text();
    let message = detail || `Request failed with status ${response.status}`;
    try {
      const parsed = detail ? JSON.parse(detail) : null;
      if (typeof parsed?.detail === "string" && parsed.detail.trim()) {
        message = parsed.detail;
      } else if (typeof parsed?.message === "string" && parsed.message.trim()) {
        message = parsed.message;
      }
    } catch {
      // Keep original text when the backend returned plain text or HTML.
    }
    throw new Error(message);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}

export { API_ORIGIN, API_URL };
