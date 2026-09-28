import { useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtGB, fmtTime } from "../lib/format";
import { useLive } from "../lib/live";
import { useAction, useBench, useModels } from "../lib/queries";
import type { BenchState, DownloadState, ModelEntry } from "../lib/types";
import { Alert, Badge, Button, Card, Checkbox, Code, ConfirmDialog, ErrorBox, Field, Input, Loading, PageHeader, Progress, Table } from "../components/ui";

function ModelState({ m, dl }: { m: ModelEntry; dl?: DownloadState }) {
  if (dl?.status === "running") {
    return (
      <div className="w-32">
        <Progress value={dl.total ? dl.done / dl.total : 0} />
        <div className="mt-0.5 text-[11px] text-muted tabular-nums">
          {fmtGB(dl.done)} / {fmtGB(dl.total)}
        </div>
      </div>
    );
  }
  return (
    <div className="flex flex-wrap gap-1">
      {m.present ? <Badge tone="success">có</Badge> : m.partial ? <Badge tone="warning">tải dở</Badge> : <Badge>chưa tải</Badge>}
      {m.active && <Badge tone="primary">đang dùng</Badge>}
      {dl?.status === "failed" && (
        <Badge tone="danger" title={dl.error ?? ""}>
          lỗi tải
        </Badge>
      )}
    </div>
  );
}

function Bench() {
  const { can } = useAuth();
  const [images, setImages] = useState("3");
  const [apply, setApply] = useState(true);
  const q = useBench();
  const st: BenchState | undefined = q.data;
  const running = st?.status === "running";
  const start = useAction((b: { images: number; apply: boolean }) => api.post<BenchState>("benchmark", b), [["bench"]]);
  return (
    <Card title="Benchmark" actions={st && <Badge tone={st.status === "done" ? "success" : st.status === "failed" ? "danger" : running ? "primary" : "neutral"}>{st.status}</Badge>}>
      <p className="mb-3 text-sm text-muted">
        Chạy vài ảnh thật để đo tốc độ GPU, rồi (tuỳ chọn) cập nhật cost model và <code>queue.max_pending</code>. Queue vẫn hoạt động; job benchmark xếp hàng như job thường.
      </p>
      {can("admin") && (
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Số ảnh (1–8)">
            <Input type="number" min={1} max={8} value={images} onChange={(e) => setImages(e.target.value)} className="w-24" />
          </Field>
          <Checkbox checked={apply} onChange={setApply} label="Áp dụng kết quả" />
          <Button loading={start.isPending} disabled={running} onClick={() => start.mutate({ images: Math.min(8, Math.max(1, Number(images) || 3)), apply })}>
            Chạy benchmark
          </Button>
        </div>
      )}
      <ErrorBox error={start.error} />
      {running && <div className="mt-3 text-sm">Đang chạy: {st?.step || "…"}</div>}
      {st?.error && <Alert tone="danger" className="mt-3">{st.error}</Alert>}
      {st?.result && (
        <div className="mt-3">
          <div className="mb-1 text-xs text-muted">Kết quả ({fmtTime(st.finished_at)})</div>
          <Code>{JSON.stringify(st.result, null, 2)}</Code>
        </div>
      )}
    </Card>
  );
}

export default function Models() {
  const q = useModels();
  const { can } = useAuth();
  const { downloads: live } = useLive();
  const [tier, setTier] = useState<string | null>(null);
  const dl = useAction((id: string) => api.post<DownloadState>(`models/${id}/download`), [["models"]]);
  const prof = useAction((t: string) => api.post<{ ok: boolean; restart_required: boolean; not_downloaded: string[] }>("models/profile", { tier: t }), [["models"]]);

  if (q.isLoading) return <Loading label="Đang đọc phần cứng và model…" />;
  if (!q.data) return <ErrorBox error={q.error} />;
  const d = q.data;
  const downloads = { ...d.downloads, ...live };
  const currentTier = String(d.current.tier ?? "");
  const gpu = d.hardware.gpus[0];

  return (
    <div className="space-y-4">
      <PageHeader title="Models" sub={<span className="font-mono text-xs">{d.root}</span>} />
      <Alert tone="warning" title="Giấy phép">
        Các model Qwen-Image 2.1 (turbo, base, text encoder, VAE) dùng <b>Qwen Research License — không dùng cho mục đích thương mại</b>. Real-ESRGAN (BSD-3) được dùng thương mại.
      </Alert>

      <Card title="Phần cứng">
        <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          <div>
            <div className="text-xs text-muted">GPU</div>
            {gpu ? `${gpu.name} · ${gpu.vram_gb} GB` : "Không có GPU NVIDIA"}
          </div>
          <div>
            <div className="text-xs text-muted">CPU / RAM</div>
            {d.hardware.cpu_cores} nhân · {d.hardware.ram_gb} GB
          </div>
          <div>
            <div className="text-xs text-muted">Ổ đĩa trống</div>
            {d.hardware.disk_free_gb} GB
          </div>
          <div>
            <div className="text-xs text-muted">Profile</div>
            hiện tại <b>{currentTier || "—"}</b> · đề xuất <b>{d.recommended}</b>
          </div>
        </div>
        {d.hardware.warnings?.map((w) => (
          <Alert key={w} tone="warning" className="mt-2">
            {w}
          </Alert>
        ))}
      </Card>

      <Card title="File model" bodyClass="p-0">
        <Table>
          <thead>
            <tr>
              <th>ID / file</th>
              <th>Vai trò</th>
              <th className="text-right">Dung lượng</th>
              <th>Trạng thái</th>
              <th>License</th>
              {can("admin") && <th />}
            </tr>
          </thead>
          <tbody>
            {d.models.map((m) => {
              const s = downloads[m.id];
              return (
                <tr key={m.id}>
                  <td className="max-w-72">
                    <div className="font-medium">{m.id}</div>
                    <div className="truncate font-mono text-[11px] text-muted" title={m.path}>
                      {m.file}
                    </div>
                    {m.note && <div className="text-[11px] text-muted">{m.note}</div>}
                  </td>
                  <td>{m.role}</td>
                  <td className="text-right tabular-nums">{fmtGB(m.bytes)}</td>
                  <td>
                    <ModelState m={m} dl={s} />
                  </td>
                  <td>
                    <div className="flex flex-wrap gap-1">
                      <Badge>{m.license}</Badge>
                      {!m.commercial_ok && <Badge tone="warning">phi thương mại</Badge>}
                    </div>
                  </td>
                  {can("admin") && (
                    <td className="text-right">
                      {!m.present && m.url && (
                        <Button size="sm" variant="outline" loading={s?.status === "running" || (dl.isPending && dl.variables === m.id)} onClick={() => dl.mutate(m.id)}>
                          {m.partial ? "Tải tiếp" : "Tải"}
                        </Button>
                      )}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </Table>
        {dl.error && (
          <div className="p-3">
            <ErrorBox error={dl.error} />
          </div>
        )}
      </Card>

      <Card title="Profile theo VRAM">
        {prof.data && (
          <Alert tone="warning" className="mb-3" title="Đã ghi profile vào config">
            Cần restart service để áp dụng.
            {prof.data.not_downloaded.length > 0 && <> Chưa tải: {prof.data.not_downloaded.join(", ")}.</>}
          </Alert>
        )}
        <ErrorBox error={prof.error} />
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {Object.entries(d.profiles).map(([t, p]) => (
            <div key={t} className={"rounded-md border p-3 " + (t === currentTier ? "border-primary" : "border-border")}>
              <div className="flex items-center justify-between gap-2">
                <div className="font-semibold">{t}</div>
                <div className="flex gap-1">
                  {t === currentTier && <Badge tone="primary">hiện tại</Badge>}
                  {t === d.recommended && <Badge tone="success">đề xuất</Badge>}
                </div>
              </div>
              <div className="mt-0.5 text-sm">{p.title}</div>
              <div className="mt-1 text-xs text-muted">
                {p.models.join(", ")} · {fmtGB(p.bytes)}
              </div>
              {can("admin") && t !== currentTier && (
                <Button size="sm" variant="outline" className="mt-2" onClick={() => setTier(t)}>
                  Áp dụng profile
                </Button>
              )}
            </div>
          ))}
        </div>
      </Card>

      <Bench />

      <ConfirmDialog
        open={tier !== null}
        title={`Áp dụng profile ${tier}?`}
        message="Profile được ghi vào file config (models, cấu hình ComfyUI). Service cần restart để dùng model mới."
        confirmLabel="Áp dụng"
        loading={prof.isPending}
        onClose={() => setTier(null)}
        onConfirm={() => tier && prof.mutate(tier, { onSettled: () => setTier(null) })}
      />
    </div>
  );
}
