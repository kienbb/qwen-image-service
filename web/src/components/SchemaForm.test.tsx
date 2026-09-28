import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { SchemaForm, diffValues, toRaw } from "./SchemaForm";
import { groupFields, parseField } from "../lib/schema";
import type { JsonSchema } from "../lib/types";

afterEach(cleanup);

// trimmed copy of RuntimeSettings.model_json_schema() plus the x-* extensions added by qis.settings
const schema: JsonSchema & { "x-restart-required": string[] } = {
  $defs: {
    QueueS: {
      type: "object",
      properties: {
        max_pending: { type: "integer", minimum: 1, maximum: 10000, default: 20, title: "Max Pending", description: "Job GPU tối đa đang chờ" },
        paused_hint: { type: "boolean", default: false, title: "x" },
      },
    },
    CpuS: { type: "object", properties: { workers: { type: "integer", minimum: 1, maximum: 64, title: "Workers" } } },
    PerfS: {
      type: "object",
      properties: {
        s_per_step: { type: "object", additionalProperties: { type: "number" }, title: "S Per Step" },
        ref_mp: { type: "number", default: 0.59, title: "Ref Mp" },
      },
    },
    KeyS: { type: "object", properties: { rpm: { anyOf: [{ type: "integer", exclusiveMinimum: 0 }, { type: "null" }], default: null, title: "Rpm" } } },
  },
  properties: {
    queue: { $ref: "#/$defs/QueueS" },
    cpu: { $ref: "#/$defs/CpuS" },
    perf: { $ref: "#/$defs/PerfS" },
    keys: { $ref: "#/$defs/KeyS" },
  },
  "x-restart-required": ["cpu.workers"],
};

describe("groupFields", () => {
  it("resolves $ref and maps types", () => {
    const f = groupFields(schema, "queue");
    expect(f.map((x) => [x.name, x.kind])).toEqual([
      ["max_pending", "integer"],
      ["paused_hint", "boolean"],
    ]);
    expect(f[0]).toMatchObject({ min: 1, max: 10000, description: "Job GPU tối đa đang chờ", restart: false });
  });
  it("flags restart-required fields, dicts and nullable anyOf", () => {
    expect(groupFields(schema, "cpu")[0].restart).toBe(true);
    expect(groupFields(schema, "perf")[0].kind).toBe("dict");
    expect(groupFields(schema, "keys")[0]).toMatchObject({ kind: "integer", nullable: true, min: 0, exclusiveMin: true });
    expect(groupFields(schema, "missing")).toEqual([]);
  });
});

describe("parseField / diffValues", () => {
  const [maxPending] = groupFields(schema, "queue");
  const [rpm] = groupFields(schema, "keys");
  it("validates numbers against bounds", () => {
    expect(parseField(maxPending, "25")).toEqual([25, null]);
    expect(parseField(maxPending, "0")[1]).toBe("≥ 1");
    expect(parseField(maxPending, "2.5")[1]).toBe("Phải là số nguyên");
    expect(parseField(rpm, "")).toEqual([null, null]);
    expect(parseField(rpm, "0")[1]).toBe("> 0");
  });
  it("returns only changed fields", () => {
    const fields = groupFields(schema, "perf");
    const current = { s_per_step: { turbo: 3.8 }, ref_mp: 0.59 };
    const raw = toRaw(fields, current);
    expect(diffValues(fields, raw, current).changes).toEqual({});
    raw.s_per_step = '{"turbo": 4.1}';
    expect(diffValues(fields, raw, current).changes).toEqual({ s_per_step: { turbo: 4.1 } });
    raw.s_per_step = "{bad";
    expect(diffValues(fields, raw, current).errors.s_per_step).toBe("JSON không hợp lệ");
  });
});

describe("SchemaForm", () => {
  it("renders inputs by kind with descriptions and restart badge", () => {
    const fields = [...groupFields(schema, "queue"), ...groupFields(schema, "cpu")];
    render(<SchemaForm fields={fields} values={toRaw(fields, { max_pending: 20, paused_hint: true, workers: 4 })} onChange={() => {}} />);
    const mp = within(screen.getByTestId("field-max_pending"));
    expect((mp.getByRole("spinbutton") as HTMLInputElement).value).toBe("20");
    expect(mp.getByText("Job GPU tối đa đang chờ")).toBeTruthy();
    expect(within(screen.getByTestId("field-paused_hint")).getByRole("switch").getAttribute("aria-checked")).toBe("true");
    expect(within(screen.getByTestId("field-workers")).getByText("cần restart")).toBeTruthy();
  });
  it("renders dicts as JSON text areas", () => {
    const f = groupFields(schema, "perf");
    render(<SchemaForm fields={f} values={toRaw(f, { s_per_step: { turbo: 3.8 }, ref_mp: 0.59 })} onChange={() => {}} readOnly />);
    const ta = within(screen.getByTestId("field-s_per_step")).getByRole("textbox") as HTMLTextAreaElement;
    expect(JSON.parse(ta.value)).toEqual({ turbo: 3.8 });
    expect(ta.readOnly).toBe(true);
  });
});
