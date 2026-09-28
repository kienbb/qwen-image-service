// Helpers to turn the pydantic JSON schema of RuntimeSettings into form fields.
import type { JsonSchema } from "./types";

export type FieldKind = "integer" | "number" | "boolean" | "string" | "dict" | "json";

export interface FieldDef {
  name: string;
  title: string;
  description?: string;
  kind: FieldKind;
  nullable: boolean;
  min?: number;
  max?: number;
  exclusiveMin?: boolean;
  default?: unknown;
  enum?: unknown[];
  restart: boolean;
}

export function resolveRef(root: JsonSchema, s: JsonSchema | undefined): JsonSchema | undefined {
  if (!s) return s;
  if (s.$ref) {
    const name = s.$ref.replace(/^#\/\$defs\//, "");
    return root.$defs?.[name];
  }
  return s;
}

function kindOf(s: JsonSchema): { kind: FieldKind; nullable: boolean; base: JsonSchema } {
  let base = s;
  let nullable = false;
  if (s.anyOf?.length) {
    const nonNull = s.anyOf.filter((x) => x.type !== "null");
    nullable = nonNull.length < s.anyOf.length;
    base = nonNull.length === 1 ? { ...nonNull[0], description: s.description, default: s.default, title: s.title } : s;
    if (nonNull.length !== 1) return { kind: "json", nullable, base };
  }
  const t = Array.isArray(base.type) ? base.type.find((x) => x !== "null") : base.type;
  switch (t) {
    case "integer":
      return { kind: "integer", nullable, base };
    case "number":
      return { kind: "number", nullable, base };
    case "boolean":
      return { kind: "boolean", nullable, base };
    case "string":
      return { kind: "string", nullable, base };
    case "object":
      return { kind: "dict", nullable, base };
    default:
      return { kind: "json", nullable, base };
  }
}

function humanize(name: string): string {
  return name.replace(/_/g, " ");
}

/** Fields of one settings group (e.g. "queue"), in declaration order. */
export function groupFields(root: JsonSchema & { "x-restart-required"?: string[] }, group: string): FieldDef[] {
  const g = resolveRef(root, root.properties?.[group]);
  if (!g?.properties) return [];
  const restart = new Set(root["x-restart-required"] ?? []);
  return Object.entries(g.properties).map(([name, raw]) => {
    const { kind, nullable, base } = kindOf(raw);
    const min = base.minimum ?? base.exclusiveMinimum;
    return {
      name,
      title: humanize(name),
      description: raw.description ?? base.description,
      kind,
      nullable,
      min,
      max: base.maximum ?? base.exclusiveMaximum,
      exclusiveMin: base.exclusiveMinimum !== undefined,
      default: raw.default ?? base.default,
      enum: base.enum,
      restart: restart.has(`${group}.${name}`),
    };
  });
}

export function groupDescription(root: JsonSchema, group: string): string | undefined {
  return resolveRef(root, root.properties?.[group])?.description;
}

/** Validate/convert a raw input string for a field. Returns [value, error]. */
export function parseField(f: FieldDef, raw: unknown): [unknown, string | null] {
  if (f.kind === "boolean") return [Boolean(raw), null];
  if (f.kind === "dict" || f.kind === "json") {
    if (typeof raw !== "string") return [raw, null];
    try {
      return [JSON.parse(raw), null];
    } catch {
      return [raw, "JSON không hợp lệ"];
    }
  }
  const s = String(raw ?? "").trim();
  if (s === "" && f.nullable) return [null, null];
  if (f.kind === "integer" || f.kind === "number") {
    if (s === "") return [null, "Bắt buộc"];
    const n = Number(s);
    if (!Number.isFinite(n)) return [s, "Không phải số"];
    if (f.kind === "integer" && !Number.isInteger(n)) return [s, "Phải là số nguyên"];
    if (f.min !== undefined && (f.exclusiveMin ? n <= f.min : n < f.min)) return [n, `${f.exclusiveMin ? ">" : "≥"} ${f.min}`];
    if (f.max !== undefined && n > f.max) return [n, `≤ ${f.max}`];
    return [n, null];
  }
  return [s, null];
}

export const SETTINGS_GROUPS: { id: string; title: string; hint?: string }[] = [
  { id: "queue", title: "Queue" },
  { id: "scheduler", title: "Scheduler" },
  { id: "keys", title: "Mặc định API key" },
  { id: "api", title: "API" },
  { id: "job", title: "Job" },
  { id: "inputs", title: "Ảnh đầu vào" },
  { id: "storage", title: "Lưu trữ" },
  { id: "cpu", title: "CPU" },
  { id: "node", title: "Node" },
  { id: "perf", title: "Perf (cost model)", hint: "Tự hiệu chỉnh từ benchmark và job thật. Chỉ sửa khi biết mình đang làm gì." },
];
