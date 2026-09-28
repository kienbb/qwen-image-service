import { useEffect, useMemo, useState } from "react";
import { parseField, type FieldDef } from "../lib/schema";
import { Badge, Input, Switch, Textarea } from "./ui";

// Form for one settings group, generated from its JSON schema. Holds raw input strings, converts on read.

export type RawValues = Record<string, unknown>;

export function toRaw(fields: FieldDef[], values: Record<string, unknown>): RawValues {
  const out: RawValues = {};
  for (const f of fields) {
    const v = values[f.name];
    if (f.kind === "boolean") out[f.name] = Boolean(v);
    else if (f.kind === "dict" || f.kind === "json") out[f.name] = JSON.stringify(v ?? null, null, 2);
    else out[f.name] = v === null || v === undefined ? "" : String(v);
  }
  return out;
}

/** Changed fields only, converted; errors keyed by field name. */
export function diffValues(fields: FieldDef[], raw: RawValues, current: Record<string, unknown>) {
  const changes: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  for (const f of fields) {
    const [v, e] = parseField(f, raw[f.name]);
    if (e) {
      errors[f.name] = e;
      continue;
    }
    if (JSON.stringify(v) !== JSON.stringify(current[f.name] ?? null)) changes[f.name] = v;
  }
  return { changes, errors };
}

export function SchemaForm({
  fields,
  values,
  readOnly,
  onChange,
  errors,
}: {
  fields: FieldDef[];
  values: RawValues;
  readOnly?: boolean;
  onChange: (name: string, v: unknown) => void;
  errors?: Record<string, string>;
}) {
  return (
    <div className="grid gap-x-4 gap-y-3 sm:grid-cols-2">
      {fields.map((f) => {
        const id = `f-${f.name}`;
        const err = errors?.[f.name];
        const wide = f.kind === "dict" || f.kind === "json";
        return (
          <div key={f.name} className={wide ? "sm:col-span-2" : ""} data-testid={`field-${f.name}`}>
            <label htmlFor={id} className="mb-1 flex flex-wrap items-center gap-1.5 text-xs font-medium">
              <span className="font-mono">{f.name}</span>
              {f.restart && <Badge tone="warning">cần restart</Badge>}
              {(f.min !== undefined || f.max !== undefined) && (
                <span className="font-normal text-muted">
                  [{f.min ?? "…"} – {f.max ?? "…"}]
                </span>
              )}
            </label>
            {f.kind === "boolean" ? (
              <Switch checked={Boolean(values[f.name])} onChange={(v) => onChange(f.name, v)} disabled={readOnly} />
            ) : wide ? (
              <Textarea id={id} rows={4} className="font-mono text-xs" value={String(values[f.name] ?? "")} readOnly={readOnly} onChange={(e) => onChange(f.name, e.target.value)} />
            ) : (
              <Input
                id={id}
                type={f.kind === "string" ? "text" : "number"}
                step={f.kind === "integer" ? 1 : "any"}
                value={String(values[f.name] ?? "")}
                readOnly={readOnly}
                placeholder={f.nullable ? "(trống)" : undefined}
                onChange={(e) => onChange(f.name, e.target.value)}
              />
            )}
            {err ? <div className="mt-1 text-xs text-danger">{err}</div> : f.description && <div className="mt-1 text-xs text-muted">{f.description}</div>}
          </div>
        );
      })}
    </div>
  );
}

/** Local editable copy of a group's values that resets when the server values change. */
export function useGroupForm(fields: FieldDef[], current: Record<string, unknown>) {
  const initial = useMemo(() => toRaw(fields, current), [fields, current]);
  const [raw, setRaw] = useState<RawValues>(initial);
  useEffect(() => setRaw(initial), [initial]);
  const { changes, errors } = useMemo(() => diffValues(fields, raw, current), [fields, raw, current]);
  return {
    raw,
    set: (name: string, v: unknown) => setRaw((r) => ({ ...r, [name]: v })),
    reset: () => setRaw(initial),
    changes,
    errors,
    dirty: Object.keys(changes).length > 0,
  };
}
