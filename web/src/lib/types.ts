// Shapes returned by the gateway's /admin/api (see qis/api/admin.py). Fields the dashboard does not
// use are left out or typed loosely on purpose.

export type Role = "admin" | "operator" | "viewer";

export interface User {
  id: string;
  username: string;
  email: string | null;
  role: Role;
  disabled: number | boolean;
  must_change_pw: number | boolean;
  failed_logins?: number;
  locked_until?: number | null;
  created_at?: number | null;
  last_login_at?: number | null;
}

export interface AuthResponse {
  user: User;
  csrf: string;
}

export interface Bootstrap {
  needs_setup: boolean;
  setup_allowed_here: boolean;
  version: string;
  node: string;
}

export interface ComfyStatus {
  state: string;
  warm: boolean;
  managed: boolean;
  restarts: number;
  last_error: string | null;
  missing_nodes: string[];
  version: string;
  url?: string;
  pid?: number | null;
}

export interface GpuStatus {
  name?: string;
  vram_total?: number;
  vram_free?: number;
  memory_total?: number;
  memory_used?: number;
  util_gpu?: number;
  temperature?: number;
  power_w?: number;
  driver?: string;
}

export interface QueueStats {
  queued: number;
  queued_cpu: number;
  running: number;
  running_cpu: number;
  max_pending: number;
  eta_s: number;
  paused: boolean;
}

export interface NodeStatus {
  node_id: string;
  name: string;
  version: string;
  uptime_s: number;
  comfy: ComfyStatus;
  gpu: GpuStatus;
  queue: QueueStats;
  loaded_model: string | null;
}

export interface Preview {
  b64: string;
  mime: string;
  op_index?: number;
}

export interface AdminStatus extends NodeStatus {
  last_24h: { done: number; failed: number; gpu_s: number; images: number };
  storage_bytes: number;
  public_url: string;
  profile: string;
  running_jobs: Job[];
  preview: Record<string, Preview | null | undefined>;
}

export interface StatsBucket {
  ts: number;
  jobs_done: number;
  jobs_failed: number;
  gpu_s: number;
  queue_depth: number;
  images: number;
}

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "canceled" | "expired";

export interface ImageRef {
  file_id: string;
  url: string;
  width?: number | null;
  height?: number | null;
  has_alpha?: boolean;
}

export interface JobOutput {
  step: number;
  op: string;
  images: ImageRef[];
  extra: Record<string, ImageRef[]>;
  text?: string | null;
  data?: Record<string, unknown>;
}

export interface JobTiming {
  op: string;
  seconds: number;
  estimate: number;
  kind: string;
}

export interface JobStep {
  op: string;
  images: ImageRef[];
  extra?: Record<string, string[]>;
  data?: Record<string, unknown>;
}

export interface JobResult {
  outputs: JobOutput[];
  timings?: JobTiming[];
  degraded?: boolean;
  steps?: JobStep[];
}

export interface JobError {
  message?: string;
  code?: string;
  [k: string]: unknown;
}

export interface JobEvent {
  seq: number;
  ts: number;
  type: string;
  data: Record<string, unknown> | null;
}

export interface Job {
  id: string;
  object?: "job";
  status: JobStatus;
  kind: string;
  preset: string | null;
  priority: string;
  attempts: number;
  created_at: number | null;
  started_at: number | null;
  finished_at: number | null;
  cost_s: number | null;
  error: JobError | null;
  result: JobResult | null;
  progress: Record<string, unknown> | null;
  degraded: boolean;
  spec: Record<string, unknown> | null;
  position?: number;
  eta_s?: number;
  key_name?: string | null;
  api_key_id?: string | null;
  events?: JobEvent[];
}

export interface JobList {
  data: Job[];
  has_more: boolean;
  queue: QueueStats;
}

export interface Presets {
  presets: Record<string, { description?: string; ops: Record<string, unknown>[] }>;
  available: string[];
  path: string;
}

export interface JsonSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  enum?: unknown[];
  properties?: Record<string, JsonSchema>;
  additionalProperties?: JsonSchema | boolean;
  anyOf?: JsonSchema[];
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  pattern?: string;
  [k: string]: unknown;
}

export interface SettingsResponse {
  values: Record<string, Record<string, unknown>>;
  schema: JsonSchema & { "x-restart-required"?: string[]; "x-scopes"?: Record<string, string> };
  config: {
    source: string | null;
    server: Record<string, unknown>;
    comfy: Record<string, unknown>;
    profile: Record<string, unknown>;
    data_dir: string;
    comfyui: string;
  };
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  owner_user_id: string | null;
  scopes: string[];
  priority: string;
  max_pending: number | null;
  rpm: number | null;
  daily_images: number | null;
  allowed_models: string[] | null;
  created_at: number | null;
  last_used_at: number | null;
  expires_at: number | null;
  revoked_at: number | null;
  jobs_24h?: number;
  failed_24h?: number;
}

export interface ModelEntry {
  id: string;
  role: string;
  file: string;
  dir: string;
  url: string;
  bytes: number;
  min_vram_gb?: number;
  license: string;
  commercial_ok: boolean;
  note?: string;
  path: string;
  present: boolean;
  partial: boolean;
  size_on_disk?: number;
  active: boolean;
}

export interface DownloadState {
  status: "running" | "done" | "failed";
  done: number;
  total: number;
  error: string | null;
  started_at?: number;
}

export interface ModelsResponse {
  models: ModelEntry[];
  profiles: Record<string, { title: string; models: string[]; bytes: number }>;
  current: Record<string, unknown> & { tier?: string };
  recommended: string;
  hardware: {
    os: string;
    arch: string;
    cpu_cores: number;
    ram_gb: number;
    disk_free_gb: number;
    gpus: { index: number; name: string; vram_mb: number; vram_gb: number; driver: string; compute_cap: string }[];
    warnings: string[];
  };
  downloads: Record<string, DownloadState>;
  root: string;
}

export interface BenchState {
  status: "idle" | "running" | "done" | "failed";
  step?: string;
  started_at?: number;
  finished_at?: number;
  error?: string | null;
  result?: Record<string, unknown> | null;
}

export interface AuditRow {
  id: string;
  ts: number;
  actor: string;
  action: string;
  target: string | null;
  data: unknown;
  ip: string | null;
}

export interface SystemInfo {
  version: string;
  node_id: string;
  name: string;
  config: string | null;
  data_dir: string;
  comfy: ComfyStatus;
  uptime_s: number;
}
