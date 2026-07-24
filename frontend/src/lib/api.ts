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

type UploadRequestOptions = {
  method?: string;
  token?: string | null;
  formData: FormData;
  onProgress?: (progress: UploadProgress) => void;
};

export type UploadProgress = {
  loaded: number;
  total: number;
  percent: number;
};

function extractApiErrorMessage(detail: string, status: number) {
  let message = detail || `Request failed with status ${status}`;
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
  return message;
}


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
    const message = extractApiErrorMessage(detail, response.status);
    throw new Error(message);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}

export function uploadApiRequest<T>(path: string, options: UploadRequestOptions): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(options.method ?? "POST", `${API_URL}${path}`);
    xhr.responseType = "text";

    if (options.token) {
      xhr.setRequestHeader("Authorization", `Bearer ${options.token}`);
    }

    xhr.upload.onprogress = (event) => {
      const total = event.total || 0;
      const loaded = event.loaded || 0;
      const percent = total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : 0;
      options.onProgress?.({ loaded, total, percent });
    };

    xhr.onerror = () => {
      reject(new Error("Failed to fetch"));
    };

    xhr.onabort = () => {
      reject(new Error("Upload aborted"));
    };

    xhr.onload = () => {
      const status = xhr.status;
      const responseText = typeof xhr.responseText === "string" ? xhr.responseText : "";

      if (status < 200 || status >= 300) {
        reject(new Error(extractApiErrorMessage(responseText, status)));
        return;
      }

      if (status === 204 || !responseText.trim()) {
        resolve(undefined as T);
        return;
      }

      try {
        resolve(JSON.parse(responseText) as T);
      } catch {
        reject(new Error("No se pudo interpretar la respuesta del upload."));
      }
    };

    xhr.send(options.formData);
  });
}

export { API_ORIGIN, API_URL };
