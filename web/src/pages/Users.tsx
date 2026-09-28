import { useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo, fmtTime } from "../lib/format";
import { useAction, useUsers } from "../lib/queries";
import type { Role, User } from "../lib/types";
import { Alert, Badge, Button, Card, Code, ConfirmDialog, CopyButton, Dialog, ErrorBox, Field, Input, Loading, PageHeader, Select, Table } from "../components/ui";

function CreateUser({ open, onClose, roles }: { open: boolean; onClose: () => void; roles: Role[] }) {
  const blank = { username: "", email: "", password: "", role: "viewer" as Role };
  const [f, setF] = useState(blank);
  const create = useAction((b: typeof blank) => api.post<User>("users", { ...b, email: b.email || null }), [["users"]]);
  const close = () => {
    setF(blank);
    create.reset();
    onClose();
  };
  return (
    <Dialog
      open={open}
      onClose={close}
      title="Thêm người dùng"
      footer={
        <>
          <Button variant="outline" onClick={close}>
            Huỷ bỏ
          </Button>
          <Button loading={create.isPending} disabled={!f.username || f.password.length < 10} onClick={() => create.mutate(f, { onSuccess: close })}>
            Tạo
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Field label="Username" hint="2–64 ký tự: chữ, số, . _ -">
          <Input autoFocus value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} />
        </Field>
        <Field label="Email (tuỳ chọn)">
          <Input type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} />
        </Field>
        <Field label="Mật khẩu tạm" hint="Tối thiểu 10 ký tự; người dùng phải đổi ở lần đăng nhập đầu">
          <Input type="text" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} autoComplete="off" />
        </Field>
        <Field label="Vai trò">
          <Select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value as Role })}>
            {roles.map((r) => (
              <option key={r}>{r}</option>
            ))}
          </Select>
        </Field>
        <ErrorBox error={create.error} />
      </div>
    </Dialog>
  );
}

type Pending = { kind: "delete" | "reset" | "toggle"; user: User } | null;

export default function Users() {
  const q = useUsers();
  const { user: me } = useAuth();
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState<Pending>(null);
  const [newPw, setNewPw] = useState<{ user: string; password: string } | null>(null);
  const patch = useAction(({ id, body }: { id: string; body: Record<string, unknown> }) => api.patch<User>(`users/${id}`, body), [["users"]]);
  const reset = useAction((id: string) => api.post<{ password: string }>(`users/${id}/reset-password`), [["users"]]);
  const del = useAction((id: string) => api.del(`users/${id}`), [["users"]]);

  if (q.isLoading) return <Loading />;
  if (!q.data) return <ErrorBox error={q.error} />;
  const busy = patch.isPending || reset.isPending || del.isPending;

  function confirm() {
    if (!pending) return;
    const u = pending.user;
    const done = { onSettled: () => setPending(null) };
    if (pending.kind === "delete") del.mutate(u.id, done);
    else if (pending.kind === "toggle") patch.mutate({ id: u.id, body: { disabled: !u.disabled } }, done);
    else reset.mutate(u.id, { ...done, onSuccess: (r) => setNewPw({ user: u.username, password: r.password }) });
  }

  return (
    <div>
      <PageHeader title="Người dùng" actions={<Button onClick={() => setOpen(true)}>+ Thêm người dùng</Button>} />
      <Card bodyClass="p-0">
        <Table>
          <thead>
            <tr>
              <th>Username</th>
              <th>Email</th>
              <th>Vai trò</th>
              <th>Trạng thái</th>
              <th>Đăng nhập cuối</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {q.data.data.map((u) => {
              const self = u.id === me?.id;
              const locked = u.locked_until && u.locked_until > Date.now() / 1000;
              return (
                <tr key={u.id}>
                  <td>
                    <div className="font-medium">
                      {u.username} {self && <Badge tone="primary">bạn</Badge>}
                    </div>
                    <div className="text-[11px] text-muted">tạo {fmtTime(u.created_at)}</div>
                  </td>
                  <td className="text-muted">{u.email ?? "—"}</td>
                  <td>
                    <Select
                      value={u.role}
                      className="h-7 w-28 text-xs"
                      disabled={busy}
                      onChange={(e) => patch.mutate({ id: u.id, body: { role: e.target.value } })}
                      aria-label={`Vai trò của ${u.username}`}
                    >
                      {q.data.roles.map((r) => (
                        <option key={r}>{r}</option>
                      ))}
                    </Select>
                  </td>
                  <td>
                    <div className="flex flex-wrap gap-1">
                      {u.disabled ? <Badge tone="danger">vô hiệu</Badge> : <Badge tone="success">hoạt động</Badge>}
                      {u.must_change_pw ? <Badge tone="warning">phải đổi mật khẩu</Badge> : null}
                      {locked ? <Badge tone="warning">bị khoá tạm</Badge> : null}
                    </div>
                  </td>
                  <td className="text-xs text-muted">{fmtAgo(u.last_login_at)}</td>
                  <td>
                    <div className="flex justify-end gap-1">
                      <Button size="sm" variant="ghost" onClick={() => setPending({ kind: "reset", user: u })}>
                        Reset mật khẩu
                      </Button>
                      {!self && (
                        <Button size="sm" variant="ghost" onClick={() => setPending({ kind: "toggle", user: u })}>
                          {u.disabled ? "Kích hoạt" : "Vô hiệu"}
                        </Button>
                      )}
                      {!self && (
                        <Button size="sm" variant="danger-ghost" onClick={() => setPending({ kind: "delete", user: u })}>
                          Xoá
                        </Button>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      </Card>
      <div className="mt-3 space-y-2">
        <ErrorBox error={patch.error || reset.error || del.error} />
        <Alert tone="info">
          Vai trò: <b>admin</b> toàn quyền · <b>operator</b> chạy playground, huỷ/chạy lại job, quản lý API key của mình · <b>viewer</b> chỉ xem.
        </Alert>
      </div>

      <CreateUser open={open} onClose={() => setOpen(false)} roles={q.data.roles} />
      <ConfirmDialog
        open={!!pending}
        title={pending?.kind === "delete" ? "Xoá người dùng?" : pending?.kind === "reset" ? "Reset mật khẩu?" : pending?.user.disabled ? "Kích hoạt người dùng?" : "Vô hiệu người dùng?"}
        message={
          pending?.kind === "delete"
            ? `Xoá vĩnh viễn ${pending.user.username}.`
            : pending?.kind === "reset"
              ? `Tạo mật khẩu tạm mới cho ${pending.user.username}; người dùng phải đổi khi đăng nhập.`
              : pending?.user.disabled
                ? `${pending?.user.username} đăng nhập lại được.`
                : `${pending?.user.username} bị đăng xuất khỏi mọi phiên và không đăng nhập được.`
        }
        danger={pending?.kind === "delete" || (pending?.kind === "toggle" && !pending.user.disabled)}
        confirmLabel={pending?.kind === "delete" ? "Xoá" : "Xác nhận"}
        loading={busy}
        onClose={() => setPending(null)}
        onConfirm={confirm}
      />
      <Dialog open={!!newPw} onClose={() => setNewPw(null)} title="Mật khẩu tạm mới" footer={<Button onClick={() => setNewPw(null)}>Đóng</Button>}>
        <Alert tone="warning" className="mb-3">
          Mật khẩu chỉ hiển thị một lần. Gửi cho {newPw?.user} qua kênh an toàn.
        </Alert>
        <Code className="select-all">{newPw?.password}</Code>
        <div className="mt-2">{newPw && <CopyButton text={newPw.password} label="Copy mật khẩu" size="md" />}</div>
      </Dialog>
    </div>
  );
}
