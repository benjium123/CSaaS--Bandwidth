/**
 * The one place that talks HTTP.
 *
 * Attaches the P0 auth contract (Bearer + X-Org-Id) to every call, and turns any 401 into
 * a single "you are logged out" signal so no page has to handle it individually.
 */

import { deviceId } from "@/lib/device";

export type AuthState = {
  token: string | null;
  orgId: string | null;
};

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    /** P23a: the whole parsed `error` object from the body. A 422 often carries MORE than a
     * message - e.g. the list of things an assistant is still missing before it can go live -
     * and flattening it to a string here would throw that away before any page could read it. */
    readonly details?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface ApiClient {
  request<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T>;
  auth: AuthState;
  setAuth(next: Partial<AuthState>): void;
  onUnauthorized?: () => void;
  /** P41: fired when the API answers step_up_required - the step-up dialog listens here. */
  onStepUpRequired?: (details: { kind: string; action: string; message: string }) => void;
}

const STORAGE_KEY = "csaas.auth";

export function loadStoredAuth(): AuthState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { token: null, orgId: null };
    const parsed = JSON.parse(raw) as AuthState;
    return { token: parsed.token ?? null, orgId: parsed.orgId ?? null };
  } catch {
    return { token: null, orgId: null };
  }
}

export function storeAuth(state: AuthState): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    /* private mode - the session simply will not persist */
  }
}

export function clearStoredAuth(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

/** P42: the CSRF token the server issued with the session cookie (readable on purpose). */
export function csrfToken(): string | null {
  if (typeof document === "undefined") return null;
  for (const part of document.cookie.split(";")) {
    const [rawName, ...rest] = part.trim().split("=");
    if (rawName === "__Host-csaas_csrf" || rawName === "csaas_csrf") {
      return decodeURIComponent(rest.join("="));
    }
  }
  return null;
}

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/** Headers every authenticated call needs: org, device, and CSRF for unsafe methods. */
export function authHeaders(api: ApiClient, method = "GET", base?: HeadersInit): Headers {
  const headers = new Headers(base);
  if (api.auth.token) headers.set("Authorization", `Bearer ${api.auth.token}`);
  if (api.auth.orgId) headers.set("X-Org-Id", api.auth.orgId);
  headers.set("X-Device-Id", deviceId());
  if (!SAFE_METHODS.has(method.toUpperCase())) {
    const csrf = csrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }
  return headers;
}

export function createClient(baseUrl = ""): ApiClient {
  const client: ApiClient = {
    auth: loadStoredAuth(),

    setAuth(next) {
      client.auth = { ...client.auth, ...next };
      storeAuth(client.auth);
    },

    async request<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
      // P42: the session is an HttpOnly cookie the browser sends itself; scripts never see it.
      const headers = authHeaders(client, init.method ?? "GET", init.headers);

      let body = init.body;
      if (init.json !== undefined) {
        headers.set("Content-Type", "application/json");
        body = JSON.stringify(init.json);
      }

      const res = await fetch(`${baseUrl}${path}`, {
        ...init,
        headers,
        body,
        credentials: "same-origin",
      });

      if (res.status === 401) {
        clearStoredAuth();
        client.auth = { token: null, orgId: null };
        client.onUnauthorized?.();
      }

      if (res.status === 204) return undefined as T;

      const text = await res.text();
      const payload = text ? JSON.parse(text) : null;

      if (!res.ok) {
        const err = payload?.error;
        if (
          (err?.code === "step_up_required" || err?.code === "passkey_required") &&
          client.onStepUpRequired
        ) {
          client.onStepUpRequired({
            kind: err.code === "passkey_required" ? "passkey_session" : String(err.kind ?? "recent_2fa"),
            action: String(err.action ?? ""),
            message: String(err.message ?? ""),
          });
        }
        throw new ApiError(
          res.status,
          err?.code ?? "http_error",
          err?.message ?? `Request failed with ${res.status}`,
          err,
        );
      }
      return payload as T;
    },
  };
  return client;
}

/**
 * Item 41: recording playback needs the raw audio bytes, not JSON - `ApiClient.request`
 * always parses the body as JSON, so it can't be reused as-is for this. This is the one
 * other place allowed to build auth headers by hand, so both CallsPage's RecordingRow and
 * Timeline's CallRecordingPlayer go through it instead of each reimplementing the same
 * Authorization/X-Org-Id header logic.
 */
export async function fetchAuthedBlob(api: ApiClient, path: string): Promise<Blob> {
  const headers = authHeaders(api, "GET");
  const res = await fetch(path, { headers, credentials: "same-origin" });
  if (!res.ok) throw new Error(`Failed to load recording (${res.status})`);
  return res.blob();
}

/**
 * P23b: the voice sample is the first endpoint that takes a JSON body and answers with
 * audio, which neither `request` (always parses JSON) nor `fetchAuthedBlob` (GET only) can
 * do. Additive on purpose — no existing caller changes.
 *
 * A failure body IS json, so it is read for the server's own sentence before falling back
 * to the status code; a customer pressing "Preview voice" with no voice connected should be
 * told that, not "Request failed with 422".
 */
export async function postAuthedBlob(
  api: ApiClient,
  path: string,
  json: unknown,
): Promise<Blob> {
  const headers = authHeaders(api, "POST", { "Content-Type": "application/json" });
  const res = await fetch(path, {
    method: "POST",
    headers,
    body: JSON.stringify(json),
    credentials: "same-origin",
  });
  if (!res.ok) {
    let message = `Request failed with ${res.status}`;
    try {
      const payload = JSON.parse(await res.text()) as {
        error?: { message?: string };
        detail?: unknown;
        message?: unknown;
      };
      const candidate =
        payload?.error?.message ??
        (typeof payload?.detail === "string" ? payload.detail : undefined) ??
        (typeof payload?.message === "string" ? payload.message : undefined);
      if (candidate) message = candidate;
    } catch {
      /* a non-JSON error body is not worth failing over - keep the status sentence */
    }
    throw new ApiError(res.status, "http_error", message);
  }
  return res.blob();
}
