import { useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { api, errMsg } from "../lib/api";
import { useAuth } from "../lib/auth";
import type { Bootstrap } from "../lib/types";
import { Alert, Button, Card, Code, Field, Input } from "../components/ui";

function Shell({ children, sub }: { children: React.ReactNode; sub?: string }) {
  return (
    <div className="flex min-h-screen items-start justify-center px-4 pt-[12vh]">
      <div className="w-full max-w-sm">
        <div className="mb-5 flex items-center gap-2.5">
          <img src={`${import.meta.env.BASE_URL}favicon.svg`} alt="" className="h-8 w-8" />
          <div>
            <div className="font-semibold">Qwen Image Service</div>
            <div className="text-xs text-muted">{sub ?? "Dashboard quản trị"}</div>
          </div>
        </div>
        {children}
      </div>
    </div>
  );
}

export function LoginPage() {
  const { login, setup } = useAuth();
  const boot = useQuery({ queryKey: ["bootstrap"], queryFn: () => api.get<Bootstrap>("auth/bootstrap", { quiet: true }) });
  const [form, setForm] = useState({ login: "", password: "", username: "", email: "", password2: "" });
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const needsSetup = boot.data?.needs_setup;
  const sub = boot.data ? `${boot.data.node} · v${boot.data.version}` : undefined;

  async function onLogin(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      await login(form.login.trim(), form.password);
    } catch (x) {
      setErr(errMsg(x));
    } finally {
      setBusy(false);
    }
  }

  async function onSetup(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    if (form.password !== form.password2) return setErr("Mật khẩu nhập lại không khớp");
    if (form.password.length < 10) return setErr("Mật khẩu tối thiểu 10 ký tự");
    setBusy(true);
    try {
      await setup(form.username.trim(), form.password, form.email.trim() || undefined);
    } catch (x) {
      setErr(errMsg(x));
    } finally {
      setBusy(false);
    }
  }

  if (needsSetup) {
    if (!boot.data?.setup_allowed_here) {
      return (
        <Shell sub={sub}>
          <Card title="Chưa có tài khoản admin">
            <p className="mb-3 text-sm text-muted">
              Node này chưa có người dùng nào. Vì lý do bảo mật, admin đầu tiên chỉ được tạo trên chính máy chạy service (localhost). Chạy lệnh:
            </p>
            <Code>qis admin create --username admin</Code>
            <Button variant="outline" className="mt-3" onClick={() => void boot.refetch()}>
              Kiểm tra lại
            </Button>
          </Card>
        </Shell>
      );
    }
    return (
      <Shell sub={sub}>
        <Card title="Tạo tài khoản admin đầu tiên">
          <form className="space-y-3" onSubmit={onSetup}>
            <Field label="Username">
              <Input autoFocus required value={form.username} onChange={set("username")} autoComplete="username" />
            </Field>
            <Field label="Email (tuỳ chọn)">
              <Input type="email" value={form.email} onChange={set("email")} />
            </Field>
            <Field label="Mật khẩu" hint="Tối thiểu 10 ký tự">
              <Input type="password" required value={form.password} onChange={set("password")} autoComplete="new-password" />
            </Field>
            <Field label="Nhập lại mật khẩu">
              <Input type="password" required value={form.password2} onChange={set("password2")} autoComplete="new-password" />
            </Field>
            {err && <Alert tone="danger">{err}</Alert>}
            <Button type="submit" className="w-full justify-center" loading={busy}>
              Tạo admin
            </Button>
          </form>
        </Card>
      </Shell>
    );
  }

  return (
    <Shell sub={sub}>
      <Card title="Đăng nhập">
        <form className="space-y-3" onSubmit={onLogin}>
          <Field label="Username hoặc email">
            <Input autoFocus required value={form.login} onChange={set("login")} autoComplete="username" name="login" />
          </Field>
          <Field label="Mật khẩu">
            <Input type="password" required value={form.password} onChange={set("password")} autoComplete="current-password" name="password" />
          </Field>
          {err && <Alert tone="danger">{err}</Alert>}
          <Button type="submit" className="w-full justify-center" loading={busy}>
            Đăng nhập
          </Button>
        </form>
      </Card>
      {boot.error && <Alert tone="danger" className="mt-3">{errMsg(boot.error)}</Alert>}
    </Shell>
  );
}

export function ChangePasswordPage({ forced }: { forced?: boolean }) {
  const { changePassword, logout, user } = useAuth();
  const nav = useNavigate();
  const [f, setF] = useState({ current: "", next: "", again: "" });
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    if (f.next !== f.again) return setErr("Mật khẩu mới nhập lại không khớp");
    if (f.next.length < 10) return setErr("Mật khẩu mới tối thiểu 10 ký tự");
    setBusy(true);
    try {
      await changePassword(f.current, f.next);
      setOk(true);
      setF({ current: "", next: "", again: "" });
      if (!forced) setTimeout(() => nav("/"), 800);
    } catch (x) {
      setErr(errMsg(x));
    } finally {
      setBusy(false);
    }
  }

  const form = (
    <Card title={forced ? "Cần đổi mật khẩu" : "Đổi mật khẩu"}>
      {forced && (
        <Alert tone="warning" className="mb-3">
          Tài khoản <b>{user?.username}</b> đang dùng mật khẩu tạm. Hãy đặt mật khẩu mới để tiếp tục.
        </Alert>
      )}
      <form className="space-y-3" onSubmit={submit}>
        <Field label="Mật khẩu hiện tại">
          <Input type="password" required value={f.current} onChange={(e) => setF({ ...f, current: e.target.value })} autoComplete="current-password" />
        </Field>
        <Field label="Mật khẩu mới" hint="Tối thiểu 10 ký tự">
          <Input type="password" required value={f.next} onChange={(e) => setF({ ...f, next: e.target.value })} autoComplete="new-password" />
        </Field>
        <Field label="Nhập lại mật khẩu mới">
          <Input type="password" required value={f.again} onChange={(e) => setF({ ...f, again: e.target.value })} autoComplete="new-password" />
        </Field>
        {err && <Alert tone="danger">{err}</Alert>}
        {ok && <Alert tone="success">Đã đổi mật khẩu.</Alert>}
        <div className="flex gap-2">
          <Button type="submit" loading={busy}>
            Lưu mật khẩu
          </Button>
          {forced && (
            <Button type="button" variant="outline" onClick={() => void logout()}>
              Đăng xuất
            </Button>
          )}
        </div>
      </form>
    </Card>
  );
  if (!forced) return <div className="max-w-md">{form}</div>;
  return <Shell>{form}</Shell>;
}
