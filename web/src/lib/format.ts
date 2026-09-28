export function fmtBytes(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (Math.abs(v) >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : digits)} ${units[i]}`;
}

export const fmtGB = (n: number | null | undefined) => (n ? `${(n / 1024 ** 3).toFixed(2)} GB` : "—");

export function fmtDuration(s: number | null | undefined): string {
  if (s === null || s === undefined || Number.isNaN(s)) return "—";
  if (s === 0) return "0 s";
  if (s < 1) return `${Math.round(s * 1000)} ms`;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${Math.round(s % 60)}s`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export function fmtTime(ts: number | null | undefined, withDate = true): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const time = d.toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  if (!withDate) return time;
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return time;
  return `${d.toLocaleDateString("vi-VN", { day: "2-digit", month: "2-digit" })} ${time}`;
}

export function fmtAgo(ts: number | null | undefined): string {
  if (!ts) return "—";
  const s = Date.now() / 1000 - ts;
  if (s < 0) return `sau ${fmtDuration(-s)}`;
  if (s < 60) return "vừa xong";
  return `${fmtDuration(s).split(" ")[0]} trước`;
}

export function jobDuration(j: { started_at: number | null; finished_at: number | null }): number | null {
  if (!j.started_at) return null;
  return (j.finished_at ?? Date.now() / 1000) - j.started_at;
}

export function jobLabel(j: { preset: string | null; kind: string; spec?: Record<string, unknown> | null }): string {
  if (j.preset) return j.preset;
  const ops = (j.spec?.ops as { op?: string }[] | undefined) ?? [];
  const chain = ops.map((o) => o.op).filter(Boolean).join(" → ");
  return chain || j.kind;
}
