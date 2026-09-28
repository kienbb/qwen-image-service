import { describe, expect, it } from "vitest";
import { unwrapStatus } from "./live";

describe("unwrapStatus", () => {
  const st = { node_id: "node_1", queue: { queued: 0 } };
  it("accepts the bare status (first SSE event)", () => expect(unwrapStatus(st)).toBe(st));
  it("accepts the wrapped status (later events)", () => expect(unwrapStatus({ type: "node.status", data: st })).toBe(st));
  it("rejects other payloads", () => {
    expect(unwrapStatus({ type: "job.queued", data: {} })).toBeNull();
    expect(unwrapStatus(null)).toBeNull();
  });
});
