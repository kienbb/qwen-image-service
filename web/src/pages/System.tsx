import { useState } from "react";
import { api, downloadPost, errMsg } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtDuration } from "../lib/format";
import { useAction, useSystemInfo } from "../lib/queries";
import { ComfyBadge } from "../components/common";
import { Alert, Badge, Button, Card, ConfirmDialog, ErrorBox, Loading, PageHeader } from "../components/ui";

function Row({ k, v, mono }: { k: string; v: React.ReactNode; mono?: boolean }) {
  return (
    <div className="grid grid-cols-[8rem_minmax(0,1fr)] gap-2 py-1.5 text-sm sm:grid-cols-[10rem_minmax(0,1fr)]">
      <div className="text-muted">{k}</div>
      <div className={mono ? "font-mono text-xs break-all" : "break-words"}>{v}</div>
    </div>
  );
}

export default function System() {
  const q = useSystemInfo();
  const { can } = useAuth();
  const [confirm, setConfirm] = useState(false);
  const [backupMsg, setBackupMsg] = useState<string | null>(null);
  const [backupErr, setBackupErr] = useState<string | null>(null);
  const [backingUp, setBackingUp] = useState(false);
  const restart = useAction(() => api.post("system/restart-comfy"), [["system"], ["status"]]);

  if (q.isLoading) return <Loading />;
  if (!q.data) return <ErrorBox error={q.error} />;
  const s = q.data;
  const c = s.comfy;

  async function backup() {
    setBackupErr(null);
    setBackupMsg(null);
    setBackingUp(true);
    try {
      const name = await downloadPost("/admin/api/system/backup", "qis-backup.db");
      setBackupMsg(`Đã tải ${name}`);
    } catch (e) {
      setBackupErr(errMsg(e));
    } finally {
      setBackingUp(false);
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader title="Hệ thống" />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="Node">
          <Row k="Tên" v={s.name} />
          <Row k="Phiên bản" v={s.version} />
          <Row k="Node ID" v={s.node_id} mono />
          <Row k="Uptime" v={fmtDuration(s.uptime_s)} />
          <Row k="File config" v={s.config ?? "—"} mono />
          <Row k="Thư mục dữ liệu" v={s.data_dir} mono />
          <Row
            k="Tài liệu API"
            v={
              <a href="/docs" target="_blank" rel="noreferrer" className="text-primary hover:underline">
                /docs (OpenAPI)
              </a>
            }
          />
        </Card>
        <Card
          title="ComfyUI"
          actions={
            can("admin") && (
              <Button size="sm" variant="outline" onClick={() => setConfirm(true)} loading={restart.isPending}>
                Restart ComfyUI
              </Button>
            )
          }
        >
          <Row k="Trạng thái" v={<ComfyBadge state={c.state} warm={c.warm} />} />
          <Row k="Chế độ" v={c.managed ? <Badge>managed (service tự chạy)</Badge> : <Badge>attach (chạy riêng)</Badge>} />
          <Row k="URL" v={c.url ?? "—"} mono />
          <Row k="Phiên bản" v={c.version || "—"} />
          <Row k="PID" v={c.pid ?? "—"} />
          <Row k="Số lần restart" v={c.restarts} />
          <Row k="Lỗi gần nhất" v={c.last_error ? <span className="text-danger">{c.last_error}</span> : "—"} />
          {c.missing_nodes?.length > 0 && <Row k="Thiếu node" v={c.missing_nodes.join(", ")} />}
          <ErrorBox error={restart.error} />
          {!c.managed && can("admin") && <div className="mt-2 text-xs text-muted">Ở chế độ attach, service không tự khởi động lại được ComfyUI.</div>}
        </Card>
      </div>
      {can("admin") && (
        <Card title="Sao lưu">
          <p className="mb-3 text-sm text-muted">Tải bản sao lưu database (users, API keys, jobs, settings). Service cũng tự sao lưu mỗi ngày, giữ 7 bản.</p>
          <Button variant="outline" onClick={() => void backup()} loading={backingUp}>
            Tải bản sao lưu DB
          </Button>
          {backupMsg && <Alert tone="success" className="mt-3">{backupMsg}</Alert>}
          {backupErr && <Alert tone="danger" className="mt-3">{backupErr}</Alert>}
        </Card>
      )}
      <ConfirmDialog
        open={confirm}
        title="Restart ComfyUI?"
        message="Job đang chạy có thể bị gián đoạn. Model phải nạp lại từ đầu."
        confirmLabel="Restart"
        danger
        loading={restart.isPending}
        onClose={() => setConfirm(false)}
        onConfirm={() => restart.mutate(undefined, { onSettled: () => setConfirm(false) })}
      />
    </div>
  );
}
