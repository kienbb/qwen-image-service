import { useInfiniteQuery, useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";
import { api } from "./api";
import type {
  AdminStatus,
  ApiKey,
  AuditRow,
  BenchState,
  Job,
  JobList,
  ModelsResponse,
  Presets,
  Role,
  SettingsResponse,
  StatsBucket,
  SystemInfo,
  User,
} from "./types";

export const useStatus = (fast = false) =>
  useQuery({
    queryKey: ["status"],
    queryFn: () => api.get<AdminStatus>("status"),
    refetchInterval: (q) => (fast || (q.state.data?.running_jobs?.length ?? 0) > 0 ? 2000 : 15000),
  });

export const useStats = (hours: number, bucketMin: number) =>
  useQuery({
    queryKey: ["stats", hours, bucketMin],
    queryFn: () => api.get<{ bucket_s: number; data: StatsBucket[] }>("stats", { query: { hours, bucket_min: bucketMin } }),
    refetchInterval: 60000,
  });

export const useJobs = (status: string) =>
  useInfiniteQuery({
    queryKey: ["jobs", "list", status],
    initialPageParam: "" as string,
    queryFn: ({ pageParam }) => api.get<JobList>("jobs", { query: { status: status || undefined, limit: 50, before: pageParam || undefined } }),
    getNextPageParam: (last) => (last.has_more && last.data.length ? last.data[last.data.length - 1].id : undefined),
  });

export const useJob = (id: string | undefined) =>
  useQuery({
    queryKey: ["jobs", "one", id],
    queryFn: () => api.get<Job>(`jobs/${id}`),
    enabled: !!id,
    refetchInterval: (q) => (q.state.data && ["queued", "running"].includes(q.state.data.status) ? 2000 : false),
  });

export const usePresets = () => useQuery({ queryKey: ["presets"], queryFn: () => api.get<Presets>("presets"), staleTime: 60000 });
export const useSettings = () => useQuery({ queryKey: ["settings"], queryFn: () => api.get<SettingsResponse>("settings") });
export const useKeys = () => useQuery({ queryKey: ["keys"], queryFn: () => api.get<{ data: ApiKey[]; scopes: string[] }>("keys") });
export const useUsers = () => useQuery({ queryKey: ["users"], queryFn: () => api.get<{ data: User[]; roles: Role[] }>("users") });
export const useModels = () => useQuery({ queryKey: ["models"], queryFn: () => api.get<ModelsResponse>("models") });
export const useBench = (poll: boolean) =>
  useQuery({ queryKey: ["bench"], queryFn: () => api.get<BenchState>("benchmark"), refetchInterval: poll ? 2000 : false });
export const useSystemInfo = () => useQuery({ queryKey: ["system"], queryFn: () => api.get<SystemInfo>("system/info"), refetchInterval: 10000 });
export const useAudit = (limit: number, actor: string) =>
  useQuery({ queryKey: ["audit", limit, actor], queryFn: () => api.get<{ data: AuditRow[] }>("audit", { query: { limit, actor_name: actor || undefined } }) });
export const useLogs = (source: string, level: string, lines: number, auto: boolean) =>
  useQuery({
    queryKey: ["logs", source, level, lines],
    queryFn: () => api.get<{ lines: string[]; path: string }>("system/logs", { query: { source, level: level || undefined, lines } }),
    refetchInterval: auto ? 3000 : false,
  });

/** Mutation that invalidates the given query keys on success. */
export function useAction<TVars, TRes = unknown>(fn: (v: TVars) => Promise<TRes>, invalidate: QueryKey[] = []) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      for (const k of invalidate) void qc.invalidateQueries({ queryKey: k });
    },
  });
}
