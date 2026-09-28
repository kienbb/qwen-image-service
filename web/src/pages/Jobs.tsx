import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { fmtDuration, fmtTime, jobDuration, jobLabel } from "../lib/format";
import { useLive } from "../lib/live";
import { useJobs } from "../lib/queries";
import { JobStatusBadge } from "../components/common";
import { Button, Card, Empty, ErrorBox, Loading, PageHeader, Progress, Select, Table } from "../components/ui";

const STATUSES = ["", "queued", "running", "succeeded", "failed", "canceled", "expired"];

export default function Jobs() {
  const [status, setStatus] = useState("");
  const q = useJobs(status);
  const nav = useNavigate();
  const { progress } = useLive();
  const rows = q.data?.pages.flatMap((p) => p.data) ?? [];
  const queue = q.data?.pages[0]?.queue;

  return (
    <div>
      <PageHeader
        title="Jobs"
        sub={queue && `Queue: ${queue.queued} chờ · ${queue.running} đang chạy${queue.paused ? " · tạm dừng" : ""}`}
        actions={
          <>
            <Select value={status} onChange={(e) => setStatus(e.target.value)} className="w-40" aria-label="Lọc trạng thái">
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s || "Tất cả trạng thái"}
                </option>
              ))}
            </Select>
            <Button variant="outline" onClick={() => void q.refetch()} loading={q.isRefetching}>
              Làm mới
            </Button>
          </>
        }
      />
      <Card bodyClass="p-0">
        {q.isLoading ? (
          <div className="px-4">
            <Loading />
          </div>
        ) : q.error ? (
          <div className="p-4">
            <ErrorBox error={q.error} />
          </div>
        ) : !rows.length ? (
          <Empty>Chưa có job nào.</Empty>
        ) : (
          <Table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Thời gian</th>
                <th>Key</th>
                <th>Loại / preset</th>
                <th>Trạng thái</th>
                <th className="text-right">Thời lượng</th>
                <th className="text-right">Cost</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((j) => {
                const p = progress[j.id];
                return (
                  <tr key={j.id} className="cursor-pointer hover:bg-subtle" onClick={() => nav(`/jobs/${j.id}`)}>
                    <td className="font-mono text-xs">
                      <Link to={`/jobs/${j.id}`} className="text-primary hover:underline" onClick={(e) => e.stopPropagation()}>
                        {j.id}
                      </Link>
                    </td>
                    <td className="whitespace-nowrap text-xs text-muted">{fmtTime(j.created_at)}</td>
                    <td className="max-w-40 truncate">{j.key_name ?? "—"}</td>
                    <td className="max-w-64">
                      <div className="truncate">{jobLabel(j)}</div>
                      <div className="text-xs text-muted">{j.kind}</div>
                    </td>
                    <td>
                      <JobStatusBadge status={j.status} />
                      {j.status === "running" && p?.pct !== undefined && <Progress value={p.pct} className="mt-1 h-1 w-20" />}
                      {j.status === "queued" && j.position ? <div className="mt-0.5 text-[11px] text-muted">#{j.position}</div> : null}
                    </td>
                    <td className="text-right tabular-nums">{fmtDuration(jobDuration(j))}</td>
                    <td className="text-right tabular-nums text-muted">{j.cost_s ? `${Math.round(j.cost_s)} s` : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>
      {q.hasNextPage && (
        <div className="mt-3 flex justify-center">
          <Button variant="outline" onClick={() => void q.fetchNextPage()} loading={q.isFetchingNextPage}>
            Tải thêm
          </Button>
        </div>
      )}
    </div>
  );
}
