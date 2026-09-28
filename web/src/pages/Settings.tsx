import { useMemo, useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useAction, useSettings } from "../lib/queries";
import { groupDescription, groupFields, SETTINGS_GROUPS } from "../lib/schema";
import type { SettingsResponse } from "../lib/types";
import { SchemaForm, useGroupForm } from "../components/SchemaForm";
import { Alert, Badge, Button, Card, Code, ErrorBox, Loading, PageHeader } from "../components/ui";

const EMPTY: Record<string, unknown> = {};

function GroupCard({ data, group, title, hint }: { data: SettingsResponse; group: string; title: string; hint?: string }) {
  const { can } = useAuth();
  const admin = can("admin");
  const fields = useMemo(() => groupFields(data.schema, group), [data.schema, group]);
  const current = data.values[group] ?? EMPTY;
  const form = useGroupForm(fields, current);
  const [saved, setSaved] = useState(false);
  const save = useAction((body: Record<string, unknown>) => api.put(`settings/${group}`, body), [["settings"], ["status"]]);
  if (!fields.length) return null;
  const hasErrors = Object.keys(form.errors).length > 0;
  const restartChanged = fields.some((f) => f.restart && f.name in form.changes);
  const scope = data.schema["x-scopes"]?.[group];

  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          {title} <span className="font-mono text-xs font-normal text-muted">{group}</span>
          {scope === "cluster" && <Badge tone="info">cluster</Badge>}
        </span>
      }
      actions={
        admin && (
          <>
            {form.dirty && (
              <Button size="sm" variant="ghost" onClick={form.reset}>
                Bỏ thay đổi
              </Button>
            )}
            <Button
              size="sm"
              disabled={!form.dirty || hasErrors}
              loading={save.isPending}
              onClick={() =>
                save.mutate(form.changes, {
                  onSuccess: () => {
                    setSaved(true);
                    setTimeout(() => setSaved(false), 2500);
                  },
                })
              }
            >
              Lưu
            </Button>
          </>
        )
      }
    >
      {(hint || groupDescription(data.schema, group)) && <div className="mb-3 text-xs text-muted">{hint ?? groupDescription(data.schema, group)}</div>}
      <SchemaForm fields={fields} values={form.raw} onChange={form.set} readOnly={!admin} errors={form.errors} />
      <div className="mt-3 space-y-2">
        {restartChanged && <Alert tone="warning">Có trường cần restart service mới có hiệu lực.</Alert>}
        {save.error && <ErrorBox error={save.error} />}
        {saved && <Alert tone="success">Đã lưu, áp dụng ngay.</Alert>}
      </div>
    </Card>
  );
}

export default function Settings() {
  const q = useSettings();
  const { can } = useAuth();
  if (q.isLoading) return <Loading />;
  if (!q.data) return <ErrorBox error={q.error} />;
  const d = q.data;
  return (
    <div className="space-y-4">
      <PageHeader title="Queue & Settings" sub="Thay đổi được lưu vào DB và áp dụng ngay, không cần restart (trừ trường có nhãn)." />
      {!can("admin") && <Alert tone="info">Bạn đang xem ở chế độ chỉ đọc (cần quyền admin để sửa).</Alert>}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {SETTINGS_GROUPS.map((g) => (
          <GroupCard key={g.id} data={d} group={g.id} title={g.title} hint={g.hint} />
        ))}
      </div>
      <Card title="Cấu hình tĩnh (config.toml, chỉ đọc)">
        <div className="mb-3 grid gap-2 text-sm sm:grid-cols-2">
          <div>
            <span className="text-muted">File config: </span>
            <span className="font-mono text-xs break-all">{d.config.source ?? "(không có — chạy bằng mặc định)"}</span>
          </div>
          <div>
            <span className="text-muted">Thư mục dữ liệu: </span>
            <span className="font-mono text-xs break-all">{d.config.data_dir}</span>
          </div>
          <div>
            <span className="text-muted">ComfyUI: </span>
            <span className="font-mono text-xs break-all">{d.config.comfyui || "—"}</span>
          </div>
          <div>
            <span className="text-muted">Profile: </span>
            <span className="font-mono text-xs">{String(d.config.profile?.tier ?? "—")}</span>
          </div>
        </div>
        <div className="grid gap-3 lg:grid-cols-3">
          <div>
            <div className="mb-1 text-xs font-medium text-muted">[server]</div>
            <Code>{JSON.stringify(d.config.server, null, 2)}</Code>
          </div>
          <div>
            <div className="mb-1 text-xs font-medium text-muted">[comfy]</div>
            <Code>{JSON.stringify(d.config.comfy, null, 2)}</Code>
          </div>
          <div>
            <div className="mb-1 text-xs font-medium text-muted">[profile]</div>
            <Code>{JSON.stringify(d.config.profile, null, 2)}</Code>
          </div>
        </div>
        <div className="mt-2 text-xs text-muted">Sửa các giá trị này trong file config rồi restart service.</div>
      </Card>
    </div>
  );
}
