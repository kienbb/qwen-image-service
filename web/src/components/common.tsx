import { useState } from "react";
import { Badge, Lightbox } from "./ui";
import type { ImageRef, JobStatus } from "../lib/types";

const JOB_TONE: Record<string, "neutral" | "success" | "danger" | "warning" | "info" | "primary"> = {
  queued: "info",
  running: "primary",
  succeeded: "success",
  failed: "danger",
  canceled: "neutral",
  expired: "warning",
};
const JOB_LABEL: Record<string, string> = {
  queued: "chờ",
  running: "đang chạy",
  succeeded: "xong",
  failed: "lỗi",
  canceled: "đã huỷ",
  expired: "hết hạn",
};

export function JobStatusBadge({ status }: { status: JobStatus | string }) {
  return <Badge tone={JOB_TONE[status] ?? "neutral"}>{JOB_LABEL[status] ?? status}</Badge>;
}

export function ComfyBadge({ state, warm }: { state?: string; warm?: boolean }) {
  const tone = state === "ready" || state === "running" ? "success" : state === "starting" || state === "restarting" ? "warning" : "danger";
  const label = state === "ready" || state === "running" ? (warm ? "sẵn sàng (warm)" : "sẵn sàng") : state || "không rõ";
  return <Badge tone={tone}>ComfyUI: {label}</Badge>;
}

export function ImageGrid({ images, small }: { images: (Pick<ImageRef, "url"> & Partial<ImageRef>)[]; small?: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!images.length) return null;
  return (
    <>
      <div className={small ? "grid grid-cols-3 gap-2 sm:grid-cols-4 lg:grid-cols-6" : "grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4"}>
        {images.map((im, i) => (
          <button key={im.file_id ?? i} type="button" onClick={() => setOpen(im.url)} className="group overflow-hidden rounded-md border border-border text-left">
            <div className="checker flex aspect-square items-center justify-center">
              <img
                src={im.url}
                alt=""
                loading="lazy"
                className={"max-h-full max-w-full object-contain " + ((im.width ?? 999) <= 256 ? "pixelated h-full w-full" : "")}
              />
            </div>
            {im.width ? (
              <div className="bg-card px-2 py-1 text-[11px] text-muted">
                {im.width}×{im.height}
              </div>
            ) : null}
          </button>
        ))}
      </div>
      <Lightbox src={open} onClose={() => setOpen(null)} />
    </>
  );
}
