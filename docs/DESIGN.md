# Qwen Image Service: thiết kế

> Trạng thái: **bản thiết kế v1**, chốt ngày 2026-09-29, trước khi bắt đầu implementation.
> Tên tạm: **Qwen Image Service**, CLI quản lý là `qis`.

## 0. Tóm tắt

Biến pipeline Qwen-Image 2.1 hiện có (CLI `gen.py` và các script trong `tools/`, chạy trên ComfyUI) thành **một service cài lên máy NVIDIA bất kỳ bằng một lệnh**. Service có các thành phần sau:

- **HTTP API** gọi được từ máy khác qua Internet (Cloudflare Tunnel), gồm:
  - lớp **tương thích OpenAI Images API**, để dùng được với OpenAI SDK có sẵn;
  - API native dạng job bất đồng bộ, có đủ các tool: generate, edit, refine, upscale, outpaint, pixelize, remove-bg, compose, fidelity và enhance prompt.
- **Queue** nhận nhiều request cùng lúc. Giới hạn queue được tính từ cấu hình máy lúc cài và chỉnh được lúc chạy.
- **Dashboard web** để quản lý setting, job, model, API key và user. Tạm thời đăng nhập bằng username hoặc email cùng password.
- **Installer** cho Windows (`irm | iex`) và Linux (`curl | bash`): tự detect GPU/VRAM/RAM/ổ đĩa, đề xuất bộ model (gồm cả upscaler) cho chất lượng tốt nhất với máy đó, rồi đăng ký service.
- Ngay từ đầu, mỗi máy được thiết kế như **một node**, để sau này ghép thành **mesh** nhiều node chia nhau xử lý request.

### Quyết định đã chốt

| # | Chủ đề | Quyết định |
|---|---|---|
| D1 | GPU | Giai đoạn đầu **chỉ NVIDIA** (CUDA). Không làm MPS, ROCm hay CPU inference. Chỉ giữ chế độ *tools-only* làm phương án dự phòng |
| D2 | Mạng | Expose ra Internet qua **Cloudflare Zero Trust Tunnel** (`cloudflared`). Gateway chỉ bind `127.0.0.1` |
| D3 | Quy mô | 1 máy, 1 GPU, 1 node. Mesh để vào roadmap (P5), nhưng P1 phải làm sẵn các điểm chuẩn bị cho mesh (§15) |
| D4 | Frontend | Dựng mới: React + Vite + TypeScript + Tailwind + shadcn/ui |
| D5 | Engine | **Giữ ComfyUI** làm engine inference, chạy thành process riêng do gateway giám sát |
| D6 | Lưu trữ | SQLite (chế độ WAL) cho metadata, filesystem cho ảnh. Không cần Redis hay Postgres cho một node |

### Không làm trong v1

- Mac, AMD, Intel GPU.
- Nhiều GPU trên một máy. Kiến trúc cho phép nhưng chưa làm.
- Mesh và đồng bộ user giữa các node (P5).
- Nhận workflow ComfyUI thô từ client (lý do bảo mật, xem §11).
- Billing.

---

## 1. Hiện trạng (điểm xuất phát)

| Thành phần | Vai trò | Ghi chú khi chuyển sang service |
|---|---|---|
| `gen.py` | Dựng workflow cho text→image, edit và multi-ref, sau đó pixel hoá; gọi ComfyUI bằng HTTP polling; enhance prompt có cache | `build_workflow`, `build_prompt`, `dims`, `enhance` chuyển sang `engine/workflows.py`. `gen.py` trở thành client của API |
| `tools/upscale.py` | ESRGAN (x2/x4), sau đó Lanczos về đúng kích thước đích | Trở thành op `upscale` |
| `tools/refine.py` | Hires-fix: resize rồi re-denoise nhẹ (turbo dùng sigma nodes, base dùng denoise) | Trở thành op `refine` |
| `tools/outpaint.py` | Đổi tỉ lệ khung: dựng canvas gợi ý → edit → upscale → refine → dán lại pixel gốc | Trở thành op `outpaint` (op tổng hợp, gọi các op khác trong cùng job) |
| `tools/compose_screenshot.py` | Ghép model RGBA vào khung UI | Op `compose` (chạy trên CPU) |
| `tools/model_fidelity.py` | Chấm điểm độ giữ nguyên model tham chiếu (NCC, hue) | Op `fidelity` (chạy trên CPU) |
| `tools/merge_lora.py` + `tools/sdcpp` | Bake Viggle LoRA vào GGUF | Không đưa vào service. Dùng như pipeline build model offline (§12.5) |
| `pixelart/` | Custom node `PixelArtRetro` và `pixelize.py` | Dùng chung cho cả node ComfyUI và op CPU |
| `custom_nodes/viggle_turbo.py` | Node `ViggleTurboSigmas` | **Hiện chỉ nằm trong ComfyUI portable (bị gitignore).** Phải đưa vào repo (`engine/comfy_nodes/`) |
| ComfyUI | v0.37.0 (`73c9bad`), ComfyUI-GGUF của leejet (`373048b`) | Pin đúng các commit này. Node `TextEncodeQwenImage21` và `QwenImage21Cache` phụ thuộc vào phiên bản |

Model và benchmark trên máy tham chiếu (RTX 3050 6 GB, 64 GB RAM):

| File | Dung lượng | Vai trò |
|---|---|---|
| `qwen-image-2.1-viggle-Q4_K_M.gguf` | 4.6 GB | turbo (mặc định), 5 step, ~30 s/ảnh 768² |
| `qwen-image-2.1-Q4_K_M.gguf` | 4.6 GB | base, 10 step, ~45 s |
| `qwen-image-2.1-Q8_0.gguf` | 7.6 GB | base_q8, 20 step |
| `qwen3vl_8b_int8_convrot.safetensors` | 9.35 GB | text encoder, đồng thời là LLM cho enhance (~2 token/s) |
| `qwen_image_2.1_vae_bf16.safetensors` | 0.68 GB | VAE (có kênh alpha) |
| `RealESRGAN_x2plus.pth` / `x4plus.pth` | 67 MB mỗi file | upscale (BSD-3) |

---

## 2. Kiến trúc

```
 Client (OpenAI SDK · curl · Unity · agent qua MCP · dashboard)
        │ HTTPS
        ▼
 Cloudflare edge ── Access (dashboard) / Bypass + API key (/v1)
        │ tunnel
        ▼
 cloudflared (service trên cùng máy) ──► http://127.0.0.1:8080
 ┌──────────────────────── qis-gateway (FastAPI / uvicorn, 1 process) ─────────────────────────┐
 │  Routers:  /v1/images/*  /v1/jobs  /v1/tools/*  /v1/files  /v1/models  /v1/node/info        │
 │            /admin/api/*  /admin (SPA static)  /mcp  /healthz /readyz /metrics                │
 │  Middleware: auth (API key | session) · rate limit · request id · size limit · CF-IP        │
 │                           │                                                                  │
 │  JobService ── Scheduler (admission · priority · fair-share · model affinity · aging)       │
 │                           │                                                                  │
 │        ┌──────────────────┴──────────────────┐                                               │
 │  GpuExecutor (interface Executor)       CpuPool (ProcessPool: pixelize/compose/fidelity)    │
 │        │                                                                                     │
 │  ComfyClient (HTTP + WebSocket) ◄── ComfySupervisor (spawn · health · restart · VRAM info)  │
 │                                                                                              │
 │  Repos: SQLite WAL (jobs, files, users, api_keys, settings, audit)   BlobStore (sha256)     │
 └────────────────────────────┬─────────────────────────────────────────────────────────────────┘
                              │ 127.0.0.1:8188 (không bao giờ ra ngoài)
                              ▼
         ComfyUI (subprocess, --listen 127.0.0.1 --reserve-vram X --output-directory data/comfy_out)
         custom nodes: ComfyUI-GGUF (pinned) · pixelart_retro · viggle_turbo
```

**Nguyên tắc:**
1. **Gateway là process duy nhất ra mạng.** ComfyUI chỉ nghe loopback. Mọi workflow đều do server tự dựng từ tham số đã validate.
2. **Queue thật nằm ở gateway**, không nằm ở ComfyUI. Mỗi lúc chỉ đẩy xuống ComfyUI tối đa 1 job đang chạy, cộng thêm 1 job prefetch để GPU không nghỉ giữa hai job. Nhờ vậy gateway giữ quyền quyết định ưu tiên, huỷ và chia công bằng.
3. **Cô lập lỗi:** ComfyUI crash hoặc OOM không kéo gateway chết theo. Supervisor khởi động lại ComfyUI, còn job đang chạy dở được retry theo chính sách ở §5.6.
4. **Một job là một chuỗi op (pipeline).** API nào, kể cả endpoint OpenAI, cuối cùng cũng được chuyển thành một `JobSpec` gồm danh sách op. Làm vậy thì chỉ có một đường thực thi duy nhất.

### 2.1 Công nghệ

| Lớp | Chọn | Lý do |
|---|---|---|
| Gateway | Python 3.12, FastAPI, uvicorn, Pydantic v2 | Cùng ngôn ngữ với engine và tools, dùng lại được Pillow/numpy |
| DB | SQLite WAL, SQLAlchemy 2 core/ORM (hoặc SQLModel), Alembic | Không cần dịch vụ ngoài. Migration có version |
| ComfyUI client | `httpx` (async) + `websockets` | Dùng WebSocket để lấy progress và preview, thay cho polling `/history` |
| Hashing | `argon2-cffi` cho password, `hashlib` cho API key (SHA-256 có pepper) | |
| Frontend | React 19, Vite, TypeScript, Tailwind, shadcn/ui, TanStack Query + Router, Recharts | Build ra static và đóng chung vào gói release |
| Môi trường | `uv` (tự quản lý Python), torch theo index CUDA phù hợp | Cài nhanh, cùng một cách trên Windows và Linux |
| Service | Windows: WinSW. Linux: systemd | |

---

## 3. Engine

### 3.1 `engine/workflows.py`

Gồm các hàm thuần (không I/O) trả về dict workflow API của ComfyUI. Code được tách từ `gen.py` và `tools/*.py`, **giữ nguyên hành vi** (có golden test so sánh JSON, xem §17).

```python
def t2i(p: GenParams, prompt: str, seed: int) -> Workflow
def edit(p: GenParams, prompt: str, seed: int, refs: list[str]) -> Workflow      # 1–3 ref
def enhance(request: str, refs: list[str], clip_device: str) -> Workflow         # TextGenerate
def upscale(image: str, model: str, w: int, h: int) -> Workflow
def refine(image: str, prompt: str, w: int, h: int, engine: str, nodes: str, denoise: float, steps: int, seed: int) -> Workflow
def pixelize_node(...)  # phần đuôi gắn thêm vào t2i/edit khi có op pixelize ngay sau (đỡ một vòng I/O)
```

Hằng số (`UNETS`, `VIGGLE_SIGMAS`, `STYLES`, `RGBA_WRAP`…) chuyển sang `engine/constants.py`. Tên file model **không hard-code** nữa: lấy từ profile đang dùng (§12.3).

### 3.2 Op (bước pipeline)

| Op | Chạy ở | Input → Output | Ghi chú |
|---|---|---|---|
| `enhance_prompt` | GPU | text (+ref) → text, ratio | Cache theo `sha1(request + sha256 các ref)` trong bảng `prompt_cache` |
| `generate` | GPU | prompt → image | t2i, hoặc edit khi có `refs` |
| `refine` | GPU | image → image | hires-fix |
| `upscale` | GPU (ESRGAN) / CPU (nearest) | image → image | `method: esrgan \| nearest \| lanczos` |
| `outpaint` | GPU (tổng hợp) | image → image | Bên trong gọi `generate(edit)`, sau đó `upscale`, `refine` và paste-back |
| `pixelize` | CPU | image → sprite + preview | `grid, colors, palette, dither, crop, remove_bg` |
| `remove_bg` | CPU | image → RGBA | `_knock_out_background` của pixelize. Sau này có thể thay bằng model riêng |
| `compose` | CPU | ui + model → image | |
| `fidelity` | CPU | ref + images → JSON scores | |

Một op nhận **artifact** (ảnh hoặc text) từ op trước qua `$prev` hoặc `$step[i]`, hoặc tham chiếu `file_id`. Mỗi op GPU là **một lần gọi ComfyUI**. `outpaint` là ngoại lệ vì gồm nhiều lần gọi.

### 3.3 ComfySupervisor

- Spawn: `python ComfyUI/main.py --listen 127.0.0.1 --port <comfy_port> --reserve-vram <cfg> --output-directory <data>/comfy_out --input-directory <data>/comfy_in --disable-auto-launch` (thêm `--preview-method latent2rgb` nếu dùng partial preview).
- Health: gọi `GET /system_stats` mỗi 5 s. Nếu fail 3 lần liên tiếp thì kill và respawn, backoff 2 s → 60 s. Trạng thái (`starting|ready|crashed|restarting`) được phát lên dashboard.
- Dùng `/system_stats` để lấy VRAM total/free. `/queue` và `/interrupt` dùng để huỷ job. `POST /free` (`unload_models`) dùng khi đổi profile.
- Log của ComfyUI ghi vào `logs/comfyui.log` (xoay vòng file, giữ 5 file × 20 MB).
- Warm-up khi khởi động (tuỳ chọn, bật mặc định): chạy một job 256² với 1 step để nạp model vào RAM/VRAM, tránh 60–90 s cold start cho request đầu tiên.

### 3.4 Progress và preview

Mỗi client kết nối vào WebSocket của ComfyUI bằng một `clientId` riêng, rồi nhận các message `progress` (`value/max`), `executing` và `executed`. Preview là binary frame (JPEG của latent2rgb). Các message này được chuyển thành sự kiện job (§6.5) và đẩy lên SSE.

---

## 4. Mô hình dữ liệu (SQLite)

Mọi ID là **ULID** có prefix, sắp xếp được theo thời gian và không trùng giữa các node: `job_…`, `file_…`, `key_…`, `usr_…`, `node_…`.

```
nodes(id PK, name, public_url, created_at, pubkey)                 -- chỉ 1 dòng ở v1 (node hiện tại)

users(id PK, email UNIQUE NULL, username UNIQUE, pw_hash, role[admin|operator|viewer],
      disabled, failed_logins, locked_until, created_at, last_login_at)
sessions(id PK = sha256(token), user_id FK, csrf, ip, ua, created_at, expires_at, revoked)

api_keys(id PK, name, prefix, hash, owner_user_id FK, scopes JSON, priority[interactive|batch],
         max_pending, rpm, daily_images NULL, allowed_models JSON NULL, created_at, last_used_at,
         expires_at NULL, revoked_at NULL)

jobs(id PK, api_key_id FK NULL, user_id FK NULL, origin_node, exec_node,
     kind[images.generations|images.edits|pipeline|tool], spec JSON, preset,
     status[queued|running|succeeded|failed|canceled|expired], priority, position_hint,
     model_affinity, attempts, error JSON NULL, result JSON NULL,
     created_at, started_at, finished_at, eta_s, idempotency_key NULL, webhook_url NULL,
     UNIQUE(api_key_id, idempotency_key))
job_events(job_id, seq, ts, type, data JSON)          -- giữ N sự kiện cuối, dùng khi client nối lại SSE

files(id PK, sha256, bytes, mime, width, height, has_alpha, purpose[input|output|intermediate],
      owner_key_id, job_id NULL, created_at, expires_at)
blobs(sha256 PK, bytes, path, refcount)                -- lưu theo nội dung

prompt_cache(key PK, request, prompt, ratio, created_at, hits)
settings(scope[node|cluster], key, value JSON, updated_by, updated_at, PRIMARY KEY(scope,key))
audit(id, ts, actor, action, target, data JSON, ip)
stats_minute(ts, jobs_done, jobs_failed, gpu_s, avg_step_s, queue_depth)  -- vẽ biểu đồ dashboard
```

---

## 5. Queue và scheduler

### 5.1 Vòng đời job

```
            admission ok                 dispatcher chọn         ComfyUI xong mọi op
 request ─────────────► queued ─────────────────────────► running ───────────────────► succeeded
    │ queue đầy / quota          │ DELETE                   │ DELETE → /interrupt
    ▼                            ▼                          ├──────► canceled
   429 (Retry-After)          canceled                      │ lỗi
                                                            ├──────► (retry theo §5.6) ─► queued
                              queued quá max_queue_age      └──────► failed
                              ─────────────────────► expired
```

### 5.2 Bốn lớp của "xử lý đồng thời"

| Lớp | Mặc định | Chỉnh ở |
|---|---|---|
| Nhận HTTP | async, giới hạn bằng `max_connections` của uvicorn | config.toml |
| **Queue chờ** `queue.max_pending` | `clamp(round(queue.target_wait_s / bench.avg_job_s), 4, 200)`, ví dụ 3050: 600 / 30 = **20** | dashboard, có hiệu lực ngay |
| Giới hạn theo key `max_pending`, `rpm`, `daily_images` | 10 / 30 / không giới hạn | dashboard, mỗi key |
| GPU concurrency | **1** (thêm 1 job prefetch) | config (nâng cao) |
| CPU pool `cpu.workers` | `max(1, cores // 2)` | dashboard |

`bench.avg_job_s` lấy từ benchmark lúc cài, sau đó được cập nhật liên tục bằng EWMA theo thời gian thực chạy.

### 5.3 Admission (khi nhận request)

1. Validate spec, tính `cost` ước lượng theo đơn vị giây GPU: `Σ op.steps × s_per_step(model, megapixel) + overhead`.
2. Từ chối với `429 queue_full` nếu tổng số job queued ≥ `max_pending`, **hoặc** tổng ETA > `queue.max_wait_s`. `Retry-After` = thời gian ước tính cho tới khi còn chỗ.
3. Từ chối với `429 key_limit` nếu vượt `max_pending` hoặc `rpm` của key.
4. Từ chối với `400` nếu job ước tính vượt `job.max_cost_s` (chống request quái vật như `n=50` ở 2048²).

### 5.4 Chọn job kế tiếp (dispatcher)

Điểm của mỗi job queued:

```
score = priority_weight(prio)                    # interactive 100, batch 0
      + aging_per_min × phút_đã_chờ              # 5/phút: job batch không bao giờ bị đói
      + affinity_bonus  nếu job.model == model_đang_nạp   # 30: tránh đổi model tốn 60–90 s
      − fair_penalty × số_job_key_này_đã_chạy_trong_5_phút  # chia đều giữa các key
```

Chọn job có score cao nhất. Khi hoà điểm thì job tạo trước đi trước. Các trọng số nằm trong setting `scheduler.*`. Job enhance prompt dùng GPU với model Qwen3-VL nên cũng có `model_affinity = "encoder"`.

### 5.5 Batch

Nếu có ≥ 2 job cùng `(preset, prompt, size)` nhưng khác seed, và `vram_total ≥ 16 GB`, thì gộp thành `batch_size = k` (tối đa `scheduler.max_batch`). **Mặc định tắt**, bật được trong dashboard sau khi đo lường thực tế. `n > 1` trong một request thì luôn tách thành n lần sample trong cùng job, nên dùng lại được cache encode.

### 5.6 Lỗi, retry và phục hồi

| Tình huống | Xử lý |
|---|---|
| ComfyUI `execution_error` có OOM | Retry 1 lần với `reserve_vram += 0.5` hoặc encoder chạy CPU. Kết quả ghi `degraded: true` |
| ComfyUI crash hoặc mất kết nối trong lúc chạy | Supervisor restart, job quay về `queued` (`attempts++`), tối đa 2 lần rồi `failed` |
| Lỗi validation của ComfyUI (node không tồn tại, file model thiếu) | `failed` ngay, không retry, báo lỗi trên dashboard |
| Gateway restart | Job `running` quay về `queued`, và mọi job `queued` được nạp lại từ DB |
| Timeout job (`job.timeout_s`, mặc định `max(300, 5 × cost)`) | `/interrupt`, rồi `failed` |

### 5.7 ETA và vị trí trong queue

`eta_s(job) = Σ cost của các job xếp trước theo thứ tự score hiện tại + cost(job)`. Giá trị này được trả trong response `202`, có trong SSE, và dùng cho `Retry-After`.

---

## 6. API

### 6.1 Quy ước chung

- Base: `https://<public_host>/v1`. JSON UTF-8. Thời gian dùng ISO-8601 UTC.
- **Auth API:** `Authorization: Bearer sk-qis-<32 ký tự base62>`. Server chỉ lưu `sha256(pepper + key)` và 8 ký tự prefix để hiển thị.
- **Lỗi theo định dạng OpenAI** để SDK tự parse được:
  `{"error": {"message": "...", "type": "invalid_request_error|rate_limit_error|server_error|authentication_error", "param": "size", "code": "queue_full"}}`
- Header phản hồi: `X-Request-Id`, `X-Qis-Node`, `X-Qis-Job-Id`. Với 429 có thêm `Retry-After` và `X-Qis-Queue-Depth`.
- Nhận `Idempotency-Key`: gửi lại cùng key thì trả về job cũ, lưu 24 h.
- Ảnh đầu vào nhận được dưới 4 dạng: multipart file, `file_id`, data URL base64, hoặc URL https. URL chỉ được nhận khi bật `inputs.allow_url` và có chặn SSRF với IP private.
- Giới hạn: `inputs.max_bytes` = 20 MB mỗi ảnh, `inputs.max_pixels` = 16 MP (chống decompression bomb). Ảnh luôn được decode lại bằng Pillow và bỏ metadata.

### 6.2 Tương thích OpenAI

#### `POST /v1/images/generations`
| Trường OpenAI | Map sang |
|---|---|
| `model` | Tên preset (§7). Mặc định `qwen-image-2.1-turbo` |
| `prompt` | subject hoặc prompt. Preset quyết định có bọc style và hint hay không |
| `n` (1–8) | số seed |
| `size` | `"WxH"` bất kỳ (bội số của 32, cạnh 256–2048) hoặc `"auto"` (theo preset, hoặc `ratio` của enhance) |
| `quality` | `low`: turbo 4 step. `medium`/`auto`: turbo 5. `high`: turbo 8, thêm refine. `hd`: base 10 |
| `background` | `transparent`: RGBA wrap. `opaque`/`auto`: không bọc |
| `output_format` | `png` (mặc định), `webp`, `jpeg` |
| `output_compression` | 0–100 cho webp/jpeg |
| `response_format` | `b64_json` (mặc định) hoặc `url` (URL có ký, hết hạn theo `storage.url_ttl`) |
| `stream`, `partial_images` (0–3) | SSE: `image_generation.partial_image` → `image_generation.completed` |
| `user` | ghi vào audit |
| **mở rộng** (`extra_body`) | `seed, steps, negative, enhance, style, grid, colors, palette, dither, keep_bg, no_crop, preview_size, aspect, mode, webhook_url, priority` |

Response đúng dạng OpenAI:
`{"created": 1759132800, "data": [{"b64_json": "...", "revised_prompt": "..."}], "usage": {...}}`.
Thêm field mở rộng `qis: {job_id, seeds, timings, sprite: {b64_json|url}, preview: {...}}` khi preset có bước pixelize: `data[i]` là sprite, còn ảnh raw nằm trong `qis`.

#### `POST /v1/images/edits`
multipart: `image` (hoặc `image[]`, 1–3 ảnh), `mask` (tuỳ chọn: có mask thì chạy inpaint/outpaint), `prompt`, cộng các trường như trên. Ảnh đầu tiên quyết định khung hình output.

#### `GET /v1/models`, `GET /v1/models/{id}`
Liệt kê các preset kèm `owned_by: "qis"` và phần mở rộng `qis: {description, ops, est_s, commercial_ok}`.

#### Giới hạn 100 s của Cloudflare (bắt buộc xử lý)
Cloudflare trả **524** nếu origin im lặng quá ~100 s. Gateway xử lý như sau cho endpoint đồng bộ:
1. `stream: true`: SSE, gửi `: ping` mỗi 15 s.
2. Không stream: gateway gửi header `200` và `Content-Type: application/json` ngay, rồi **gửi một khoảng trắng mỗi 15 s** cho đến khi có kết quả, sau đó gửi phần JSON. JSON hợp lệ cho phép khoảng trắng đứng trước, và SDK của OpenAI parse được. Nếu job thất bại, body vẫn là object `{"error": …}`, nhưng status code đã là 200. Đây là đánh đổi đã chấp nhận; có thể tắt bằng `api.sync_keepalive=false`, khi đó job ước tính vượt `api.sync_max_s` (mặc định 90) trả `400 use_async` kèm `job_id` để poll.
3. Client native nên dùng `POST /v1/jobs` (§6.3).

### 6.3 Job API (native)

```
POST   /v1/jobs                  body: JobSpec → 202 {id, status, position, eta_s, links}
GET    /v1/jobs/{id}             → Job (status, progress, result.outputs[], error, timings)
GET    /v1/jobs/{id}/events      SSE (hỗ trợ Last-Event-ID để nối lại)
DELETE /v1/jobs/{id}             huỷ (queued: xoá khỏi queue; running: /interrupt)
GET    /v1/jobs?status=&limit=&after=   liệt kê job của key hiện tại
POST   /v1/jobs/{id}/retry       tạo job mới với cùng spec
```

`JobSpec`:
```jsonc
{
  "preset": "pixelart-16bit-64",          // tuỳ chọn: ops mặc định lấy từ preset
  "inputs": {"hero": "file_01J…", "axe": "https://…"},
  "ops": [
    {"op": "enhance_prompt", "prompt": "a goblin archer"},
    {"op": "generate", "prompt": "$prev.text", "size": "auto", "quality": "high", "n": 4, "seed": 7,
     "refs": ["$inputs.hero"]},
    {"op": "refine", "image": "$prev", "size": "1152x1536", "nodes": "0.25, 0.12"},
    {"op": "upscale", "image": "$prev", "size": "2304x3072", "method": "esrgan", "model": "realesrgan-x2"},
    {"op": "pixelize", "image": "$step[1]", "grid": 64, "palette": "db32"}
  ],
  "outputs": ["$step[3]", "$step[4]"],    // mặc định là output của op cuối
  "priority": "batch",
  "webhook_url": "https://…",
  "retention_hours": 72
}
```
Quy tắc: `n > 1` ở một op làm các op phía sau chạy **map** trên từng ảnh. `$prev` trỏ tới toàn bộ danh sách ảnh của op trước.

### 6.4 Tools (đường tắt cho job một op)

`POST /v1/tools/{op}` nhận multipart hoặc JSON với tham số của op. Mặc định chạy đồng bộ (vẫn có keepalive); thêm `?async=1` thì trả `202`.
Gồm: `enhance-prompt`, `refine`, `upscale`, `outpaint`, `pixelize`, `remove-bg`, `compose`, `fidelity`.
Op CPU (`pixelize`, `remove-bg`, `compose`, `fidelity`) **không vào queue GPU**. Chúng chạy trong CpuPool, có semaphore riêng.

### 6.5 Sự kiện SSE

```
event: queued     data: {"position":3,"eta_s":95}
event: started    data: {"op_index":0,"op":"generate"}
event: progress   data: {"op_index":1,"step":3,"steps":5,"pct":0.42}
event: preview    data: {"op_index":1,"b64_jpeg":"…"}          // chỉ khi partial_images > 0
event: op_done    data: {"op_index":1,"outputs":["file_…"]}
event: succeeded  data: {Job}
event: failed     data: {"error":{…}}
: ping                                                          // mỗi 15 s
```
Endpoint OpenAI `stream:true` dùng tên sự kiện của OpenAI: `image_generation.partial_image` (`partial_image_index`, `b64_json`) và `image_generation.completed`.

### 6.6 Files

```
POST   /v1/files            multipart file, purpose=input → {id, sha256, width, height, has_alpha}
GET    /v1/files/{id}       metadata
GET    /v1/files/{id}/content   (API key, hoặc ?sig=…&exp=… cho URL có ký)
DELETE /v1/files/{id}
```

### 6.7 Node, sức khoẻ, metrics

- `GET /v1/node/info`: `{node_id, name, version, gpu:{name, vram_total, compute_cap, driver}, profile, presets[], models[], queue:{depth, max_pending, eta_s}, perf:{s_per_step{…}, avg_job_s}, status}`. Đây là **hợp đồng cho mesh** sau này.
- `/healthz` trả 200 khi gateway sống. `/readyz` trả 200 khi ComfyUI `ready` và model đã warm.
- `/metrics` (Prometheus, cần token riêng hoặc chỉ cho phép từ loopback): `qis_queue_depth`, `qis_jobs_total{status}`, `qis_job_seconds_bucket{preset}`, `qis_gpu_vram_bytes`, `qis_comfy_restarts_total`.

### 6.8 MCP (P2)

`/mcp` (Streamable HTTP), xác thực bằng API key. Các tool: `generate_image`, `edit_image`, `pixelize`, `upscale`, `refine`, `outpaint`, `get_job`, `list_presets`. Output trả về dạng image content kèm URL.

### 6.9 Admin API (chỉ dashboard dùng)

`/admin/api/*` dùng session cookie cộng CSRF: auth (login/logout/me/đổi password), users, api-keys, jobs (xem tất cả, huỷ, retry), settings (đọc/ghi theo scope, kèm schema), models (danh sách, tải, xoá, đổi profile, benchmark), system (trạng thái, restart ComfyUI, xem log dạng tail, version, kiểm tra update), stats. SSE `/admin/api/events` phát trạng thái realtime.

---

## 7. Preset (model ảo)

Preset là một **pipeline có tên** cùng tham số mặc định. Tất cả nằm trong `presets.yaml`, admin sửa được trên dashboard. Preset là thứ client nhìn thấy trong `GET /v1/models`.

```yaml
qwen-image-2.1-turbo:  {ops: [generate], defaults: {mode: turbo, steps: 5, size: 1024x1024}}
qwen-image-2.1-base:   {ops: [generate], defaults: {mode: base, steps: 10}}
qwen-image-2.1-hq:     {ops: [generate, refine, upscale], defaults: {generate: {size: 1024x1024}, upscale: {scale: 2}}}
pixelart-16bit-64:     {ops: [generate, pixelize], defaults: {style: 16bit, size: 768x768, grid: 64, colors: 24}}
pixelart-32bit-128:    {ops: [generate, pixelize], defaults: {style: 32bit, grid: 128}}
pixelart-icon-32:      {ops: [generate, pixelize], defaults: {grid: 32, background: transparent}}
```
Preset nào chứa model mà profile hiện tại không có (ví dụ base Q8 trên máy 6 GB) thì tự ẩn khỏi danh sách.

---

## 8. Lưu trữ

```
<data_dir>/
  qis.db (+ -wal, -shm)
  blobs/ab/cd/<sha256>.<ext>        # lưu theo nội dung, refcount
  comfy_in/  comfy_out/             # thư mục trao đổi với ComfyUI, dọn ngay sau mỗi op
  logs/
  backups/                          # sao lưu DB hằng ngày (giữ 7 bản)
```
- Thời gian giữ file: input 24 h, output 72 h, intermediate 1 h (đều chỉnh được, và từng job có thể ghi đè). Một task nền chạy mỗi 10 phút để dọn file hết hạn và blob có refcount = 0.
- Có hạn mức ổ đĩa `storage.max_gb`. Khi vượt, xoá file cũ nhất trước (theo LRU); nếu vẫn không đủ thì admission trả `507 storage_full`.
- URL có ký: `/v1/files/{id}/content?exp=…&sig=HMAC(secret, id|exp)`. `public_url` lấy từ setting, không lấy từ header `Host`.
- Về sau có thể thêm backend S3/R2 (P5), giữ cùng interface `BlobStore`.

---

## 9. Dashboard

Nằm tại `/admin`, là SPA dựng bằng React, Vite và TypeScript, được gateway serve dạng static (nhánh fallback của SPA). Dữ liệu realtime lấy qua SSE.

| Trang | Nội dung |
|---|---|
| Đăng nhập | username/email + password. Lần đăng nhập đầu tiên buộc đổi password nếu đang dùng password sinh tự động |
| **Tổng quan** | Trạng thái node và ComfyUI, GPU (tên, VRAM đang dùng/tổng, nhiệt độ và util lấy qua `nvidia-smi`/NVML), độ sâu queue, job đang chạy (có progress và preview), throughput 24 h, tỉ lệ lỗi, ETA |
| **Jobs** | Bảng lọc theo trạng thái, key, preset, thời gian. Xem chi tiết: spec, timeline sự kiện, ảnh output. Có huỷ, retry và tải xuống |
| **Playground** | Form cho generate, edit, tool và pipeline. Kéo thả ảnh ref, xem progress và preview trực tiếp, và **copy lệnh curl hoặc code Python tương ứng** |
| **Queue & Limits** | `max_pending`, `target_wait_s`, `max_wait_s`, trọng số scheduler, số CPU worker, thời gian keepalive đồng bộ; có hiệu lực ngay |
| **Models & Profile** | Profile hiện tại, danh sách model đã cài (dung lượng, license, commercial_ok), tải thêm từ manifest (có progress), đổi profile, chạy benchmark, xem kết quả benchmark |
| **Presets** | Sửa `presets.yaml` bằng form, kèm validate |
| **API Keys** | Tạo key (chỉ hiện một lần), đặt tên, scope, priority, quota, hạn dùng; thu hồi; xem thống kê sử dụng |
| **Users** | Tạo, khoá, reset password, gán role |
| **Settings** | Public URL, CORS, thời gian giữ file, hạn mức lưu trữ, kích thước input, trạng thái Cloudflare tunnel, `reserve_vram`, warm-up |
| **Logs** | Tail log của gateway và ComfyUI, lọc theo mức độ |
| **System** | Version, kiểm tra và áp dụng update, restart ComfyUI hoặc service, sao lưu và tải DB |

Setting nào cần restart thì có nhãn *restart required* và một nút restart riêng.

---

## 10. Xác thực và phân quyền

### 10.1 Dashboard (người dùng)
- Password hash bằng **argon2id** (m=64 MB, t=3). Tối thiểu 10 ký tự.
- Session token 256-bit trong cookie `qis_session` với `HttpOnly; Secure; SameSite=Lax; Path=/`. Hết hạn sau 12 h không hoạt động hoặc tối đa 7 ngày. Lưu hash của token trong DB để thu hồi được.
- **CSRF:** mọi request thay đổi dữ liệu phải có header `X-CSRF-Token` khớp với giá trị lưu trong session. Kiểm tra thêm header `Origin`.
- Chống brute-force: sau 5 lần sai liên tiếp thì khoá 15 phút theo user, cộng rate limit theo IP (lấy `CF-Connecting-IP`).
- Role:
  - `admin`: toàn quyền.
  - `operator`: quản lý job, key của chính mình và playground; không đụng được setting hệ thống, user và model.
  - `viewer`: chỉ xem.
- Tạo admin đầu tiên: installer hỏi, hoặc tự sinh. Có lệnh `qis admin reset-password <user>` để khôi phục.
- Thiết kế interface `AuthProvider` sẵn để sau này thêm **OIDC** (Google Workspace) hoặc tin JWT của **Cloudflare Access** (`Cf-Access-Jwt-Assertion`).

### 10.2 API
- Chỉ dùng API key, không chấp nhận session cookie ở `/v1` (tránh CSRF).
- Scope gồm `images:generate`, `images:edit`, `tools:*`, `files:*`, `jobs:read`, `admin:*` (dành cho automation). Key còn có thể bị giới hạn preset qua `allowed_models`.

---

## 11. Bảo mật

| Rủi ro | Biện pháp |
|---|---|
| ComfyUI không có auth, và workflow có thể đọc/ghi file tuỳ ý | ComfyUI chỉ nghe `127.0.0.1` trên port ngẫu nhiên hoặc cấu hình. Gateway không bao giờ proxy tới ComfyUI. Mọi workflow do server tự dựng. Không có endpoint nhận workflow thô |
| Ảnh độc (decompression bomb, polyglot) | Giới hạn byte và pixel, `Image.MAX_IMAGE_PIXELS`, decode lại và ghi ra PNG sạch trước khi đưa vào ComfyUI |
| SSRF qua input URL | Mặc định tắt. Khi bật thì chỉ nhận https, resolve DNS rồi chặn dải private, loopback và link-local, giới hạn kích thước và timeout |
| Path traversal | Tên file do server sinh (ULID). Không dùng tên file của client |
| Lộ key | Key chỉ hiện một lần và chỉ lưu hash. Log che `Authorization` |
| Lạm dụng tài nguyên | Queue, quota theo key, `max_cost_s`, `rpm` |
| Header giả mạo | Chỉ tin `CF-Connecting-IP` và `X-Forwarded-*` khi request đến từ loopback, tức là từ cloudflared |
| Cập nhật độc | Gói release có SHA-256 (và chữ ký minisign ở P4). Installer kiểm tra trước khi giải nén |
| Secret | `secret_key` (HMAC, pepper) được sinh lúc cài, nằm trong `config/secret.key`. Linux đặt quyền 600; Windows đặt ACL chỉ cho service account và Administrators |

---

## 12. Installer

### 12.1 Cách gọi

```powershell
# Windows (PowerShell 5.1+, chạy với quyền Administrator để đăng ký service)
irm https://get.<domain>/install.ps1 | iex
# không tương tác:
& ([scriptblock]::Create((irm https://get.<domain>/install.ps1))) -Profile auto -Yes -Port 8080 `
    -AdminUser admin -TunnelToken <token> -InstallDir 'C:\qis' -ModelsDir 'D:\models'
```
```bash
curl -fsSL https://get.<domain>/install.sh | bash
curl -fsSL https://get.<domain>/install.sh | bash -s -- --profile auto --yes --tunnel-token <token>
```
- Khi chạy `curl | bash`, mọi lệnh `read` phải đọc từ `/dev/tty`. Nếu không có tty (CI chẳng hạn) thì chuyển sang chế độ `--yes` với profile `auto`.
- Các flag đều có biến môi trường tương ứng (`QIS_PROFILE`, `QIS_YES`, `HF_TOKEN`…).
- Script host trên Cloudflare Pages/R2. Gói release lấy từ GitHub Releases (repo private thì cần `-GithubToken`), hoặc từ mirror R2.

### 12.2 Các bước

1. **Preflight**
   - OS: Windows 10/11 x64 hoặc Linux x86_64 với glibc ≥ 2.31.
   - Quyền cài đặt.
   - Port còn trống.
   - Có Internet.
2. **Detect phần cứng**
   - Chạy `nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv,noheader`. Không có NVIDIA thì hỏi có cài chế độ *tools-only* không; nếu không thì thoát.
   - Driver phải ≥ bản tối thiểu cho CUDA 12.8 (Windows ≥ 572.x, Linux ≥ 570.x). Thấp hơn thì dừng và in hướng dẫn update driver.
   - RAM: Windows qua `Get-CimInstance Win32_ComputerSystem`, Linux qua `/proc/meminfo`.
   - CPU cores, dung lượng trống ở thư mục cài và thư mục model.
   - Windows **không** dùng `Win32_VideoController.AdapterRAM` vì giá trị bị kẹt ở 4 GB.
3. **Chọn profile** (§12.3). Hiển thị bảng khuyến nghị, dung lượng tải, ổ đĩa cần có, thời gian ước tính mỗi ảnh và license. Có 4 lựa chọn: `Recommended / Max quality / Fast / Custom`. Chọn Custom thì tự chọn từng model trong manifest.
4. **Môi trường**
   - Cài `uv` vào `<install>/bin`, rồi `uv python install 3.12`.
   - Tạo venv, cài torch (`cu128` nếu `compute_cap ≥ 7.5`, `cu126` cho Pascal 6.x, và Maxwell trở về trước không hỗ trợ).
   - Cài `requirements.lock`.
5. **ComfyUI:** tải tarball đúng commit đã pin, sau đó ComfyUI-GGUF (pin), rồi link hoặc copy `engine/comfy_nodes/*` (pixelart_retro, viggle_turbo) vào `custom_nodes/`. Ghi `extra_model_paths.yaml` trỏ tới `ModelsDir`.
6. **Model:** tải theo manifest.
   - Tải song song 2 file, có resume (`curl -C -` hoặc `Invoke-WebRequest` kèm Range), kiểm tra sha256, hỗ trợ `HF_TOKEN`.
   - Tự phát hiện model đã có: file trùng sha256 ở `ModelsDir` hay bất kỳ đường dẫn nào khai báo qua `-ReuseModels` thì dùng lại, không tải.
7. **Config**
   - Ghi `config.toml`, sinh `secret.key`.
   - Tạo admin: hỏi mật khẩu, hoặc sinh ngẫu nhiên và in ra đúng một lần.
   - Chạy `qis db upgrade`.
8. **Service**
   - Windows: WinSW (`qis-gateway` chạy dưới LocalSystem hoặc account được chỉ định, tự restart khi lỗi). Không cần mở firewall vì gateway chỉ nghe loopback; trừ khi chọn `-Lan` thì mới mở rule cho port.
   - Linux: systemd unit `qis.service` với `User=qis`, `Restart=on-failure`, thêm một số tuỳ chọn hardening.
9. **Cloudflare (tuỳ chọn):** có `--tunnel-token` thì cài `cloudflared` và `cloudflared service install <token>`. Sau đó in hướng dẫn trỏ public hostname tới `http://127.0.0.1:<port>`, và gợi ý policy Access: *Bypass* cho `/v1/*` và `/healthz`, *Allow email* cho `/admin*`.
10. **Benchmark:** khởi động service, đợi `/readyz`, chạy benchmark (1 ảnh warm-up, sau đó 3 ảnh cho mỗi preset chính), ghi `s_per_step` và `avg_job_s`, rồi tính `max_pending` mặc định.
11. **Tổng kết:** in URL dashboard, thông tin admin, API key đầu tiên (tuỳ chọn tạo luôn), lệnh test `curl`, và lệnh `qis` hữu ích.

Script **idempotent**: chạy lại thì chỉ bổ sung phần còn thiếu, hoặc hỏi có muốn đổi profile không. Có `--uninstall` (giữ model trừ khi thêm `--purge`).

### 12.3 Profile theo VRAM

Profile nằm trong manifest và được chọn theo **VRAM, sau đó RAM**. Encoder int8 nặng 9.35 GB, nên nếu không nằm hết trong VRAM thì được stream từ RAM; vì vậy **RAM tối thiểu 32 GB**, và 16 GB chỉ dùng được với cảnh báo chậm.

| Tier | VRAM | Diffusion (turbo) | Base (tuỳ chọn) | Encoder | Upscale | `reserve_vram` | Ghi chú |
|---|---|---|---|---|---|---|---|
| T0 | < 6 GB | – | – | – | RealESRGAN (CPU/GPU) | – | *tools-only*: pixelize, upscale, compose |
| T1 | 6–8 GB | viggle **Q4_K_M** | Q4_K_M | int8 (stream) | RealESRGAN x2/x4 | 1.2 | Máy tham chiếu: ~30 s/ảnh |
| T2 | 10–12 GB | viggle **Q8_0** | Q4_K_M | int8 (stream) | + refine | 1.0 | Mặt phẳng mịn hơn Q4 |
| T3 | 16 GB | viggle Q8_0 | Q8_0 | int8 (stream một phần) | + refine | 1.0 | Có thể bật batch 2 |
| T4 | 24 GB | viggle Q8_0 | Q8_0 | int8 **nằm trong VRAM** | + refine, batch 2–4 | 1.5 | Không mất thời gian stream encoder, enhance nhanh hơn nhiều |
| T5 | ≥ 32 GB | viggle **BF16** | BF16 | int8/bf16 trong VRAM | + refine | 2 | Chất lượng tối đa |

Lựa chọn upscaler:
- **Pixel art:** `nearest`, không cần model.
- **Illustration:** Real-ESRGAN x2/x4 là mặc định (BSD-3, dùng thương mại được).
- **HQ:** `refine` bằng diffusion rồi mới ESRGAN.
- Upscaler có license NC (4x-UltraSharp…) chỉ bật được qua *Custom*, có gắn cờ `commercial_ok: false` và hiện trên dashboard.

### 12.4 `models.yaml` (manifest)

```yaml
version: 3
models:
  - id: qwen-image-2.1-viggle-q4km
    role: diffusion          # diffusion | text_encoder | vae | upscaler
    file: qwen-image-2.1-viggle-Q4_K_M.gguf
    dir: diffusion_models
    url: https://huggingface.co/<org>/qwen-image-2.1-viggle-gguf/resolve/main/qwen-image-2.1-viggle-Q4_K_M.gguf
    sha256: "…"
    bytes: 4604557984
    min_vram_gb: 6
    license: qwen-research    # non-commercial
    commercial_ok: false
    variant: {family: qwen-image-2.1, kind: turbo, quant: Q4_K_M, lora: viggle-v0.2.1}
profiles:
  T1: {vram_gb: [6, 8], models: [qwen-image-2.1-viggle-q4km, qwen-image-2.1-q4km, qwen3vl-8b-int8, qwen-image-2.1-vae, realesrgan-x2, realesrgan-x4],
       comfy: {reserve_vram: 1.2}, presets: [qwen-image-2.1-turbo, qwen-image-2.1-base, pixelart-*]}
```
Manifest được đóng trong gói release. Có thể cập nhật từ URL (`qis models refresh`) mà không cần nâng version service.

### 12.5 File Viggle bake sẵn

Việc bake mất khoảng 25 phút và cần khoảng 45 GB ổ tạm, nên **không để installer làm việc này mặc định**. Thay vào đó:
- Dùng `tools/merge_lora.py` + `sd-cli` để bake sẵn Q4_K_M, Q8_0 và BF16, rồi đẩy lên một repo HF riêng.
- **Cần xác nhận trước khi publish:** Qwen Research License có cho phép phân phối lại bản phái sinh không. Nếu không, đặt repo HF ở chế độ private (installer dùng `HF_TOKEN`), hoặc giữ phương án *bake tại máy* (`qis models bake`) làm dự phòng.

### 12.6 Cấu trúc thư mục sau khi cài

```
<install>/                       Windows mặc định C:\qis, Linux /opt/qis
  app/            (gói release: server/, engine/, web/dist, install/, VERSION)
  comfyui/        (ComfyUI đã pin cùng custom_nodes)
  .venv/  bin/uv  bin/winsw.exe
  config/config.toml  config/secret.key  config/presets.yaml
<data>/                          Windows C:\ProgramData\qis, Linux /var/lib/qis
<models>/                        mặc định <data>/models, có thể trỏ đi nơi khác
```

### 12.7 CLI `qis`

`qis status | start | stop | restart | logs [-f] | doctor | benchmark | update [--check] | models list|add|remove|refresh|bake | profile set <T> | admin create|reset-password | key create|revoke | db backup|upgrade | uninstall`

`qis doctor` kiểm tra: driver/CUDA, torch có thấy GPU không, ComfyUI có lên không, node bắt buộc có đủ không (`TextEncodeQwenImage21`, `UnetLoaderGGUF`, `ViggleTurboSigmas`, `PixelArtRetro`), sha256 của model, dung lượng ổ trống, port, cloudflared.

### 12.8 Cập nhật

`qis update` tải release mới, kiểm tra hash, giải nén vào `app.new/`, chạy migration DB (sao lưu DB trước), rồi đổi thư mục nguyên tử và restart. Nếu `/readyz` không lên trong 3 phút thì tự rollback. Chỉ cài lại ComfyUI khi commit pin thay đổi.

---

## 13. Cấu hình

**`config.toml`** (cần restart khi sửa):
```toml
[server]   host = "127.0.0.1"  port = 8080  public_url = "https://img.example.com"
[paths]    data = "…"  models = "…"  comfyui = "…"
[comfy]    port = 8188  reserve_vram = 1.2  extra_args = []  warmup = true
[gpu]      device = 0
[security] trust_proxy_from = ["127.0.0.1"]  cors_origins = []
[profile]  tier = "T1"
```
**Runtime settings** (lưu trong bảng `settings`, sửa trên dashboard, có hiệu lực ngay): `queue.*`, `scheduler.*`, `cpu.workers`, `api.sync_keepalive`, `api.sync_max_s`, `inputs.*`, `storage.*`, `job.timeout_s`, `job.max_cost_s`, `defaults.*`. Mỗi key có schema Pydantic, dùng để validate và sinh form trên dashboard.

---

## 14. Quan sát và vận hành

- Log dạng JSON, mỗi dòng có `request_id` và `job_id`. Xoay vòng file.
- `/metrics` cho Prometheus. `stats_minute` phục vụ biểu đồ dashboard (không bắt buộc cài Prometheus).
- Bảng `audit` ghi đăng nhập, tạo và thu hồi key, đổi setting, đổi profile, huỷ job của người khác.
- Đọc NVML qua `pynvml` (util, VRAM, nhiệt độ, công suất) mỗi 2 s khi dashboard đang mở, 30 s khi không có ai xem.

---

## 15. Chuẩn bị cho mesh (làm ngay trong P1–P4)

Mesh sẽ thiết kế chi tiết ở P5. Để P5 không phải đập lại, các phần sau phải làm sẵn:

1. **Danh tính node:** lúc cài, sinh `node_id` (ULID) cùng cặp khoá **ed25519** (`config/node.key`). Public key có trong `/v1/node/info`.
2. **ULID có prefix cho mọi entity**, không dùng autoincrement.
3. **Interface `Executor`**:
   ```python
   class Executor(Protocol):
       def capabilities(self) -> Capabilities: ...          # model, preset, VRAM, perf
       def estimate(self, spec: JobSpec) -> float: ...     # giây GPU
       async def run(self, job: Job, emit: EventSink) -> JobResult: ...
       async def cancel(self, job_id: str) -> None: ...
   ```
   P1 chỉ có `LocalComfyExecutor`. P5 thêm `RemoteNodeExecutor`, gọi `/v1/jobs` của node khác bằng peer token ký ed25519.
4. **Job ghi `origin_node` và `exec_node`**. Sự kiện job có `seq` tăng dần, nên có thể relay qua node trung gian mà không lẫn thứ tự.
5. **Blob lưu theo sha256**, nhờ vậy chuyển input và output giữa các node không trùng lặp và kiểm tra được toàn vẹn.
6. **Setting có hai scope `node` và `cluster`**. User, API key, preset và quota thuộc `cluster`, sau này sẽ đồng bộ hoặc có một nguồn chính duy nhất.
7. **`/v1/node/info` ổn định và có version**, đóng vai trò hợp đồng capability và tải hiện tại để các node chọn nơi đẩy job.
8. Các hướng cho P5 (chưa quyết, chỉ ghi lại): overlay mạng giữa các node (Tailscale/WireGuard hoặc Cloudflare), khám phá node (seed list hoặc registry), định tuyến theo ETA thấp nhất cộng affinity model, đồng bộ `cluster` (một node làm leader, hoặc CRDT/Litestream), và API key hợp lệ trên mọi node.

---

## 16. Cấu trúc repo

```
qwen-image-service/
  server/                 FastAPI app
    app.py  config.py  db/ (models, migrations)  auth/  api/ (openai.py, jobs.py, tools.py, files.py, node.py, admin/)
    scheduler/ (queue.py, dispatcher.py, admission.py, cost.py)
    executors/ (base.py, local_comfy.py)  cpu_pool.py  storage/ (blobstore.py, retention.py)  events.py  mcp.py
  engine/
    workflows.py  constants.py  ops/ (generate.py, refine.py, upscale.py, outpaint.py, pixelize.py, compose.py, fidelity.py, enhance.py)
    comfy/ (client.py, supervisor.py)  hardware.py (nvidia-smi/NVML)  bench.py
    comfy_nodes/ (pixelart_retro/, viggle_turbo.py)
  pixelart/               (giữ nguyên, được engine import)
  web/                    React + Vite + TS (build ra web/dist)
  install/
    install.ps1  install.sh  models.yaml  presets.yaml  winsw.xml.tmpl  qis.service.tmpl  comfy.lock (pin commit)
  cli/qis.py              CLI quản lý
  gen.py  tools/          (giữ tạm; sau P2 gen.py chỉ còn là client của API)
  tests/                  unit, golden workflow, integration (fake ComfyUI), e2e (GPU, đánh dấu riêng)
  docs/DESIGN.md  docs/API.md  docs/OPERATIONS.md
  pyproject.toml  uv.lock  .github/workflows/ (lint, test, build web, release)
```

---

## 17. Kế hoạch triển khai

Mỗi phase kết thúc bằng một bản chạy được và test pass.

### P1: Core (engine, queue, API)
1. Khung repo: `pyproject.toml` với uv, ruff, pytest, cấu trúc thư mục. Đưa `viggle_turbo.py` và `pixelart_retro` vào `engine/comfy_nodes/`.
2. `engine/workflows.py` tách từ `gen.py` và `tools/`, kèm **golden test**: JSON workflow sinh bởi code mới phải giống hệt code cũ với cùng tham số.
3. `ComfyClient` (HTTP + WS), `ComfySupervisor` (spawn, health, restart, warm-up).
4. DB và migration, ULID, bảng `jobs`, `files`, `blobs`, `api_keys`, `settings`, `prompt_cache`.
5. Scheduler: admission, dispatcher (priority, aging, affinity, fair-share), cost model, ETA, cancel, recovery khi restart.
6. `LocalComfyExecutor` cho các op GPU (generate, edit, enhance) và CpuPool cho op pixelize.
7. API: `/v1/images/generations`, `/v1/images/edits`, `/v1/models`, `/v1/jobs*`, `/v1/files*`, `/v1/node/info`, health. Lỗi theo định dạng OpenAI. Keepalive vượt giới hạn 100 s.
8. API key: tạo bằng CLI (`qis key create`).
9. Test integration với **fake ComfyUI**: một server giả trả history, progress và lỗi, dùng để test scheduler mà không cần GPU.

**Xong khi:** từ máy khác, `openai.OpenAI(base_url=…).images.generate(model="pixelart-16bit-64", prompt=…)` chạy được. Bắn 30 request cùng lúc thì 20 request vào queue và 10 request nhận 429 kèm `Retry-After`. Kill ComfyUI giữa chừng thì job tự chạy lại. Restart gateway không mất job.

### P2: Tools và pipeline
1. Các op `refine`, `upscale` (esrgan, nearest, lanczos), `outpaint`, `remove_bg`, `compose`, `fidelity`.
2. Pipeline nhiều op (`$prev`, `$step[i]`, map theo `n`), `/v1/tools/*`.
3. SSE cho job, `stream`/`partial_images` theo chuẩn OpenAI, preview qua WebSocket.
4. Webhook có ký HMAC và retry 3 lần.
5. Retention và dọn blob, URL có ký.
6. Endpoint `/mcp`.
7. `gen.py` chuyển thành client của API (giữ nguyên các flag); `tools/*.py` chuyển thành wrapper mỏng.

**Xong khi:** chạy được pipeline `enhance → generate(n=4) → refine → upscale → pixelize` trong một request; mọi chức năng của `tools/*.py` đều gọi được qua API.

### P3: Dashboard
1. Auth: users, sessions, CSRF, lockout, role; lệnh `qis admin`.
2. Admin API và SSE `/admin/api/events`.
3. Khung SPA (router, layout, theme sáng/tối, trang đăng nhập), sau đó lần lượt các trang ở §9.
4. Runtime settings dùng schema chung để sinh form tự động và có hiệu lực ngay.

**Xong khi:** mọi setting ở §13 chỉnh được trên web; đổi `max_pending` thì admission áp dụng giá trị mới ngay; tạo và thu hồi key trên web thì API phản ánh ngay.

### P4: Installer và phát hành
1. `hardware.py` với logic chọn tier, dùng chung cho installer, `qis doctor` và dashboard.
2. `models.yaml`, và bake Viggle Q8/BF16 rồi đẩy lên HF (sau khi xác nhận license).
3. `install.ps1` trước: test trên máy này và trên một VM Windows sạch có GPU. Sau đó `install.sh` (Ubuntu 22.04/24.04).
4. Service (WinSW, systemd), cloudflared, benchmark sau khi cài, `qis update` có rollback.
5. CI build release (web + server + manifest + hash), host script trên Cloudflare Pages/R2.

**Xong khi:** trên một máy Windows sạch có NVIDIA, chạy **một lệnh** là cài xong, service tự khởi động cùng máy, gọi được API qua hostname của tunnel, và dashboard đăng nhập được.

### P5: Mesh (thiết kế riêng)
Xem §15. Sẽ có tài liệu `docs/MESH.md` riêng trước khi code.

---

## 18. Chiến lược test

| Tầng | Công cụ | Nội dung |
|---|---|---|
| Unit | pytest | cost model, điểm dispatcher, parse size/aspect, `dims()`, map tham số OpenAI, ký URL, auth |
| Golden | pytest | Workflow JSON của engine mới so với code cũ (`gen.py`, `tools/`), snapshot trong `tests/golden/` |
| Integration | pytest + fake ComfyUI (FastAPI giả) | Admission/429, fair-share, cancel/interrupt, crash rồi retry, restart rồi phục hồi, SSE, keepalive |
| E2E GPU | pytest `-m gpu`, chạy tay trên máy thật | Gen thật mỗi preset, so kích thước, kênh alpha, thời gian với benchmark |
| Frontend | Vitest + Playwright (smoke) | Đăng nhập, đổi setting, tạo key, chạy playground |
| Installer | VM sạch (Windows, Ubuntu) | Cài mới, cài lại (idempotent), update, uninstall |

---

## 19. Rủi ro và câu hỏi còn mở

| # | Rủi ro / câu hỏi | Ảnh hưởng | Hướng xử lý |
|---|---|---|---|
| R1 | **License Qwen-Image 2.1 và các bản phái sinh chỉ cho dùng phi thương mại** | Dùng asset trong game thương mại cần license riêng. Phân phối lại GGUF đã bake có thể không được phép | Hiển thị `commercial_ok` trên dashboard và trong `/v1/models`. Liên hệ `model-business@notice.qwencloud.com`. Để repo HF private cho tới khi rõ |
| R2 | Keepalive bằng khoảng trắng khiến lỗi trả về HTTP 200 | Client OpenAI thuần phải đọc `error` trong body | Ghi rõ trong tài liệu, có thể tắt bằng setting. Khuyến nghị client native dùng `/v1/jobs` |
| R3 | ComfyUI đổi API hoặc node giữa các version | Hỏng workflow | Pin commit. Golden test cộng e2e trước mỗi lần nâng pin |
| R4 | Enhance chiếm GPU 80–140 s trên máy yếu | Queue tắc | Có cost riêng và quota riêng cho enhance, cache toàn cục, có thể tắt enhance theo tier |
| R5 | RAM 16 GB không đủ để stream encoder 9.35 GB | Rất chậm hoặc OOM | Installer cảnh báo; tier yêu cầu RAM ≥ 32 GB |
| R6 | Windows service chạy trong session 0 | CUDA vẫn hoạt động, nhưng đường dẫn và quyền khác | Dùng đường dẫn tuyệt đối, chạy dưới account riêng, `qis doctor` kiểm tra |
| R7 | Phân phối repo private cho `irm \| iex` | Installer cần token | Host gói release trên R2 (URL có ký hoặc public), hoặc cho repo public phần installer |
| Q1 | Tên sản phẩm và domain cho `get.<domain>` | – | Cần chốt trước P4 |
| Q2 | Có cần API key dùng được trên toàn mesh ngay từ P5 không? | Quyết định thiết kế đồng bộ `cluster` | Bàn khi thiết kế P5 |

---

## 20. Ghi chú triển khai (các điểm khác bản thiết kế v1)

Bản cài đặt thực tế (M0–M7) có vài chỗ lệch khỏi bản thiết kế. Thiết kế vẫn giữ nguyên, bảng dưới ghi lại lựa chọn cuối cùng và lý do:

| Mục | Thiết kế v1 | Thực tế | Lý do |
|---|---|---|---|
| Cấu trúc repo (§16) | `server/`, `engine/` riêng | Một package `qis/` (`qis/api`, `qis/engine`, `qis/scheduler.py`, …) | Một wheel duy nhất, import gọn, cài bằng `pip install -e` |
| DB (§2.1) | SQLAlchemy + Alembic | `sqlite3` chuẩn + danh sách migration trong code (`PRAGMA user_version`) | Schema nhỏ, không cần ORM; bớt phụ thuộc |
| Python | 3.12 | 3.13 (giống bản ComfyUI portable đã chạy ổn) | Tránh sai khác với bản tham chiếu |
| PyTorch | cu128 / cu126 | cu130 nếu driver ≥ 580, cu128 nếu driver ≥ 570, cu126 cho Pascal | Bản tham chiếu chạy torch 2.13 cu130 |
| Model (§12.4–12.5) | Tự bake Viggle rồi host HF riêng | Manifest dùng nguồn công khai: Viggle v0.2.1 GGUF (Abiray), base (unsloth), encoder/VAE (Comfy-Org), Real-ESRGAN (GitHub). File tự bake vẫn dùng được và được hiện là "local" | Không phải phân phối lại weights; sha256 lấy từ LFS oid |
| Mô hình op | Pixelize gộp vào workflow ComfyUI | Pixelize chạy ở CPU pool của gateway (cùng code `pixelize.py`) | Không giữ GPU trong lúc làm việc CPU; dùng lại được cho `/v1/tools/pixelize` |
| MCP (§6.8) | – | Tự cài JSON-RPC Streamable HTTP (không dùng MCP SDK); SSE có progress và ping | Ít phụ thuộc; qua được giới hạn 100 s của Cloudflare |
| Mask trong `/images/edits` | Inpaint | Chưa hỗ trợ (trả 400 kèm hướng dẫn) | Chưa có op inpaint; để làm sau |
| Admission | Theo tổng ETA | Thêm: queue rỗng thì luôn nhận job (miễn là dưới `max_cost_s`) | Job lớn không bị kẹt khi node rảnh |
