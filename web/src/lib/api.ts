// Typed fetch wrapper for the gateway's admin API: JSON in/out, CSRF header on writes, cookies same-origin,
// errors normalized to ApiError (the gateway answers {"error": {message, type, code, param}}).

export const API_BASE = "/admin/api";

export class ApiError extends Error {
  status: number;
  code: string | null;
  type: string | null;
  param: string | null;

  constructor(status: number, message: string, code: string | null = null, type: string | null = null, param: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.type = type;
    this.param = param;
  }
}

/** Build an ApiError from any response body shape the gateway (or a proxy in front of it) may return. */
export function toApiError(status: number, body: unknown, statusText = ""): ApiError {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    const e = b.error;
    if (e && typeof e === "object") {
      const err = e as Record<string, unknown>;
      return new ApiError(
        status,
        String(err.message ?? statusText ?? "error"),
        (err.code as string | null) ?? null,
        (err.type as string | null) ?? null,
        (err.param as string | null) ?? null,
      );
    }
    if (typeof e === "string") return new ApiError(status, e);
    if (typeof b.detail === "string") return new ApiError(status, b.detail);
    if (Array.isArray(b.detail) && b.detail.length) {
      const d = b.detail[0] as { msg?: string; loc?: unknown[] };
      return new ApiError(status, `${(d.loc ?? []).join(".")}: ${d.msg ?? "invalid"}`);
    }
    if (typeof b.message === "string") return new ApiError(status, b.message);
  }
  if (typeof body === "string" && body.trim()) return new ApiError(status, body.trim().slice(0, 300));
  return new ApiError(status, statusText || (status ? `HTTP ${status}` : "network error"));
}

let csrfToken: string | null = null;
export const setCsrf = (t: string | null) => {
  csrfToken = t;
};
export const getCsrf = () => csrfToken;

type AuthListener = (err: ApiError) => void;
let authListener: AuthListener | null = null;
/** Called on 401 (session gone) and 403 must_change_password, so the app can switch screens. */
export const onAuthError = (fn: AuthListener | null) => {
  authListener = fn;
};

export interface RequestOptions {
  query?: Record<string, string | number | boolean | null | undefined>;
  body?: unknown;
  form?: FormData;
  signal?: AbortSignal;
  /** return the raw Response (for downloads) */
  raw?: boolean;
  /** do not notify the auth listener (used by the session probe itself) */
  quiet?: boolean;
  fetchImpl?: typeof fetch;
}

export function buildUrl(path: string, query?: RequestOptions["query"]): string {
  let url = path.startsWith("/") ? path : `${API_BASE}/${path}`;
  if (query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    const s = qs.toString();
    if (s) url += (url.includes("?") ? "&" : "?") + s;
  }
  return url;
}

export async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  let body: BodyInit | undefined;
  if (opts.form) body = opts.form;
  else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  if (method !== "GET" && method !== "HEAD" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const f = opts.fetchImpl ?? fetch;
  let res: Response;
  try {
    res = await f(buildUrl(path, opts.query), { method, headers, body, credentials: "same-origin", signal: opts.signal });
  } catch (e) {
    if ((e as Error)?.name === "AbortError") throw e;
    throw new ApiError(0, "Không kết nối được tới gateway");
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    let parsed: unknown = text;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch {
      /* not JSON */
    }
    const err = toApiError(res.status, parsed, res.statusText);
    if (!opts.quiet && authListener && (err.status === 401 || (err.status === 403 && err.code === "must_change_password"))) {
      authListener(err);
    }
    throw err;
  }
  if (opts.raw) return res as unknown as T;
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const api = {
  get: <T>(path: string, opts?: RequestOptions) => request<T>("GET", path, opts),
  post: <T>(path: string, body?: unknown, opts?: RequestOptions) => request<T>("POST", path, { ...opts, body }),
  put: <T>(path: string, body?: unknown, opts?: RequestOptions) => request<T>("PUT", path, { ...opts, body }),
  patch: <T>(path: string, body?: unknown, opts?: RequestOptions) => request<T>("PATCH", path, { ...opts, body }),
  del: <T>(path: string, opts?: RequestOptions) => request<T>("DELETE", path, opts),
  upload: <T>(path: string, form: FormData, opts?: RequestOptions) => request<T>("POST", path, { ...opts, form }),
};

/** POST that returns a file (e.g. the DB backup): fetch with the CSRF header, then save the blob. */
export async function downloadPost(path: string, fallbackName: string): Promise<string> {
  const res = await request<Response>("POST", path, { raw: true });
  const cd = res.headers.get("content-disposition") ?? "";
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
  const name = m ? decodeURIComponent(m[1]) : fallbackName;
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
  return name;
}

export function errMsg(e: unknown): string {
  if (!e) return "";
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return String(e);
}
