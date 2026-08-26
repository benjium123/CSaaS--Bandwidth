import { describe, expect, it, vi, beforeEach } from "vitest";
import { createClient, loadStoredAuth, storeAuth, ApiError } from "./client";

function mockFetch(status: number, body: unknown) {
  return vi.fn(async () =>
    new Response(status === 204 ? null : JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

describe("ApiClient", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("attaches Authorization and X-Org-Id", async () => {
    const fetchMock = mockFetch(200, { ok: true });
    vi.stubGlobal("fetch", fetchMock);

    const api = createClient();
    api.setAuth({ token: "tok-123", orgId: "org-abc" });
    await api.request("/api/v1/inbox/threads");

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const headers = init.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer tok-123");
    expect(headers.get("X-Org-Id")).toBe("org-abc");
  });

  it("serializes json bodies and sets the content type", async () => {
    const fetchMock = mockFetch(201, { id: "m1" });
    vi.stubGlobal("fetch", fetchMock);

    const api = createClient();
    await api.request("/api/v1/messages", { method: "POST", json: { to: "+1", body: "hi" } });

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.body).toBe(JSON.stringify({ to: "+1", body: "hi" }));
    expect((init.headers as Headers).get("Content-Type")).toBe("application/json");
  });

  it("clears stored auth and signals logout on 401", async () => {
    vi.stubGlobal("fetch", mockFetch(401, { error: { code: "unauthenticated", message: "no" } }));

    const api = createClient();
    api.setAuth({ token: "tok", orgId: "org" });
    const onUnauthorized = vi.fn();
    api.onUnauthorized = onUnauthorized;

    await expect(api.request("/api/v1/auth/me")).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledOnce();
    expect(api.auth.token).toBeNull();
    expect(loadStoredAuth().token).toBeNull();
  });

  it("surfaces the backend error code, not just the status", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(422, {
        error: { code: "sticky_sender_unavailable", message: "number retired" },
      }),
    );
    const api = createClient();
    await expect(api.request("/api/v1/messages", { method: "POST" })).rejects.toMatchObject({
      status: 422,
      code: "sticky_sender_unavailable",
    });
  });

  it("handles 204 with no body", async () => {
    vi.stubGlobal("fetch", mockFetch(204, null));
    const api = createClient();
    await expect(api.request("/api/v1/threads/x/read", { method: "POST" })).resolves.toBeUndefined();
  });

  it("survives unreadable localStorage", () => {
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(loadStoredAuth()).toEqual({ token: null, orgId: null });
    spy.mockRestore();

    const setSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(() => storeAuth({ token: "a", orgId: "b" })).not.toThrow();
    setSpy.mockRestore();
  });
});
