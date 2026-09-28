import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtDuration, fmtTime, jobDuration, jobLabel } from "../lib/format";
import { useLive } from "../lib/live";
import { useAction, useJob } from "../lib/queries";
import type { Job, JobEvent } from "../lib/types";
import { ImageGrid, JobStatusBadge } from "../components/common";
import { Alert, Badge, Button, Card, Code, ConfirmDialog, CopyButton, Empty, ErrorBox, Loading, PageHeader, Progress, Tabs } from "../components/ui";

function eventSummary(ev: JobEvent): string {
  const d = ev.data ?? {};
  switch (ev.type) {
    case "queued":
      return `vị trí ${d.position ?? "?"}, ETA ${fmtDuration(d.eta_s as number)}, cost ${Math.round((d.cost_s as number) ?? 0)} s`;
    case "started":
      return `lần ${d.attempt ?? 1}${d.degraded ? " (degraded)" : ""}`;
    case "op_started":
      return `#${d.op_index} ${d.op} (${d.kind})`;
    case "progress":
      return `${d.op ?? ""} ${d.step ?? ""}/${d.steps ?? ""} · ${Math.round(((d.pct as number) ?? 0) * 100)}%`;
    case "op_done":
      return `#${d.op_index} ${d.op} xong trong ${d.seconds} s · ${(d.images as unknown[] | undefined)?.length ?? 0} ảnh`;
    case "failed":
    case "canceled":
    case "expired": {
      const e = d.error as { message?: string; code?: string } | undefined;
      return e ? `${e.code ?? ""} ${e.message ?? ""}` : "";
    }
    default:
      return "";
  }
}

function Timeline({ events }: { events: JobEvent[] }) {
  if (!events.length) return <Empty>Không có event.</Empty>;
  return (
    <ol className="space-y-1.5">
      {events.map((ev) => (
        <li key={ev.seq} className="flex gap-3 text-sm">
          <span className="w-20 shrink-0 text-xs tabular-nums text-muted">{fmtTime(ev.ts, false)}</span>
          <Badge tone={ev.type === "failed" ? "danger" : ev.type === "succeeded" ? "success" : "neutral"}>{ev.type}</Badge>
          <span className="min-w-0 break-words text-xs text-muted">{eventSummary(ev)}</span>
        </li>
      ))}
    </ol>
  );
}

function Outputs({ job }: { job: Job }) {
  const res = job.result;
  if (!res) return null;
  return (
    <div className="space-y-4">
      {res.outputs.map((o, i) => (
        <div key={i}>
          <div className="mb-2 flex items-center gap-2 text-sm">
            <Badge tone="primary">bước {o.step}</Badge> <span className="font-medium">{o.op}</span>
            <span className="text-xs text-muted">{o.images.length} ảnh</span>
          </div>
          <ImageGrid images={o.images} />
          {Object.entries(o.extra ?? {}).map(([name, imgs]) =>
            imgs.length ? (
              <div key={name} className="mt-2">
                <div className="mb-1 text-xs text-muted">{name}</div>
                <ImageGrid images={imgs} small />
              </div>
            ) : null,
          )}
          {o.text && (
            <details className="mt-2">
              <summary className="cursor-pointer text-xs text-muted">Prompt thực tế</summary>
              <Code className="mt-1 whitespace-pre-wrap">{o.text}</Code>
            </details>
          )}
        </div>
      ))}
    </div>
  );
}

export default function JobDetail() {
  const { id } = useParams();
  const q = useJob(id);
  const { can } = useAuth();
  const nav = useNavigate();
  const { progress } = useLive();
  const [tab, setTab] = useState<"out" | "steps" | "spec" | "events">("out");
  const [confirmCancel, setConfirmCancel] = useState(false);
  const cancel = useAction(() => api.post<Job>(`jobs/${id}/cancel`), [["jobs"]]);
  const retry = useAction(() => api.post<Job>(`jobs/${id}/retry`), [["jobs"]]);

  if (q.isLoading) return <Loading />;
  if (!q.data) return <ErrorBox error={q.error ?? "Không tìm thấy job"} />;
  const j = q.data;
  const live = progress[j.id];
  const active = j.status === "queued" || j.status === "running";
  const steps = j.result?.steps ?? [];

  return (
    <div className="space-y-4">
      <PageHeader
        title={
          <span className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-base">{j.id}</span>
            <JobStatusBadge status={j.status} />
            {j.degraded && <Badge tone="warning">degraded</Badge>}
          </span>
        }
        sub={
          <Link to="/jobs" className="hover:underline">
            ← Danh sách jobs
          </Link>
        }
        actions={
          can("operator") && (
            <>
              {active && (
                <Button variant="danger" onClick={() => setConfirmCancel(true)}>
                  Huỷ
                </Button>
              )}
              <Button
                variant="outline"
                loading={retry.isPending}
                onClick={() => retry.mutate(undefined, { onSuccess: (nj) => nav(`/jobs/${nj.id}`) })}
              >
                Chạy lại
              </Button>
            </>
          )
        }
      />
      <ErrorBox error={cancel.error || retry.error} />

      <Card>
        <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4 lg:grid-cols-6">
          <Info k="Loại" v={jobLabel(j)} />
          <Info k="Kind" v={j.kind} />
          <Info k="Key" v={j.api_key_id ? j.api_key_id : "dashboard"} mono />
          <Info k="Priority" v={j.priority} />
          <Info k="Tạo lúc" v={fmtTime(j.created_at)} />
          <Info k="Bắt đầu" v={fmtTime(j.started_at)} />
          <Info k="Kết thúc" v={fmtTime(j.finished_at)} />
          <Info k="Thời lượng" v={fmtDuration(jobDuration(j))} />
          <Info k="Cost ước tính" v={j.cost_s ? `${Math.round(j.cost_s)} s` : "—"} />
          <Info k="Số lần chạy" v={String(j.attempts)} />
          {j.status === "queued" && <Info k="Vị trí" v={`#${j.position ?? "?"} · ETA ${fmtDuration(j.eta_s)}`} />}
          {j.status === "running" && <Info k="ETA" v={fmtDuration(j.eta_s)} />}
        </div>
        {j.status === "running" && (
          <div className="mt-3">
            <Progress value={live?.pct ?? 0} />
            <div className="mt-1 text-xs text-muted">
              {live?.op ?? "…"} {live?.steps ? `step ${live.step}/${live.steps}` : ""}
            </div>
          </div>
        )}
        {j.result?.timings?.length ? (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {j.result.timings.map((t, i) => (
              <Badge key={i} title={`ước tính ${t.estimate}s`}>
                {t.op}: {t.seconds.toFixed(1)} s ({t.kind})
              </Badge>
            ))}
          </div>
        ) : null}
      </Card>

      {j.error && (
        <Alert tone="danger" title={`Lỗi: ${j.error.code ?? j.status}`}>
          {j.error.message}
        </Alert>
      )}

      <Tabs
        value={tab}
        onChange={setTab}
        items={[
          { id: "out", label: "Kết quả" },
          { id: "steps", label: `Từng bước (${steps.length})` },
          { id: "spec", label: "Spec" },
          { id: "events", label: `Events (${j.events?.length ?? 0})` },
        ]}
      />
      <Card>
        {tab === "out" && (j.result ? <Outputs job={j} /> : <Empty>{active ? "Job chưa xong." : "Không có kết quả."}</Empty>)}
        {tab === "steps" &&
          (steps.length ? (
            <div className="space-y-4">
              {steps.map((s, i) => (
                <div key={i}>
                  <div className="mb-1 text-sm">
                    <Badge>#{i}</Badge> <span className="font-medium">{s.op}</span>
                  </div>
                  {s.images.length ? <ImageGrid images={s.images} small /> : <div className="text-xs text-muted">(không có ảnh)</div>}
                  {s.data && Object.keys(s.data).length > 0 && (
                    <details className="mt-1">
                      <summary className="cursor-pointer text-xs text-muted">data</summary>
                      <Code className="mt-1">{JSON.stringify(s.data, null, 2)}</Code>
                    </details>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <Empty>Không có dữ liệu từng bước.</Empty>
          ))}
        {tab === "spec" && (
          <div>
            <div className="mb-2 flex justify-end">
              <CopyButton text={JSON.stringify(j.spec, null, 2)} label="Copy JSON" />
            </div>
            <Code className="max-h-[60vh]">{JSON.stringify(j.spec, null, 2)}</Code>
          </div>
        )}
        {tab === "events" && <Timeline events={j.events ?? []} />}
      </Card>

      <ConfirmDialog
        open={confirmCancel}
        title="Huỷ job?"
        message={`Job ${j.id} sẽ bị dừng. Ảnh đã tạo xong (nếu có) sẽ không được trả về.`}
        confirmLabel="Huỷ job"
        danger
        loading={cancel.isPending}
        onClose={() => setConfirmCancel(false)}
        onConfirm={() => cancel.mutate(undefined, { onSettled: () => setConfirmCancel(false) })}
      />
    </div>
  );
}

function Info({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <div className="text-xs text-muted">{k}</div>
      <div className={"truncate " + (mono ? "font-mono text-xs" : "")} title={v}>
        {v}
      </div>
    </div>
  );
}
