import { useEffect, useRef, useState } from "react";
import { fmtTime } from "../lib/format";
import { useAudit, useLogs } from "../lib/queries";
import { Button, Card, Empty, ErrorBox, Input, Loading, PageHeader, Select, Switch, Table, Tabs, cn } from "../components/ui";

function levelClass(line: string): string {
  if (/ (ERROR|CRITICAL) /.test(line) || /Traceback|Exception/.test(line)) return "text-danger";
  if (/ WARNING /.test(line)) return "text-warning";
  if (/ DEBUG /.test(line)) return "text-muted";
  return "";
}

function LogView() {
  const [source, setSource] = useState("gateway");
  const [level, setLevel] = useState("");
  const [lines, setLines] = useState(300);
  const [auto, setAuto] = useState(true);
  const [filter, setFilter] = useState("");
  const q = useLogs(source, level, lines, auto);
  const box = useRef<HTMLDivElement>(null);
  const shown = (q.data?.lines ?? []).filter((l) => !filter || l.toLowerCase().includes(filter.toLowerCase()));

  useEffect(() => {
    const el = box.current;
    if (el && auto) el.scrollTop = el.scrollHeight;
  }, [q.data, auto]);

  return (
    <Card
      title={<span className="font-mono text-xs font-normal text-muted">{q.data?.path}</span>}
      actions={
        <>
          <Select value={source} onChange={(e) => setSource(e.target.value)} className="h-8 w-32" aria-label="Nguồn log">
            <option value="gateway">gateway</option>
            <option value="comfyui">comfyui</option>
          </Select>
          <Select value={level} onChange={(e) => setLevel(e.target.value)} className="h-8 w-32" aria-label="Mức log">
            <option value="">mọi mức</option>
            <option value="ERROR">ERROR</option>
            <option value="WARNING">WARNING</option>
            <option value="INFO">INFO</option>
            <option value="DEBUG">DEBUG</option>
          </Select>
          <Select value={lines} onChange={(e) => setLines(Number(e.target.value))} className="h-8 w-32" aria-label="Số dòng">
            {[100, 300, 1000, 3000].map((n) => (
              <option key={n} value={n}>
                {n} dòng
              </option>
            ))}
          </Select>
          <Input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Lọc…" className="h-8 w-36" />
          <Switch checked={auto} onChange={setAuto} label="Tự làm mới" />
          <Button size="sm" variant="outline" onClick={() => void q.refetch()} loading={q.isFetching && !auto}>
            Làm mới
          </Button>
        </>
      }
      bodyClass="p-0"
    >
      {q.isLoading ? (
        <div className="px-4">
          <Loading />
        </div>
      ) : q.error ? (
        <div className="p-4">
          <ErrorBox error={q.error} />
        </div>
      ) : !shown.length ? (
        <Empty>Không có dòng log nào.</Empty>
      ) : (
        <div ref={box} className="h-[65vh] overflow-auto bg-subtle/50 p-3 font-mono text-[11.5px] leading-5">
          {shown.map((l, i) => (
            <div key={i} className={cn("whitespace-pre-wrap break-all", levelClass(l))}>
              {l}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function AuditView() {
  const [actor, setActor] = useState("");
  const [limit, setLimit] = useState(200);
  const q = useAudit(limit, actor);
  return (
    <Card
      actions={
        <>
          <Input value={actor} onChange={(e) => setActor(e.target.value)} placeholder="Lọc theo actor" className="h-8 w-40" />
          <Select value={limit} onChange={(e) => setLimit(Number(e.target.value))} className="h-8 w-32">
            {[100, 200, 500, 2000].map((n) => (
              <option key={n} value={n}>
                {n} dòng
              </option>
            ))}
          </Select>
        </>
      }
      bodyClass="p-0"
    >
      {q.isLoading ? (
        <div className="px-4">
          <Loading />
        </div>
      ) : q.error ? (
        <div className="p-4">
          <ErrorBox error={q.error} />
        </div>
      ) : !q.data?.data.length ? (
        <Empty>Không có bản ghi.</Empty>
      ) : (
        <Table>
          <thead>
            <tr>
              <th>Thời gian</th>
              <th>Actor</th>
              <th>Hành động</th>
              <th>Đối tượng</th>
              <th>IP</th>
              <th>Dữ liệu</th>
            </tr>
          </thead>
          <tbody>
            {q.data.data.map((r) => (
              <tr key={r.id}>
                <td className="text-xs whitespace-nowrap text-muted">{fmtTime(r.ts)}</td>
                <td>{r.actor}</td>
                <td className="font-mono text-xs">{r.action}</td>
                <td className="max-w-40 truncate font-mono text-xs" title={r.target ?? ""}>
                  {r.target || "—"}
                </td>
                <td className="text-xs text-muted">{r.ip || "—"}</td>
                <td className="max-w-80">
                  {r.data && !(typeof r.data === "object" && !Array.isArray(r.data) && Object.keys(r.data as object).length === 0) ? (
                    <details>
                      <summary className="cursor-pointer truncate font-mono text-[11px] text-muted">{JSON.stringify(r.data).slice(0, 80)}</summary>
                      <pre className="mt-1 max-h-60 overflow-auto font-mono text-[11px]">{JSON.stringify(r.data, null, 2)}</pre>
                    </details>
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

export default function Logs() {
  const [tab, setTab] = useState<"logs" | "audit">("logs");
  return (
    <div className="space-y-3">
      <PageHeader
        title="Logs"
        actions={
          <Tabs
            value={tab}
            onChange={setTab}
            items={[
              { id: "logs", label: "Log file" },
              { id: "audit", label: "Audit" },
            ]}
          />
        }
      />
      {tab === "logs" ? <LogView /> : <AuditView />}
    </div>
  );
}
