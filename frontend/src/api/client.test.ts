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

  it("asks for a reason when a major operator action needs one, then retries with it", async () => {
    const refused = { error: { code: "ops_reason_required", message: "Say why" } };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(refused), { status: 422 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const api = createClient();
    api.onReasonRequired = vi.fn(async () => "owner asked – fax off");
    const res = await api.request("/api/v1/ops/console/orgs/o1/features/fax", {
      method: "PUT",
      json: { enabled: false },
    });

    expect(res).toEqual({ ok: true });
    expect(api.onReasonRequired).toHaveBeenCalledWith({ message: "Say why" });
    const [, retry] = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    const sent = (retry.headers as Headers).get("X-Ops-Reason");
    expect(decodeURIComponent(sent ?? "")).toBe("owner asked – fax off");
    expect(retry.body).toBe(JSON.stringify({ enabled: false }));
  });

  it("gives up with the original error when the reason is cancelled", async () => {
    const refused = { error: { code: "ops_reason_required", message: "Say why" } };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(refused), { status: 422 })));
    const api = createClient();
    api.onReasonRequired = vi.fn(async () => null);
    await expect(api.request("/api/v1/ops/ban-list", { method: "POST", json: {} })).rejects.toMatchObject({
      code: "ops_reason_required",
    });
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
