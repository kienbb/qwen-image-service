import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, buildUrl, onAuthError, request, setCsrf, toApiError } from "./api";

function fakeFetch(status: number, body: unknown) {
  return vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => {
    const text = typeof body === "string" ? body : JSON.stringify(body);
    return new Response(status === 204 ? null : text, { status, headers: { "content-type": "application/json" } });
  });
}

afterEach(() => {
  setCsrf(null);
  onAuthError(null);
});

describe("toApiError", () => {
  it("reads the gateway error envelope", () => {
    const e = toApiError(429, { error: { message: "queue is full", type: "rate_limit_error", code: "queue_full", param: null } });
    expect(e).toBeInstanceOf(ApiError);
    expect(e.message).toBe("queue is full");
    expect(e.code).toBe("queue_full");
    expect(e.type).toBe("rate_limit_error");
    expect(e.status).toBe(429);
  });
  it("falls back to FastAPI detail and plain text", () => {
    expect(toApiError(404, { detail: "job not found" }).message).toBe("job not found");
    expect(toApiError(422, { detail: [{ loc: ["body", "name"], msg: "field required" }] }).message).toBe("body.name: field required");
    expect(toApiError(502, "<html>Bad gateway</html>").message).toContain("Bad gateway");
    expect(toApiError(500, null, "Internal Server Error").message).toBe("Internal Server Error");
    expect(toApiError(503, undefined).message).toBe("HTTP 503");
  });
});

describe("request", () => {
  it("sends the CSRF header on writes only, same-origin credentials", async () => {
    setCsrf("tok123");
    const f = fakeFetch(200, { ok: true });
    await request("POST", "keys", { body: { name: "x" }, fetchImpl: f });
    await request("GET", "keys", { fetchImpl: f });
    const [url, post] = f.mock.calls[0];
    expect(url).toBe("/admin/api/keys");
    const h = post!.headers as Record<string, string>;
    expect(h["X-CSRF-Token"]).toBe("tok123");
    expect(h["Content-Type"]).toBe("application/json");
    expect(post!.credentials).toBe("same-origin");
    expect(post!.body).toBe('{"name":"x"}');
    expect((f.mock.calls[1][1]!.headers as Record<string, string>)["X-CSRF-Token"]).toBeUndefined();
  });

  it("throws ApiError and notifies the auth listener on 401 and must_change_password", async () => {
    const seen: string[] = [];
    onAuthError((e) => seen.push(`${e.status}:${e.code}`));
    await expect(
      request("GET", "status", { fetchImpl: fakeFetch(401, { error: { message: "login required", code: "not_logged_in" } }) }),
    ).rejects.toMatchObject({ status: 401, message: "login required" });
    await expect(
      request("GET", "status", { fetchImpl: fakeFetch(403, { error: { message: "change your password first", code: "must_change_password" } }) }),
    ).rejects.toBeInstanceOf(ApiError);
    await expect(
      request("GET", "users", { fetchImpl: fakeFetch(403, { error: { message: "requires role admin", code: "forbidden" } }) }),
    ).rejects.toThrow("requires role admin");
    expect(seen).toEqual(["401:not_logged_in", "403:must_change_password"]);
  });

  it("does not notify when quiet", async () => {
    const fn = vi.fn();
    onAuthError(fn);
    await expect(request("GET", "auth/me", { quiet: true, fetchImpl: fakeFetch(401, { error: { message: "x" } }) })).rejects.toBeInstanceOf(ApiError);
    expect(fn).not.toHaveBeenCalled();
  });

  it("maps network failures to status 0", async () => {
    const f = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    await expect(request("GET", "status", { fetchImpl: f })).rejects.toMatchObject({ status: 0 });
  });

  it("parses empty bodies as undefined", async () => {
    expect(await request("DELETE", "keys/1", { fetchImpl: fakeFetch(204, "") })).toBeUndefined();
  });
});

describe("buildUrl", () => {
  it("drops empty query values", () => {
    expect(buildUrl("jobs", { status: "", limit: 50, before: undefined })).toBe("/admin/api/jobs?limit=50");
    expect(buildUrl("/admin/api/system/backup")).toBe("/admin/api/system/backup");
  });
});
