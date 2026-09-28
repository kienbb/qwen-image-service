import { useState } from "react";
import { Link } from "react-router";
import { Bar, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, errMsg } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtBytes, fmtDuration, fmtTime, jobLabel } from "../lib/format";
import { useLive } from "../lib/live";
import { useAction, useStats, useStatus } from "../lib/queries";
import type { AdminStatus, Job } from "../lib/types";
import { ComfyBadge, JobStatusBadge } from "../components/common";
import { Alert, Badge, Button, Card, Empty, ErrorBox, Loading, PageHeader, Progress, Select, Stat } from "../components/ui";

function mb(n?: number) {
  return n ? n / 1024 ** 2 : 0;
}

function GpuCard({ st }: { st: AdminStatus }) {
  const g = st.gpu ?? {};
  // NVML gives memory_total/used (bytes); ComfyUI's system_stats gives vram_total/free (bytes). Prefer NVML.
  const total = g.memory_total || g.vram_total || 0;
  const used = g.memory_used ?? (g.vram_total ? g.vram_total - (g.vram_free ?? 0) : 0);
  const frac = total ? used / total : 0;
  return (
    <Card title="GPU">
      {g.name ? (
        <div className="space-y-3">
          <div className="truncate text-sm font-medium" title={g.name}>
            {g.name}
          </div>
          <div>
            <div className="mb-1 flex justify-between text-xs text-muted">
              <span>VRAM</span>
              <span className="tabular-nums">
                {(mb(used) / 1024).toFixed(1)} / {(mb(total) / 1024).toFixed(1)} GB
              </span>
            </div>
            <Progress value={frac} tone={frac > 0.92 ? "danger" : frac > 0.8 ? "warning" : "primary"} />
          </div>
          <div className="grid grid-cols-3 gap-2">
            <Stat label="Util" value={g.util_gpu !== undefined ? `${g.util_gpu}%` : "—"} />
            <Stat label="Nhiệt độ" value={g.temperature !== undefined ? `${g.temperature}°C` : "—"} />
            <Stat label="Công suất" value={g.power_w !== undefined ? `${Math.round(g.power_w)} W` : "—"} />
          </div>
          <div className="text-xs text-muted">
            Model đang nạp: <span className="font-mono">{st.loaded_model || "—"}</span>
            {g.driver && <> · driver {g.driver}</>}
          </div>
        </div>
      ) : (
        <Empty>Không đọc được thông tin GPU (chưa có NVML / ComfyUI chưa chạy).</Empty>
      )}
    </Card>
  );
}

function QueueCard({ st }: { st: AdminStatus }) {
  const { can } = useAuth();
  const q = st.queue;
  const act = useAction((a: "pause" | "resume") => api.post(`queue/${a}`), [["status"]]);
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          Queue {q.paused && <Badge tone="warning">đang tạm dừng</Badge>}
        </span>
      }
      actions={
        can("admin") &&
        (q.paused ? (
          <Button size="sm" onClick={() => act.mutate("resume")} loading={act.isPending}>
            Tiếp tục
          </Button>
        ) : (
          <Button size="sm" variant="outline" onClick={() => act.mutate("pause")} loading={act.isPending}>
            Tạm dừng
          </Button>
        ))
      }
    >
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Đang chờ (GPU)" value={q.queued} sub={q.queued_cpu ? `+${q.queued_cpu} CPU` : undefined} />
        <Stat label="Đang chạy" value={q.running} sub={q.running_cpu ? `+${q.running_cpu} CPU` : undefined} />
        <Stat label="Tối đa chờ" value={q.max_pending} />
        <Stat label="ETA queue" value={fmtDuration(q.eta_s)} />
      </div>
      <div className="mt-3">
        <Progress value={q.max_pending ? q.queued / q.max_pending : 0} tone={q.queued >= q.max_pending ? "danger" : "primary"} />
        <div className="mt-1 text-xs text-muted">
          Độ đầy queue {q.queued}/{q.max_pending}
        </div>
      </div>
      {act.error && <ErrorBox error={act.error} />}
    </Card>
  );
}

function RunningJob({ job, preview }: { job: Job; preview?: { b64: string; mime: string } | null }) {
  const { progress } = useLive();
  const p = progress[job.id];
  const pct = p?.pct ?? 0;
  return (
    <div className="flex gap-3 rounded-md border border-border p-3">
      <div className="checker flex h-20 w-20 shrink-0 items-center justify-center overflow-hidden rounded">
        {preview?.b64 ? <img src={`data:${preview.mime};base64,${preview.b64}`} alt="preview" className="h-full w-full object-contain" /> : <span className="text-[10px] text-muted">no preview</span>}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <Link to={`/jobs/${job.id}`} className="truncate font-mono text-xs text-primary hover:underline">
            {job.id}
          </Link>
          <JobStatusBadge status={job.status} />
        </div>
        <div className="mt-0.5 truncate text-sm">{jobLabel(job)}</div>
        <Progress value={pct} className="mt-2" />
        <div className="mt-1 flex justify-between text-xs text-muted">
          <span>
            {p?.op ?? p?.label ?? "…"}
            {p?.steps ? ` · step ${p.step}/${p.steps}` : ""}
            {p?.parts && p.parts > 1 ? ` · ảnh ${p.part}/${p.parts}` : ""}
          </span>
          <span className="tabular-nums">{Math.round(pct * 100)}%</span>
        </div>
      </div>
    </div>
  );
}

function StatsChart() {
  const [hours, setHours] = useState(24);
  const bucket = hours <= 6 ? 5 : hours <= 24 ? 30 : hours <= 168 ? 180 : 720;
  const stats = useStats(hours, bucket);
  const data = (stats.data?.data ?? []).map((b) => ({
    ...b,
    label: new Date(b.ts * 1000).toLocaleString("vi-VN", hours > 24 ? { day: "2-digit", month: "2-digit", hour: "2-digit" } : { hour: "2-digit", minute: "2-digit" }),
  }));
  return (
    <Card
      title="Hoạt động"
      actions={
        <Select value={hours} onChange={(e) => setHours(Number(e.target.value))} className="h-7 w-auto text-xs">
          <option value={6}>6 giờ</option>
          <option value={24}>24 giờ</option>
          <option value={168}>7 ngày</option>
          <option value={720}>30 ngày</option>
        </Select>
      }
    >
      {stats.isLoading ? (
        <Loading />
      ) : stats.error ? (
        <ErrorBox error={stats.error} />
      ) : !data.length ? (
        <Empty>Chưa có dữ liệu trong khoảng này.</Empty>
      ) : (
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={data} barCategoryGap="15%" margin={{ top: 5, right: 5, bottom: 0, left: -20 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
              <XAxis dataKey="label" tick={{ fontSize: 11, fill: "var(--muted)" }} stroke="var(--border)" minTickGap={24} />
              <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: "var(--muted)" }} stroke="var(--border)" />
              <Tooltip contentStyle={{ background: "var(--card)", border: "1px solid var(--border)", borderRadius: 6, fontSize: 12 }} />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Bar dataKey="jobs_done" name="Xong" stackId="a" fill="var(--success)" maxBarSize={48} />
              <Bar dataKey="jobs_failed" name="Lỗi" stackId="a" fill="var(--danger)" maxBarSize={48} />
              <Line dataKey="queue_depth" name="Queue (max)" stroke="var(--primary)" dot={false} strokeWidth={2} type="monotone" />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </Card>
  );
}

export default function Overview() {
  const status = useStatus();
  const st = status.data;
  if (status.isLoading) return <Loading />;
  if (!st) return <ErrorBox error={status.error ?? "Không có dữ liệu"} />;
  const c = st.comfy;
  return (
    <div className="space-y-4">
      <PageHeader
        title="Tổng quan"
        sub={
          <span className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-fg">{st.name}</span>
            <span className="font-mono text-xs">{st.node_id}</span>
            <span>v{st.version}</span>
            <span>· chạy {fmtDuration(st.uptime_s)}</span>
          </span>
        }
        actions={
          <div className="flex flex-wrap gap-1.5">
            <ComfyBadge state={c.state} warm={c.warm} />
            <Badge tone="primary">profile {st.profile}</Badge>
            {c.managed ? <Badge>managed</Badge> : <Badge>attach</Badge>}
          </div>
        }
      />
      {status.error && <ErrorBox error={errMsg(status.error)} />}
      {c.state !== "ready" && (
        <Alert tone={c.state === "starting" ? "warning" : "danger"} title={`ComfyUI ${c.state}`}>
          {c.last_error || "Engine chưa sẵn sàng; job GPU sẽ chờ tới khi ComfyUI chạy."}
          {c.restarts ? ` (đã restart ${c.restarts} lần)` : ""}
        </Alert>
      )}
      {c.missing_nodes?.length > 0 && <Alert tone="warning" title="Thiếu custom node">{c.missing_nodes.join(", ")}</Alert>}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <QueueCard st={st} />
        </div>
        <GpuCard st={st} />
      </div>

      <Card title={`Job đang chạy (${st.running_jobs.length})`}>
        {st.running_jobs.length ? (
          <div className="grid gap-3 md:grid-cols-2">
            {st.running_jobs.map((j) => (
              <RunningJob key={j.id} job={j} preview={st.preview?.[j.id]} />
            ))}
          </div>
        ) : (
          <Empty>Không có job nào đang chạy.</Empty>
        )}
      </Card>

      <Card title="24 giờ qua">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          <Stat label="Job xong" value={st.last_24h.done} />
          <Stat label="Job lỗi" value={<span className={st.last_24h.failed ? "text-danger" : ""}>{st.last_24h.failed}</span>} />
          <Stat label="Ảnh" value={st.last_24h.images} />
          <Stat label="GPU time" value={fmtDuration(st.last_24h.gpu_s)} />
          <Stat label="Lưu trữ" value={fmtBytes(st.storage_bytes)} sub={st.public_url || undefined} />
        </div>
      </Card>

      <StatsChart />
      <div className="text-right text-xs text-muted">Cập nhật {fmtTime(status.dataUpdatedAt / 1000, false)}</div>
    </div>
  );
}
