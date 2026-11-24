// Thin client for the hub's /api/v1 routes.
//
// * Credentials: the session is an HttpOnly cookie the browser manages; the CSRF token
//   lives only in this module's memory (never localStorage/sessionStorage/URL).
// * Every mutation sends the CSRF header; fetch with credentials "same-origin" sends
//   Origin automatically for POSTs, which the hub requires.
// * Errors are normalised into ApiError with a kind the UI can render consistently.

export const API = "/api/v1";
export const CSRF_HEADER = "x-prometheos-csrf";

let csrfToken: string | null = null;

export function setCsrf(token: string | null): void {
  csrfToken = token;
}

export function hasCsrf(): boolean {
  return csrfToken !== null;
}

export type ErrorKind =
  | "offline"
  | "unpaired"
  | "denied"
  | "not_found"
  | "stale"
  | "validation"
  | "unsupported"
  | "busy"
  | "bad_host"
  | "aborted"
  | "server";

export class ApiError extends Error {
  readonly kind: ErrorKind;
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly details: Record<string, unknown>;

  constructor(
    kind: ErrorKind,
    message: string,
    status = 0,
    code: string = kind,
    retryable = false,
    details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.details = details;
  }
}

function kindForStatus(status: number): ErrorKind {
  switch (status) {
    case 400:
      return "validation";
    case 401:
      return "unpaired";
    case 403:
      return "denied";
    case 404:
      return "not_found";
    case 409:
      return "stale";
    case 421:
      return "bad_host";
    case 422:
      return "unsupported";
    case 503:
      return "busy";
    default:
      return "server";
  }
}

type Listener = (event: "unpaired" | "offline" | "online") => void;
const listeners = new Set<Listener>();

export function onConnectionEvent(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function emit(event: "unpaired" | "offline" | "online"): void {
  for (const listener of listeners) listener(event);
}

export interface RequestOptions {
  signal?: AbortSignal;
  /** Do not broadcast 401 (used by the session probe itself). */
  quietUnpaired?: boolean;
  /** Receives the response headers of a successful reply (the run watch's count). */
  onHeaders?: (headers: Headers) => void;
}

export async function request<T>(
  method: "GET" | "POST" | "PUT" | "DELETE",
  path: string,
  body?: unknown,
  options: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = { accept: "application/json" };
  if (method !== "GET") {
    headers["content-type"] = "application/json";
    if (csrfToken) headers[CSRF_HEADER] = csrfToken;
  }
  let response: Response;
  try {
    response = await fetch(API + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (exc) {
    if (options.signal?.aborted) {
      throw new ApiError("aborted", "Request cancelled.");
    }
    emit("offline");
    throw new ApiError(
      "offline",
      "The hub cannot be reached. Check that `prometheos hub` is still running.",
      0,
      "offline",
      true,
      { cause: String(exc) },
    );
  }
  emit("online");
  const text = await response.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
    }
  }
  if (!response.ok) {
    const envelope =
      parsed && typeof parsed === "object" && "error" in parsed
        ? (parsed as { error: Record<string, unknown> }).error
        : null;
    const kind = kindForStatus(response.status);
    const err = new ApiError(
      kind,
      typeof envelope?.message === "string" ? envelope.message : `HTTP ${response.status}`,
      response.status,
      typeof envelope?.code === "string" ? envelope.code : kind,
      envelope?.retryable === true,
      (envelope?.details as Record<string, unknown> | undefined) ?? {},
    );
    if (kind === "unpaired" && !options.quietUnpaired) {
      csrfToken = null;
      emit("unpaired");
    }
    throw err;
  }
  options.onHeaders?.(response.headers);
  return parsed as T;
}

export const get = <T>(path: string, options?: RequestOptions) =>
  request<T>("GET", path, undefined, options);
export const post = <T>(path: string, body: unknown, options?: RequestOptions) =>
  request<T>("POST", path, body, options);
export const put = <T>(path: string, body: unknown, options?: RequestOptions) =>
  request<T>("PUT", path, body, options);
export const del = <T>(path: string, options?: RequestOptions) =>
  request<T>("DELETE", path, undefined, options);

const ERROR_KINDS: Record<string, ErrorKind> = {
  validation_error: "validation",
  not_found: "not_found",
  revision_conflict: "stale",
  stale_task: "stale",
  policy_denied: "denied",
  unsupported: "unsupported",
  budget_exceeded: "unsupported",
  busy: "busy",
  source_unavailable: "busy",
  io_error: "server",
};

interface RunLike {
  run_id: string;
  state: string;
  result: Record<string, unknown> | null;
  error: { code: string; message: string; retryable: boolean; details: Record<string, unknown> } | null;
}

/**
 * A long operation answers an operation run at once (A02, CP10.3; decision 0017). The page
 * waits for it like any other request: the server waits up to 20 s, then the run is polled
 * until it ends. A completed run resolves with its result (and run_id); any other end is an
 * ApiError carrying the run's error, so pages keep their existing error handling.
 */
export async function runOperation<T>(path: string, body: Record<string, unknown>): Promise<T & { run_id: string }> {
  let run = await post<RunLike>(path, { ...body, wait_seconds: 20 });
  while (run.state === "queued" || run.state === "running") {
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
    run = await get<RunLike>(`/runs/${encodeURIComponent(run.run_id)}`);
  }
  if (run.state === "completed" && run.result) return { ...(run.result as T), run_id: run.run_id };
  const error = run.error;
  const kind = error ? (ERROR_KINDS[error.code] ?? "server") : "server";
  throw new ApiError(
    kind,
    error?.message ?? `The run ended as ${run.state.replace(/_/g, " ")}; see Runs (${run.run_id}).`,
    0,
    error?.code ?? run.state,
    error?.retryable ?? false,
    { ...(error?.details ?? {}), run_id: run.run_id, run_state: run.state },
  );
}

/** Lowercase UUID4 from the browser's CSPRNG (127.0.0.1 is a secure context). */
export function uuid4(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID().toLowerCase();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40;
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function q(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export function describeError(err: unknown): { title: string; detail: string } {
  if (!(err instanceof ApiError)) {
    return { title: "Something went wrong", detail: String(err) };
  }
  switch (err.kind) {
    case "offline":
      return {
        title: "Offline",
        detail:
          "The hub cannot be reached. Drafts on this page are kept; the view retries automatically.",
      };
    case "unpaired":
      return { title: "Not paired", detail: "Pair this browser with the hub to continue." };
    case "denied":
      return { title: "Not permitted", detail: err.message };
    case "not_found":
      return { title: "Not found", detail: "This record does not exist in this project." };
    case "stale":
      return {
        title: "Changed since you loaded it",
        detail: `${err.message}. Your draft is kept; refresh and review before trying again.`,
      };
    case "unsupported":
      return { title: "Not available yet", detail: err.message };
    case "busy":
      return { title: "Busy or unavailable", detail: `${err.message}. Try again shortly.` };
    case "validation":
      return { title: "Not accepted", detail: err.message };
    case "bad_host":
      return { title: "Unexpected address", detail: "Open the hub at the address the terminal printed." };
    default:
      return { title: "Server error", detail: err.message };
  }
}
