import { useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo, fmtTime } from "../lib/format";
import { useAction, useKeys } from "../lib/queries";
import type { ApiKey } from "../lib/types";
import { Alert, Badge, Button, Card, Checkbox, Code, ConfirmDialog, CopyButton, Dialog, Empty, ErrorBox, Field, Input, Loading, PageHeader, Select, Table } from "../components/ui";

interface NewKey {
  name: string;
  scopes: string[];
  priority: string;
  max_pending: string;
  rpm: string;
  daily_images: string;
  expires_in_days: string;
}

const DEFAULT_SCOPES = ["images:generate", "images:edit", "tools:*", "files:*", "jobs:read"];

function keyStatus(k: ApiKey): { tone: "success" | "danger" | "warning"; label: string } {
  if (k.revoked_at) return { tone: "danger", label: "đã thu hồi" };
  if (k.expires_at && k.expires_at < Date.now() / 1000) return { tone: "warning", label: "hết hạn" };
  return { tone: "success", label: "hoạt động" };
}

const num = (s: string) => (s.trim() === "" ? null : Number(s));

function CreateDialog({ open, onClose, scopes }: { open: boolean; onClose: () => void; scopes: string[] }) {
  const { can } = useAuth();
  const blank: NewKey = { name: "", scopes: DEFAULT_SCOPES.filter((s) => scopes.includes(s)), priority: "interactive", max_pending: "", rpm: "", daily_images: "", expires_in_days: "" };
  const [f, setF] = useState<NewKey>(blank);
  const [created, setCreated] = useState<(ApiKey & { key: string }) | null>(null);
  const create = useAction(
    (b: Record<string, unknown>) => api.post<ApiKey & { key: string }>("keys", b),
    [["keys"]],
  );
  const close = () => {
    setF(blank);
    setCreated(null);
    create.reset();
    onClose();
  };
  const toggle = (s: string, on: boolean) => setF((x) => ({ ...x, scopes: on ? [...x.scopes, s] : x.scopes.filter((y) => y !== s) }));
  const submit = () =>
    create.mutate(
      {
        name: f.name.trim(),
        scopes: f.scopes,
        priority: f.priority,
        max_pending: num(f.max_pending),
        rpm: num(f.rpm),
        daily_images: num(f.daily_images),
        expires_in_days: num(f.expires_in_days),
      },
      { onSuccess: setCreated },
    );

  if (created) {
    return (
      <Dialog open={open} onClose={close} title="API key đã tạo" footer={<Button onClick={close}>Tôi đã lưu key</Button>}>
        <Alert tone="warning" className="mb-3">
          Đây là lần duy nhất key được hiển thị. Hãy copy và lưu vào nơi an toàn.
        </Alert>
        <Code className="break-all whitespace-pre-wrap select-all">{created.key}</Code>
        <div className="mt-2">
          <CopyButton text={created.key} label="Copy key" size="md" />
        </div>
      </Dialog>
    );
  }
  return (
    <Dialog
      open={open}
      onClose={close}
      title="Tạo API key"
      footer={
        <>
          <Button variant="outline" onClick={close}>
            Huỷ bỏ
          </Button>
          <Button onClick={submit} loading={create.isPending} disabled={!f.name.trim() || !f.scopes.length}>
            Tạo key
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Field label="Tên">
          <Input autoFocus value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="vd. game-client-prod" />
        </Field>
        <Field label="Scopes">
          <div className="grid grid-cols-2 gap-1.5">
            {scopes.map((s) => (
              <Checkbox key={s} checked={f.scopes.includes(s)} onChange={(v) => toggle(s, v)} disabled={s === "admin:*" && !can("admin")} label={<span className="font-mono text-xs">{s}</span>} />
            ))}
          </div>
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Priority">
            <Select value={f.priority} onChange={(e) => setF({ ...f, priority: e.target.value })}>
              <option value="interactive">interactive</option>
              <option value="batch">batch</option>
            </Select>
          </Field>
          <Field label="Hết hạn sau (ngày)" hint="Trống = không hết hạn">
            <Input type="number" min={1} value={f.expires_in_days} onChange={(e) => setF({ ...f, expires_in_days: e.target.value })} />
          </Field>
          <Field label="Max pending" hint="Trống = mặc định">
            <Input type="number" min={1} value={f.max_pending} onChange={(e) => setF({ ...f, max_pending: e.target.value })} />
          </Field>
          <Field label="Request / phút" hint="Trống = mặc định">
            <Input type="number" min={1} value={f.rpm} onChange={(e) => setF({ ...f, rpm: e.target.value })} />
          </Field>
          <Field label="Ảnh / ngày" hint="Trống = không giới hạn">
            <Input type="number" min={1} value={f.daily_images} onChange={(e) => setF({ ...f, daily_images: e.target.value })} />
          </Field>
        </div>
        <ErrorBox error={create.error} />
      </div>
    </Dialog>
  );
}

export default function Keys() {
  const q = useKeys();
  const [open, setOpen] = useState(false);
  const [revoke, setRevoke] = useState<ApiKey | null>(null);
  const [showRevoked, setShowRevoked] = useState(false);
  const del = useAction((id: string) => api.del(`keys/${id}`), [["keys"]]);

  if (q.isLoading) return <Loading />;
  if (!q.data) return <ErrorBox error={q.error} />;
  const rows = q.data.data.filter((k) => showRevoked || !k.revoked_at);
  return (
    <div>
      <PageHeader
        title="API Keys"
        actions={
          <>
            <Checkbox checked={showRevoked} onChange={setShowRevoked} label="Hiện key đã thu hồi" />
            <Button onClick={() => setOpen(true)}>+ Tạo key</Button>
          </>
        }
      />
      <Card bodyClass="p-0">
        {!rows.length ? (
          <Empty>Chưa có API key.</Empty>
        ) : (
          <Table>
            <thead>
              <tr>
                <th>Tên</th>
                <th>Prefix</th>
                <th>Scopes</th>
                <th>Priority</th>
                <th>Giới hạn</th>
                <th className="text-right">Jobs 24h</th>
                <th>Dùng lần cuối</th>
                <th>Trạng thái</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((k) => {
                const s = keyStatus(k);
                return (
                  <tr key={k.id} className={k.revoked_at ? "opacity-60" : ""}>
                    <td>
                      <div className="font-medium">{k.name}</div>
                      <div className="text-[11px] text-muted">tạo {fmtTime(k.created_at)}</div>
                    </td>
                    <td className="font-mono text-xs whitespace-nowrap">{k.prefix}…</td>
                    <td>
                      <div className="flex max-w-56 flex-wrap gap-1">
                        {k.scopes.map((sc) => (
                          <Badge key={sc} tone={sc === "admin:*" ? "danger" : "neutral"}>
                            {sc}
                          </Badge>
                        ))}
                      </div>
                    </td>
                    <td>{k.priority}</td>
                    <td className="text-xs whitespace-nowrap text-muted">
                      pending {k.max_pending ?? "mặc định"} · rpm {k.rpm ?? "mặc định"}
                      {k.daily_images ? ` · ${k.daily_images} ảnh/ngày` : ""}
                      {k.expires_at ? <div>hết hạn {fmtTime(k.expires_at)}</div> : null}
                    </td>
                    <td className="text-right tabular-nums">
                      {k.jobs_24h ?? 0}
                      {k.failed_24h ? <span className="text-danger"> ({k.failed_24h} lỗi)</span> : null}
                    </td>
                    <td className="text-xs whitespace-nowrap text-muted">{fmtAgo(k.last_used_at)}</td>
                    <td>
                      <Badge tone={s.tone}>{s.label}</Badge>
                    </td>
                    <td className="text-right">
                      {!k.revoked_at && (
                        <Button size="sm" variant="danger-ghost" onClick={() => setRevoke(k)}>
                          Thu hồi
                        </Button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>
      <ErrorBox error={del.error} />
      <CreateDialog open={open} onClose={() => setOpen(false)} scopes={q.data.scopes} />
      <ConfirmDialog
        open={!!revoke}
        title="Thu hồi API key?"
        message={
          <>
            Key <b>{revoke?.name}</b> ({revoke?.prefix}…) sẽ ngừng hoạt động ngay. Không thể hoàn tác.
          </>
        }
        confirmLabel="Thu hồi"
        danger
        loading={del.isPending}
        onClose={() => setRevoke(null)}
        onConfirm={() => revoke && del.mutate(revoke.id, { onSettled: () => setRevoke(null) })}
      />
    </div>
  );
}
