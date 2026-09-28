import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { API_BASE } from "./api";
import type { AdminStatus, DownloadState, NodeStatus } from "./types";

// One EventSource on /admin/api/events for the whole app. It feeds:
//  - node.status  -> merged into the ["status"] query cache
//  - job.*        -> per-job progress map + (throttled) invalidation of job lists/status
//  - model.download -> download progress map

export interface JobProgress {
  pct?: number;
  step?: number;
  steps?: number;
  op?: string;
  label?: string;
  part?: number;
  parts?: number;
  type: string;
  ts: number;
}

interface LiveCtx {
  connected: boolean;
  progress: Record<string, JobProgress>;
  downloads: Record<string, DownloadState>;
}

const Ctx = createContext<LiveCtx>({ connected: false, progress: {}, downloads: {} });

export const JOB_EVENTS = ["queued", "started", "op_started", "progress", "op_done", "succeeded", "failed", "canceled", "expired", "retry"];

/** The first node.status event carries the status itself; later ones wrap it as {type, data}. Accept both. */
export function unwrapStatus(payload: unknown): NodeStatus | null {
  if (!payload || typeof payload !== "object") return null;
  const p = payload as Record<string, unknown>;
  if ("node_id" in p && "queue" in p) return p as unknown as NodeStatus;
  const d = p.data as Record<string, unknown> | undefined;
  if (d && typeof d === "object" && "node_id" in d) return d as unknown as NodeStatus;
  return null;
}

export function LiveProvider({ children, enabled }: { children: ReactNode; enabled: boolean }) {
  const qc = useQueryClient();
  const [connected, setConnected] = useState(false);
  const [progress, setProgress] = useState<Record<string, JobProgress>>({});
  const [downloads, setDownloads] = useState<Record<string, DownloadState>>({});
  const pending = useRef<number | null>(null);

  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") return;
    let es: EventSource | null = null;
    let retry: number | null = null;
    let closed = false;

    const invalidateSoon = () => {
      if (pending.current !== null) return;
      pending.current = window.setTimeout(() => {
        pending.current = null;
        void qc.invalidateQueries({ queryKey: ["jobs"] });
        void qc.invalidateQueries({ queryKey: ["status"] });
      }, 800);
    };

    const connect = () => {
      es = new EventSource(`${API_BASE}/events`, { withCredentials: true });
      es.onopen = () => setConnected(true);
      es.onerror = () => {
        setConnected(false);
        es?.close();
        if (!closed) retry = window.setTimeout(connect, 5000);
      };
      es.addEventListener("node.status", (ev) => {
        try {
          const st = unwrapStatus(JSON.parse((ev as MessageEvent).data));
          if (st) qc.setQueryData<AdminStatus>(["status"], (old) => (old ? { ...old, ...st } : old));
        } catch {
          /* ignore malformed */
        }
      });
      for (const t of JOB_EVENTS) {
        es.addEventListener(`job.${t}`, (ev) => {
          try {
            const msg = JSON.parse((ev as MessageEvent).data) as { job_id: string; data?: Record<string, unknown> };
            const d = (msg.data ?? {}) as Record<string, unknown>;
            setProgress((p) => {
              const next = { ...p };
              if (["succeeded", "failed", "canceled", "expired"].includes(t)) delete next[msg.job_id];
              else next[msg.job_id] = { ...(p[msg.job_id] ?? {}), ...(t === "progress" ? d : {}), type: t, ts: Date.now() } as JobProgress;
              return next;
            });
            if (t !== "progress") invalidateSoon();
          } catch {
            /* ignore */
          }
        });
      }
      es.addEventListener("model.download", (ev) => {
        try {
          const msg = JSON.parse((ev as MessageEvent).data) as { data: DownloadState & { id: string } };
          setDownloads((d) => ({ ...d, [msg.data.id]: msg.data }));
          if (msg.data.status !== "running") void qc.invalidateQueries({ queryKey: ["models"] });
        } catch {
          /* ignore */
        }
      });
    };
    connect();
    return () => {
      closed = true;
      if (retry !== null) clearTimeout(retry);
      es?.close();
      setConnected(false);
    };
  }, [enabled, qc]);

  return <Ctx.Provider value={{ connected, progress, downloads }}>{children}</Ctx.Provider>;
}

export const useLive = () => useContext(Ctx);
