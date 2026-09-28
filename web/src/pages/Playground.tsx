import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router";
import { api, API_BASE, errMsg } from "../lib/api";
import { usePresets } from "../lib/queries";
import { curlSnippet, pythonSnippet, type PlaygroundRequest } from "../lib/snippets";
import type { Job } from "../lib/types";
import { ImageGrid, JobStatusBadge } from "../components/common";
import { Alert, Badge, Button, Card, Checkbox, Code, CopyButton, ErrorBox, Field, Input, Loading, PageHeader, Progress, Select, Tabs, Textarea } from "../components/ui";

interface Upload {
  id: string;
  width: number;
  height: number;
  name: string;
  src: string;
}

interface RunState {
  jobId: string;
  status: string;
  pct: number;
  label: string;
  preview: string | null;
  log: { t: number; type: string; text: string }[];
  job: Job | null;
  error: string | null;
}

const SIZES = ["auto", "1024x1024", "768x768", "1328x1328", "1024x768", "768x1024", "1536x1024", "1024x1536", "1664x928", "928x1664"];
const PALETTES = ["adaptive", "pico8", "sweetie16", "db32"];
const TERMINAL = ["succeeded", "failed", "canceled", "expired"];

const PIPELINE_TEMPLATE = JSON.stringify(
  {
    ops: [
      { op: "generate", prompt: "a cute slime monster, game asset", mode: "turbo", size: 768, template: "asset", style: "16bit", n: 2 },
      { op: "pixelize", image: "$prev", grid: 64, colors: 24 },
    ],
    outputs: ["$step[0]", "$step[1]"],
  },
  null,
  2,
);

const EDIT_TEMPLATE = JSON.stringify(
  {
    inputs: { ref: "<file id từ ô tải ảnh>" },
    ops: [{ op: "generate", prompt: "make the character wear a red scarf", refs: ["$inputs.ref"], mode: "turbo" }],
  },
  null,
  2,
);

function numOrUndef(s: string): number | undefined {
  if (s.trim() === "") return undefined;
  const n = Number(s);
  return Number.isFinite(n) ? n : undefined;
}

export default function Playground() {
  const presets = usePresets();
  const [mode, setMode] = useState<"preset" | "pipeline">("preset");
  const [preset, setPreset] = useState("");
  const [prompt, setPrompt] = useState("");
  const [n, setN] = useState("1");
  const [size, setSize] = useState("auto");
  const [seed, setSeed] = useState("");
  const [steps, setSteps] = useState("");
  const [grid, setGrid] = useState("");
  const [palette, setPalette] = useState("");
  const [transparent, setTransparent] = useState(false);
  const [enhance, setEnhance] = useState(false);
  const [pipeline, setPipeline] = useState(PIPELINE_TEMPLATE);
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [uploading, setUploading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [run, setRun] = useState<RunState | null>(null);
  const [snip, setSnip] = useState<"curl" | "python">("curl");
  const esRef = useRef<EventSource | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const available = useMemo(() => presets.data?.available ?? [], [presets.data]);
  useEffect(() => {
    if (!preset && available.length) setPreset(available[0]);
  }, [available, preset]);
  useEffect(() => () => esRef.current?.close(), []);

  const presetDef = presets.data?.presets[preset];
  const hasPixelize = !!presetDef?.ops.some((o) => o.op === "pixelize");

  // -------- request built from the form (also feeds the curl / Python snippets)
  const built = useMemo((): { req: PlaygroundRequest | null; error: string | null } => {
    if (mode === "pipeline") {
      try {
        const body = JSON.parse(pipeline) as Record<string, unknown>;
        if (!body || typeof body !== "object" || !Array.isArray(body.ops)) return { req: null, error: "JSON phải có mảng ops[]" };
        return { req: { kind: "pipeline", body }, error: null };
      } catch (e) {
        return { req: null, error: `JSON không hợp lệ: ${(e as Error).message}` };
      }
    }
    if (!preset) return { req: null, error: "Chọn preset" };
    const params: Record<string, unknown> = {};
    const nn = numOrUndef(n);
    if (nn && nn !== 1) params.n = nn;
    if (size && size !== "auto") params.size = size;
    if (transparent) params.transparent = true;
    const sd = numOrUndef(seed);
    if (sd !== undefined) params.seed = sd;
    const st = numOrUndef(steps);
    if (st !== undefined) params.steps = st;
    if (enhance) params.enhance = true;
    if (hasPixelize) {
      const g = numOrUndef(grid);
      if (g !== undefined) params.grid = g;
      if (palette) params.palette = palette;
    }
    if (uploads.length) params.refs = uploads.map((u) => u.id);
    return { req: { kind: "preset", preset, prompt, params }, error: null };
  }, [mode, pipeline, preset, prompt, n, size, transparent, seed, steps, enhance, hasPixelize, grid, palette, uploads]);

  async function onFiles(files: FileList | null) {
    if (!files?.length) return;
    setUploading(true);
    setErr(null);
    try {
      for (const f of Array.from(files)) {
        const form = new FormData();
        form.append("file", f);
        const r = await api.upload<{ id: string; width: number; height: number }>("playground/files", form);
        setUploads((u) => [...u, { ...r, name: f.name, src: URL.createObjectURL(f) }]);
      }
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  function follow(jobId: string) {
    esRef.current?.close();
    const add = (type: string, text: string) =>
      setRun((r) => (r && r.jobId === jobId ? { ...r, log: [...r.log.slice(-200), { t: Date.now(), type, text }] } : r));
    const finish = async () => {
      esRef.current?.close();
      esRef.current = null;
      try {
        const job = await api.get<Job>(`jobs/${jobId}`);
        setRun((r) => (r && r.jobId === jobId ? { ...r, job, status: job.status, pct: job.status === "succeeded" ? 1 : r.pct, error: job.error?.message ?? null } : r));
      } catch (e) {
        setRun((r) => (r ? { ...r, error: errMsg(e) } : r));
      }
    };
    const es = new EventSource(`${API_BASE}/playground/jobs/${jobId}/events`, { withCredentials: true });
    esRef.current = es;
    const on = (type: string, fn: (d: Record<string, unknown>) => void) =>
      es.addEventListener(type, (ev) => {
        try {
          fn(JSON.parse((ev as MessageEvent).data));
        } catch {
          /* ignore */
        }
      });
    on("queued", (d) => {
      setRun((r) => (r ? { ...r, status: "queued", label: `vị trí ${d.position ?? "?"}` } : r));
      add("queued", `vị trí ${d.position ?? "?"}, ETA ${d.eta_s ?? "?"} s`);
    });
    on("started", () => {
      setRun((r) => (r ? { ...r, status: "running", label: "đang chạy (nạp model nếu cần)…" } : r));
      add("started", "");
    });
    on("op_started", (d) => {
      setRun((r) => (r ? { ...r, label: `#${d.op_index} ${d.op}…` } : r));
      add("op_started", `#${d.op_index} ${d.op}`);
    });
    on("progress", (d) =>
      setRun((r) =>
        r ? { ...r, status: "running", pct: Number(d.pct ?? r.pct), label: `${d.op ?? ""} step ${d.step}/${d.steps}${Number(d.parts) > 1 ? ` · ảnh ${d.part}/${d.parts}` : ""}` } : r,
      ),
    );
    on("preview", (d) => setRun((r) => (r ? { ...r, preview: `data:${d.mime};base64,${d.b64}` } : r)));
    on("op_done", (d) => add("op_done", `#${d.op_index} ${d.op} · ${d.seconds} s`));
    for (const t of TERMINAL) {
      on(t, (d) => {
        const e = d.error as { message?: string } | undefined;
        add(t, e?.message ?? "");
        setRun((r) => (r ? { ...r, status: t } : r));
        void finish();
      });
    }
    es.onerror = () => {
      // stream closed (terminal event already handled) or network error: fall back to one fetch
      if (esRef.current === es) void finish();
    };
  }

  async function submit() {
    if (!built.req) return;
    if (built.req.kind === "preset" && !prompt.trim()) return setErr("Nhập prompt");
    setErr(null);
    setSubmitting(true);
    try {
      const body = built.req.kind === "preset" ? { preset: built.req.preset, prompt: built.req.prompt, params: built.req.params } : built.req.body;
      const job = await api.post<Job>("playground/jobs", body);
      setRun({ jobId: job.id, status: job.status, pct: 0, label: "", preview: null, log: [], job: null, error: null });
      follow(job.id);
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setSubmitting(false);
    }
  }

  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const snippet = built.req ? (snip === "curl" ? curlSnippet(built.req, origin) : pythonSnippet(built.req, origin)) : "";
  const images = run?.job?.result?.outputs.flatMap((o) => o.images) ?? [];
  const extras = run?.job?.result?.outputs.flatMap((o) => Object.values(o.extra ?? {}).flat()) ?? [];

  if (presets.isLoading) return <Loading />;

  return (
    <div>
      <PageHeader title="Playground" sub="Chạy thử preset hoặc pipeline; job chạy dưới tên dashboard (không cần API key)." />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="min-w-0 space-y-4">
          <Card
            title={
              <Tabs
                value={mode}
                onChange={setMode}
                items={[
                  { id: "preset", label: "Preset" },
                  { id: "pipeline", label: "Pipeline JSON (nâng cao)" },
                ]}
              />
            }
          >
            {mode === "preset" ? (
              <div className="space-y-3">
                {!available.length && <Alert tone="warning">Node này chưa có preset nào dùng được (thiếu model?).</Alert>}
                <Field label="Preset" hint={presetDef?.description}>
                  <Select value={preset} onChange={(e) => setPreset(e.target.value)}>
                    {available.map((p) => (
                      <option key={p} value={p}>
                        {p}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Prompt">
                  <Textarea rows={4} value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="a red dragon guarding a treasure chest, game asset" />
                </Field>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <Field label="Số ảnh (n)">
                    <Input type="number" min={1} max={16} value={n} onChange={(e) => setN(e.target.value)} />
                  </Field>
                  <Field label="Kích thước">
                    <Input list="pg-sizes" value={size} onChange={(e) => setSize(e.target.value)} />
                    <datalist id="pg-sizes">
                      {SIZES.map((s) => (
                        <option key={s} value={s} />
                      ))}
                    </datalist>
                  </Field>
                  <Field label="Seed">
                    <Input type="number" min={0} value={seed} onChange={(e) => setSeed(e.target.value)} placeholder="ngẫu nhiên" />
                  </Field>
                  <Field label="Steps">
                    <Input type="number" min={0} max={100} value={steps} onChange={(e) => setSteps(e.target.value)} placeholder="mặc định" />
                  </Field>
                  {hasPixelize && (
                    <>
                      <Field label="Grid (px)">
                        <Input type="number" min={8} max={512} value={grid} onChange={(e) => setGrid(e.target.value)} placeholder="theo preset" />
                      </Field>
                      <Field label="Palette">
                        <Select value={palette} onChange={(e) => setPalette(e.target.value)}>
                          <option value="">theo preset</option>
                          {PALETTES.map((p) => (
                            <option key={p}>{p}</option>
                          ))}
                        </Select>
                      </Field>
                    </>
                  )}
                </div>
                <div className="flex flex-wrap gap-4">
                  <Checkbox checked={transparent} onChange={setTransparent} label="Nền trong suốt" />
                  <Checkbox checked={enhance} onChange={setEnhance} label="Enhance prompt (chậm hơn ~2 phút)" />
                </div>
              </div>
            ) : (
              <div className="space-y-2">
                <div className="flex flex-wrap gap-2">
                  <Button size="sm" variant="outline" onClick={() => setPipeline(PIPELINE_TEMPLATE)}>
                    Mẫu: generate → pixelize
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => setPipeline(EDIT_TEMPLATE.replace("<file id từ ô tải ảnh>", uploads[0]?.id ?? "<file id từ ô tải ảnh>"))}>
                    Mẫu: edit với ảnh tham chiếu
                  </Button>
                </div>
                <Textarea rows={16} className="font-mono text-xs" spellCheck={false} value={pipeline} onChange={(e) => setPipeline(e.target.value)} />
                <div className="text-xs text-muted">
                  Body giống POST /v1/jobs: <code>ops[]</code>, <code>inputs</code> (tên → file id), <code>outputs</code>. Tham chiếu ảnh: <code>$prev</code>, <code>$step[i]</code>,{" "}
                  <code>$inputs.name</code>.
                </div>
                {built.error && <Alert tone="danger">{built.error}</Alert>}
              </div>
            )}
          </Card>

          <Card title="Ảnh tham chiếu" actions={<Badge>{uploads.length} ảnh</Badge>}>
            <input ref={fileRef} type="file" accept="image/*" multiple className="hidden" onChange={(e) => void onFiles(e.target.files)} />
            <div className="flex flex-wrap items-start gap-2">
              {uploads.map((u) => (
                <div key={u.id} className="w-24 text-[10px]">
                  <div className="checker relative h-24 w-24 overflow-hidden rounded border border-border">
                    <img src={u.src} alt={u.name} className="h-full w-full object-contain" />
                    <button
                      className="absolute right-0.5 top-0.5 rounded bg-black/60 px-1 text-white"
                      onClick={() => setUploads((x) => x.filter((y) => y.id !== u.id))}
                      aria-label="Bỏ ảnh"
                    >
                      ✕
                    </button>
                  </div>
                  <button className="mt-0.5 block w-full truncate font-mono text-muted hover:text-fg" title="Copy file id" onClick={() => void navigator.clipboard?.writeText(u.id)}>
                    {u.id}
                  </button>
                </div>
              ))}
              <Button variant="outline" className="h-24 w-24 justify-center" loading={uploading} onClick={() => fileRef.current?.click()}>
                + Tải ảnh
              </Button>
            </div>
            <div className="mt-2 text-xs text-muted">
              {mode === "preset" ? "Ở chế độ preset, ảnh được gửi làm refs (edit / multi-reference, tối đa 3)." : "Dùng file id trong inputs hoặc refs của pipeline."}
            </div>
          </Card>

          {err && <ErrorBox error={err} />}
          <div className="flex gap-2">
            <Button onClick={() => void submit()} loading={submitting} disabled={!built.req}>
              Chạy
            </Button>
            {run && !TERMINAL.includes(run.status) && (
              <Button variant="outline" onClick={() => void api.post(`jobs/${run.jobId}/cancel`).catch((e) => setErr(errMsg(e)))}>
                Huỷ job
              </Button>
            )}
          </div>

          <Card
            title="Gọi từ code"
            actions={
              <>
                <Tabs
                  value={snip}
                  onChange={setSnip}
                  items={[
                    { id: "curl", label: "curl" },
                    { id: "python", label: "Python" },
                  ]}
                />
                {snippet && <CopyButton text={snippet} />}
              </>
            }
          >
            {snippet ? <Code className="max-h-80 whitespace-pre">{snippet}</Code> : <div className="text-sm text-muted">{built.error}</div>}
            <div className="mt-2 text-xs text-muted">
              Đặt biến môi trường <code>QIS_API_KEY</code> bằng một key tạo ở trang <Link to="/keys" className="text-primary hover:underline">API Keys</Link>.
            </div>
          </Card>
        </div>

        <div className="min-w-0 space-y-4">
          <Card
            title="Kết quả"
            actions={
              run && (
                <>
                  <JobStatusBadge status={run.status} />
                  <Link to={`/jobs/${run.jobId}`} className="font-mono text-xs text-primary hover:underline">
                    {run.jobId}
                  </Link>
                </>
              )
            }
          >
            {!run ? (
              <div className="py-12 text-center text-sm text-muted">Chưa chạy job nào.</div>
            ) : (
              <div className="space-y-3">
                {!TERMINAL.includes(run.status) && (
                  <div>
                    <Progress value={run.pct} />
                    <div className="mt-1 flex justify-between text-xs text-muted">
                      <span>{run.label || run.status}</span>
                      <span>{Math.round(run.pct * 100)}%</span>
                    </div>
                  </div>
                )}
                {run.preview && !run.job && (
                  <div className="checker mx-auto flex aspect-square max-w-sm items-center justify-center overflow-hidden rounded">
                    <img src={run.preview} alt="preview" className="max-h-full max-w-full object-contain" />
                  </div>
                )}
                {run.error && <Alert tone="danger">{run.error}</Alert>}
                {images.length > 0 && <ImageGrid images={images} />}
                {extras.length > 0 && (
                  <div>
                    <div className="mb-1 text-xs text-muted">Preview / ảnh phụ</div>
                    <ImageGrid images={extras} small />
                  </div>
                )}
                {run.job?.result?.timings && (
                  <div className="flex flex-wrap gap-1.5">
                    {run.job.result.timings.map((t, i) => (
                      <Badge key={i}>
                        {t.op}: {t.seconds.toFixed(1)} s
                      </Badge>
                    ))}
                  </div>
                )}
                {run.log.length > 0 && (
                  <details>
                    <summary className="cursor-pointer text-xs text-muted">Events ({run.log.length})</summary>
                    <ol className="mt-1 space-y-0.5 font-mono text-[11px]">
                      {run.log.map((l, i) => (
                        <li key={i}>
                          <span className="text-muted">{new Date(l.t).toLocaleTimeString("vi-VN")}</span> {l.type} {l.text}
                        </li>
                      ))}
                    </ol>
                  </details>
                )}
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}
