# QIS Mesh: hub và worker

> Trạng thái: **bản thiết kế v1 cho P5**, viết ngày 2026-10-09, trước khi code.
> Phạm vi: biến các node QIS hiện có thành **worker** nối ra một **QIS Hub** trung tâm. Hub là địa chỉ duy nhất nhận request.
> Tài liệu này là **hợp đồng** giữa hai repo: `qis-hub` (GitLab `ai-lab/qis-hub`) và `qwen-image-service` (worker mode). Hai người làm song song phải đọc được §2, §4 và §5 như một spec, không cần hỏi lại.

## 0. Tóm tắt

```
 Client (OpenAI SDK, curl, Unity, batch script)
        │ HTTPS  https://qis.runnow.cc
        ▼
 Cloudflare edge ── tunnel ──► QIS Hub (Docker, FastAPI :8101, SQLite WAL)
                                 │  ▲  WebSocket outbound từ worker (wss://qis.runnow.cc/worker/ws)
              presigned PUT/GET  │  │
   R2 bucket ◄───────────────────┼──┼─────────────── worker 1 (Windows, RTX 3060, QIS worker mode)
   (in/ out/)  ◄─────────────────┘  └─────────────── worker 2 (RTX 3050) …
```

- **Hub** nhận mọi request (OpenAI-compatible + jobs API), giữ queue bền trong SQLite, chấm điểm worker theo năng lực đo được và giao job.
- **Worker** là node QIS hiện có bật `[worker] enabled = true`. Worker **tự nối ra** hub bằng WebSocket, nhận job, chạy bằng engine/scheduler/ComfyUI sẵn có, upload ảnh lên R2 bằng URL presigned do hub cấp, rồi báo kết quả.
- **R2** là nơi duy nhất chứa ảnh: input do hub stream lên, output do worker PUT lên. Credential R2 chỉ nằm ở hub.
- Worker có thể rớt **bất cứ lúc nào**: mọi giao việc đều có **lease**, có `attempt_id`, kết quả commit idempotent.

### So với DESIGN §15

| §15 dự kiến | Mesh v1 chọn | Lý do |
|---|---|---|
| Mesh ngang hàng, `RemoteNodeExecutor` gọi `/v1/jobs` của node khác, peer token ed25519 | Hub-and-workers; worker nối **ra**; không node nào nhận request trực tiếp | Worker là máy Windows sau NAT, không cần mở cổng, không cần tunnel riêng cho từng máy |
| Đồng bộ scope `cluster` (user, key, preset) giữa node | User, key, quota chỉ ở hub. Preset vẫn đọc từ `presets.yaml` của worker (§5.3) | Một nguồn chính duy nhất, không phải đồng bộ |
| BlobStore S3/R2 | R2 cho mọi ảnh đi qua mạng; blob store local của worker giữ nguyên làm cache làm việc | Không phải viết lại `Storage` |
| `node_id` + `node.json` | Dùng lại: `worker_id = node_id` | Đã có sẵn và bền qua update |
| `/v1/node/info` | Thay bằng message `hello` (cùng nội dung, đẩy chủ động) | Hub không gọi vào được worker |

---

## 1. Kiến trúc

### 1.1 Thành phần phía hub (`qis-hub`, scaffold từ pixon-webapp)

Scaffold bằng `scaffold.py` của skill pixon-webapp (slug `qis-hub`, prefix env `QISHUB_`), thay domain mẫu `item` bằng các module dưới. Chạy **một process uvicorn** (một worker): dispatcher và registry giữ trạng thái trong RAM, DB là bản bền.

| Module | Vai trò |
|---|---|
| `app/hub/protocol.py` | Model Pydantic cho mọi message WS (§2). **Bản sao y hệt** `qis/mesh/protocol.py` của repo QIS |
| `app/hub/gateway.py` | Endpoint `GET /worker/ws`: xác thực join token, vòng đọc/ghi từng session, chuyển message cho registry/dispatcher |
| `app/hub/registry.py` | `WorkerRegistry`: trạng thái từng worker (session, slot, heartbeat mới nhất, preset + fingerprint, mô hình thời gian, độ tin cậy, quarantine) |
| `app/hub/perf.py` | Đặc trưng op và mô hình thời gian học theo từng worker, từng class (§3.1) |
| `app/hub/dispatcher.py` | Queue, chấm điểm job, chọn worker, lease, retry, timeout |
| `app/hub/jobs.py` | `JobService`: submit, admission, cancel, idempotency, event bus, webhook |
| `app/hub/r2.py` | `BlobStore`: presign PUT/GET/HEAD (botocore, tính offline), HEAD/GET/DELETE thật bằng `httpx` qua URL presigned, list theo prefix, `DeleteObjects` theo lô |
| `app/hub/present.py` | Sync keepalive (port từ `qis/api/present.py`), đường b64 streaming giới hạn bộ nhớ (§5.6) |
| `app/routers/openai.py`, `jobs.py`, `files.py`, `batches.py` | API public `/v1/*` (§5) |
| `app/routers/hub_admin.py` | API cho console: overview, workers, requests, keys, settings (§7) |
| `app/hub/sweeper.py` | Dọn R2 theo retention, dọn output của attempt thua (§4.5) |
| `app/hub/stats.py` | `stats_minute`, `worker_stats_minute`, p95 |
| `app/console/views.js` | Thêm view Overview, Workers, Requests, API Keys theo DESIGN v2 của template |

Biến môi trường của hub:

| Env | Mặc định | Ghi chú |
|---|---|---|
| `QISHUB_PORT` | `8101` | Docker map `8101:8101` |
| `QISHUB_PUBLIC_URL` | `https://qis.runnow.cc` | Dùng để dựng link trong job view; không lấy từ header `Host` |
| `QISHUB_DB_PATH` | `/data/hub.db` | SQLite WAL, volume `./data:/data` |
| `QISHUB_WORKER_TOKENS` | (bắt buộc) | Join token dùng chung, dạng `qisw_<32 base62>`; cho phép 2 giá trị cách nhau dấu phẩy để xoay vòng |
| `QISHUB_KEY_PEPPER` | (bắt buộc) | Pepper cho hash API key |
| `QISHUB_R2_ACCOUNT_ID`, `QISHUB_R2_ACCESS_KEY_ID`, `QISHUB_R2_SECRET_ACCESS_KEY`, `QISHUB_R2_BUCKET` | (bắt buộc) | |
| `QISHUB_R2_ENDPOINT` | `https://<account>.r2.cloudflarestorage.com` | Đặt sang MinIO khi dev/test |
| `QISHUB_R2_APPLY_LIFECYCLE` | `false` | Chỉ bật khi devops cấp token có quyền quản trị bucket. Key S3 hiện có **không** có quyền này (§4.4) |
| `QISHUB_JWT_SECRET` … | theo template | RBAC console |

Các núm chỉnh lúc chạy (`retention_days`, trọng số scheduler, ngưỡng admission, `url_ttl_s`, `b64_concurrency`…) nằm trong bảng `hub_settings`, sửa trên console (§7), có hiệu lực ngay.

Cloudflare: tunnel trỏ `qis.runnow.cc` → `http://qis-hub:8101`. Policy Access: **Bypass** cho `/v1/*`, `/worker/ws`, `/health`; console có thể để JWT của template lo hoặc thêm Allow-email. WebSocket qua tunnel bật sẵn; Cloudflare cắt WS im lặng quá 100 s nên heartbeat 10 s (§2.5) là bắt buộc.

### 1.2 Thành phần phía worker (`qwen-image-service`)

| Module | Vai trò |
|---|---|
| `qis/mesh/protocol.py` | Bản **gốc** của schema message. Chỉ import `pydantic`, không import gì khác của `qis` |
| `qis/mesh/link.py` | `HubLink`: client WS, backoff, `hello`/`heartbeat`, máy trạng thái kết nối (§8.3) |
| `qis/mesh/runner.py` | `AttemptRunner`: tải input, dựng `JobSpec`, submit vào `JobService` local, relay progress, upload output, gửi `result`/`failure`, xử lý `cancel` |
| `qis/mesh/state.py` | Snapshot trạng thái worker cho `/healthz`, `/admin/api/status`, tray |
| `qis/cli.py` | Nhóm lệnh `qis worker join|leave|status|drain|resume|run` (§8.1) |
| `qis/tray.py` | Thêm mục Hub, màu theo trạng thái hub, menu Drain/Resume (§8.3) |

**Những gì đổi trong QIS:**

1. `JobService.submit(..., internal=True)`: bỏ qua giới hạn theo key (`max_pending`, `rpm`) và `queue_full`, vì hub đã làm admission. Vẫn giữ `max_cost_s`. Job local tạo với `kind="hub"`, `api_key_id=NULL`, `origin_node="hub"`, `idempotency_key=<attempt_id>` (giao trùng một attempt thì trả lại đúng job local cũ).
2. Engine, `LocalComfyExecutor`, CPU pool, retry OOM `degraded`, restart ComfyUI: **giữ nguyên**. Retry local (OOM, ComfyUI crash) chạy bên trong một attempt của hub; hub chỉ thấy kết quả cuối.
3. Khi `worker.enabled = true` và `worker.accept_local_jobs = false` (mặc định), các route tạo job (`POST /v1/images/*`, `POST /v1/jobs`, `/v1/tools/*`, `/mcp`) trả `409 worker_mode`. Gateway vẫn bind `127.0.0.1` như hiện tại; `/healthz`, `/readyz`, `/admin` (dashboard local) và `/v1/node/info` vẫn chạy để tray và người vận hành xem máy. Không cài `cloudflared` cho máy worker.
4. Khởi động lại process worker: job local `kind="hub"` đang `running`/`queued` bị **huỷ**, không chạy lại (hub đã hoặc sẽ giao lại, §2.7).
5. Output đã được hub chấp nhận thì đặt hạn còn 1 h trên blob store local (không cần giữ 72 h nữa).

---

## 2. Giao thức worker ↔ hub

### 2.1 Transport

- **WebSocket outbound**, worker mở `wss://qis.runnow.cc/worker/ws` (LAN/dev: `ws://<host>:8101/worker/ws`). Chỉ text frame, mỗi frame một object JSON UTF-8.
- **Không có HTTP long-poll fallback** ở v1: mọi worker là máy nội bộ, Cloudflare tunnel hỗ trợ WS. (Xem Quyết định mở Q1.)
- Kích thước frame tối đa **2 MiB** (cả hai chiều). Ảnh không bao giờ đi qua WS, trừ preview JPEG cho `partial_images` (≤ 256 KiB mỗi cái).
- Dùng thêm WS ping/pong của thư viện (20 s) để phát hiện TCP chết; heartbeat ứng dụng (§2.5) mới là nguồn chính cho lease.

### 2.2 Xác thực và danh tính

1. Worker gửi upgrade request với header `Authorization: Bearer <join_token>` và `X-Qis-Worker-Id: <worker_id>`.
2. Hub so `join_token` với từng giá trị trong `QISHUB_WORKER_TOKENS` bằng `hmac.compare_digest`. Sai thì **accept rồi close ngay** với code `4401` (trình duyệt và thư viện đọc được close code, không đọc được HTTP 401 của upgrade).
3. `worker_id` = `node_id` trong `<data>/node.json` (dạng `node_<ULID>`), bền qua update và cài lại (trừ khi xoá data dir). Hub không duyệt worker: worker_id lạ thì tự tạo dòng mới trong bảng `workers`.
4. Trong 10 s sau khi mở, frame đầu tiên của worker phải là `hello`. Quá hạn thì close `4400`.
5. Cùng `worker_id` nối lần nữa khi session cũ còn mở: hub đóng session **cũ** bằng `4409 replaced`, giữ session mới (thường là cùng process vừa reconnect). Attempt của session cũ được chuyển sang session mới theo danh sách `running` trong `hello`.

### 2.3 Envelope

Mọi message có các trường chung, phần còn lại nằm phẳng cùng cấp:

```json
{"v": 1, "type": "heartbeat", "id": "m_01JA…", "ts": 1760000000.123}
```

- `v`: phiên bản protocol (`PROTO = 1`). Hub chấp nhận `v` trong `[PROTO_MIN, PROTO_MAX]`, ngoài khoảng thì close `4426`.
- `id`: id message (ULID, prefix `m_`), dùng cho log và `reply_to`.
- `ts`: đồng hồ bên gửi, **chỉ để log**. Mọi lease và timeout tính theo đồng hồ hub; timing worker gửi lên đều là khoảng thời gian (giây), không phải mốc.
- Trường lạ thì bỏ qua (forward compatible). Thiếu trường bắt buộc thì bên nhận gửi `error {reply_to, code:"bad_message"}` và bỏ message đó; không đóng kết nối.

### 2.4 Danh mục message

**Worker → hub**

| type | Khi nào | Trường chính |
|---|---|---|
| `hello` | Frame đầu tiên của mỗi session | Xem ví dụ dưới |
| `heartbeat` | Mỗi `heartbeat_s` (10 s) | `state`, `running[]`, `free_slots`, `loaded_model`, `gpu`, `cpu_pct`, `ram_used_mb`, `comfy` |
| `accept` | Trả lời `assign` trong `accept_timeout_s` (10 s) | `attempt_id` |
| `reject` | Không nhận được `assign` | `attempt_id`, `reason`: `busy \| draining \| preset_unknown \| model_missing \| vram_insufficient \| invalid` , `message` |
| `progress` | Đổi phase, hoặc tiến độ, tối đa 1 lần/giây/attempt | `attempt_id`, `phase`: `fetching \| queued_local \| running \| uploading`, `op_index`, `op`, `step`, `steps`, `pct` (0–1), `preview_jpeg_b64?` |
| `upload_request` | Job local xong, xin URL PUT | `attempt_id`, `files[]: {name, content_type, bytes}` |
| `result` | Đã PUT xong mọi file | §2.6 |
| `failure` | Attempt thất bại hoặc đã huỷ xong | `attempt_id`, `code`, `message`, `retryable`, `timings` |
| `state` | Worker đổi trạng thái (drain, resume, engine down/up) | `state`, `reason` |
| `bye` | Sắp đóng có chủ đích (stop service, `qis worker leave`) | `reason`: `shutdown \| leave \| drained`, `abandon[]`: attempt_id bỏ dở |

**Hub → worker**

| type | Khi nào | Trường chính |
|---|---|---|
| `welcome` | Trả lời `hello` | `session_id`, `heartbeat_s`, `lease_ttl_s`, `accept_timeout_s`, `paused`, `resume{attempt_id: "keep"\|"drop"}`, `limits{max_frame_bytes}` |
| `heartbeat_ack` | Mỗi `heartbeat` | `server_time` |
| `assign` | Giao một attempt | §2.6 |
| `upload_grant` | Trả lời `upload_request` | `attempt_id`, `files[]: {name, key, url, method:"PUT", headers, expires_at}` |
| `upload_denied` | Attempt không còn hiệu lực (job đã xong bởi attempt khác, đã huỷ) | `attempt_id`, `reason`: `stale \| canceled \| too_large` |
| `result_ack` | Trả lời `result` | `attempt_id`, `accepted` (bool), `reason?`: `stale \| missing_object \| size_mismatch \| bad_key` |
| `cancel` | Client huỷ, timeout, hoặc attempt thua | `attempt_id`, `reason`: `client \| timeout \| superseded \| admin` |
| `pause` / `resume` | Admin bấm Pause/Resume trên console | `reason` |
| `error` | Message không hợp lệ | `reply_to`, `code`, `message` |

`state` của worker: `warming` (ComfyUI đang khởi động hoặc đang warm-up, kèm `warm_eta_s` nếu ước được; hub không giao việc), `ready`, `draining` (làm nốt, không nhận mới), `paused` (hub bảo dừng). "Bận" không phải state; hub suy ra từ slot.

Ví dụ `hello`:

```json
{"v": 1, "type": "hello", "id": "m_01JA…", "ts": 1760000000.1,
 "worker_id": "node_01J9Z…", "name": "PIXON-3060-01", "version": "0.9.3", "proto": 1,
 "gpu": {"name": "NVIDIA GeForce RTX 3060", "vram_mb": 12288, "driver": "581.57", "compute_cap": "8.6"},
 "ram_mb": 65536, "tier": "T2",
 "model_files": [
   {"role": "diffusion", "mode": "turbo", "file": "qwen-image-2.1-viggle-Q8_0.gguf", "bytes": 7612345678,
    "manifest_id": "qwen-image-2.1-viggle-q8", "sha256": "4be1…"},
   {"role": "diffusion", "mode": "base", "file": "qwen-image-2.1-Q4_K_M.gguf", "bytes": 4604557984,
    "manifest_id": "qwen-image-2.1-q4km", "sha256": "77c0…"},
   {"role": "text_encoder", "file": "qwen3vl_8b_int8_convrot.safetensors", "bytes": 9350000000,
    "manifest_id": "qwen3vl-8b-int8", "sha256": "a9d3…"}],
 "modes": ["turbo", "base"], "upscalers": ["realesrgan-x2", "realesrgan-x4"],
 "ops": ["generate", "enhance_prompt", "refine", "upscale", "outpaint", "pixelize", "remove_bg", "compose", "fidelity"],
 "presets": {"qwen-image-2.1-turbo": {"description": "…", "ops": [{"op": "generate", "mode": "turbo", "size": 1024}],
                                      "files": ["qwen-image-2.1-viggle-Q8_0.gguf", "qwen3vl_8b_int8_convrot.safetensors",
                                                "qwen_image_2.1_vae_bf16.safetensors"],
                                      "fingerprint": "mf_3c9a07d2e41b5f60"}},
 "limits": {"max_n": 8, "max_refs": 3, "max_pixels": 16000000, "max_input_bytes": 20971520},
 "slots": {"gpu": 1, "prefetch": 1, "cpu": 2},
 "perf_model": {"source": "history", "classes": {
   "t2i":  {"a1": 52.0, "aN": 24.0, "b": 2.4, "samples": 140},
   "edit": {"a1": 79.0, "aN": 30.0, "b": 2.5, "samples": 35}},
   "fallback": {"overhead_s": 6.0, "s_per_step": {"turbo": 4.7, "base": 4.9}, "ref_mp": 0.59,
                "calib": {"generate": 1.0}, "enhance_s": 110.0, "upscale_s_per_mp": 3.0}},
 "state": "ready", "warm_eta_s": null, "loaded_model": "qwen-image-2.1-viggle-Q8_0.gguf",
 "running": [{"attempt_id": "att_01JA…", "phase": "running"}],
 "unacked": [{"attempt_id": "att_01JB…"}]}
```

- `presets` là preset **mà worker chạy được** với bộ model **đã cấu hình** (`Presets.available`), giữ nguyên định nghĩa từ `presets.yaml`. Ví dụ worker 3050 (T1) có file base_q8 trên đĩa nhưng không cấu hình, nên preset cần `base_q8` không xuất hiện ở đó; hub không bao giờ giao preset đó cho nó.
- `model_files`: danh tính từng file model đang cấu hình. `manifest_id` có khi file khớp `models.yaml`, ngược lại `null` (ví dụ file turbo Q4_K_M tự merge trên máy 3050, khác file `turbo-q4km` của manifest). `sha256` lấy từ manifest nếu khớp; file tự merge thì worker băm một lần ở nền và cache vào `<data>/model_hashes.json` theo `(path, bytes, mtime)`; trong lúc chưa băm xong gửi `null` và dùng `name:bytes` làm danh tính tạm.
- `presets[*].files` là các file preset đó dùng (unet theo mode, encoder, VAE, upscaler); `fingerprint = "mf_" + sha256(join(sorted("role:sha256|name:bytes")))[:16]`. Hai worker cùng fingerprint cho cùng seed thì ra cùng ảnh (trong giới hạn của GPU); khác fingerprint thì **không**. Hub ghi `worker_id` và `model_fingerprint` vào mọi attempt và trả lại cho client (§5.1).
- `perf_model`: tham số mô hình thời gian (§3.1) worker tự tính từ lịch sử job local (`source: "history"`, cần ≥ 5 mẫu mỗi class), cộng `fallback` là `PerfS` đã calibration để hub suy ra giá trị khởi đầu cho class chưa có lịch sử (`source: "calibration"`).
- `running`: attempt đang chạy dở trong process này (sau reconnect). `unacked`: attempt đã xong nhưng chưa nhận `result_ack`; worker sẽ gửi lại `result`/`failure` ngay sau `welcome`.

Ví dụ `heartbeat`:

```json
{"v": 1, "type": "heartbeat", "id": "m_…", "ts": 1760000010.1, "state": "ready",
 "running": [{"attempt_id": "att_01JA…", "phase": "running", "pct": 0.4}],
 "free_slots": {"gpu": 0, "prefetch": 1, "cpu": 2}, "loaded_model": "qwen-image-2.1-viggle-Q8_0.gguf",
 "gpu": {"util": 98, "vram_used_mb": 10920, "vram_total_mb": 12288, "temp_c": 71, "power_w": 168},
 "cpu_pct": 22.5, "ram_used_mb": 30210, "comfy": "ready"}
```

### 2.5 Heartbeat và lease

- Worker gửi `heartbeat` mỗi `heartbeat_s` = **10 s**. Hub trả `heartbeat_ack`.
- Mỗi attempt đang sống có `lease_expires_at`. Hub gia hạn `= now + lease_ttl_s` (**45 s**) khi nhận `accept`, `progress`, `upload_request`, hoặc `heartbeat` có attempt đó trong `running`.
- Heartbeat **không** liệt kê một attempt mà hub nghĩ worker đang giữ (và attempt đó đã `accepted` hơn 15 s): hub coi attempt `lost` ngay, không chờ hết lease.
- Phía worker: không nhận được frame nào từ hub trong **30 s** thì tự đóng và reconnect.

### 2.6 Vòng đời một attempt

```
hub: assign ──► worker: accept ──► progress(fetching) ──► progress(queued_local) ──► progress(running …)
     ──► upload_request ──► hub: upload_grant ──► worker PUT R2 ──► result ──► hub: result_ack
```

`assign`:

```json
{"v": 1, "type": "assign", "id": "m_…", "ts": 1760000000.5,
 "attempt_id": "att_01JA…", "job_id": "job_01JA…", "attempt_no": 1,
 "kind": "images.edits",
 "payload": {"request": {"model": "qwen-image-2.1-turbo", "prompt": "give him a wooden shield", "n": 1,
                         "size": "auto", "quality": "auto", "output_format": "png", "seed": 42},
             "refs": ["file_01JA…"]},
 "inputs": [{"file_id": "file_01JA…", "url": "https://<acct>.r2.cloudflarestorage.com/qis/in/2026/10/09/file_01JA….png?X-Amz-…",
             "bytes": 734512, "content_type": "image/png", "width": 1024, "height": 1024}],
 "output": {"format": "png", "compression": null, "partial_images": 0},
 "model_fingerprint": "mf_3c9a07d2e41b5f60", "est_s": 92.0, "deadline_s": 368, "priority": "interactive"}
```

- `kind` và `payload`:
  - `images.generations`: `payload.request` = các trường OpenAI (`model, prompt, n, size, quality, background, output_format, output_compression, style`) cộng các khoá extra (`seed, steps, negative, enhance, palette…`). Worker gọi lại đúng logic `_build()` của `qis/api/openai_images.py` (preset → `JobSpec`), với `refs = None`.
  - `images.edits`: như trên, `payload.refs` là danh sách `file_id` của hub theo thứ tự ảnh.
  - `pipeline`: `payload.spec` là `JobSpec` (DESIGN §6.3); các giá trị file trong `inputs` và tham chiếu trực tiếp là `file_id` của hub.
- Worker tải từng `inputs[i].url` (GET, timeout 60 s, tối đa `max_input_bytes`), chạy `normalize_image`, lưu vào storage local, được `file_id` local. Sau đó thay **mọi** chuỗi khớp `file_id` hub (trong `refs`, `spec.inputs`, trường ảnh của op) bằng id local. Hub và worker cùng dạng `file_<ULID>` nên không cần đổi format.
- Định dạng output (`png | webp | jpeg`) do **worker** chuyển (`present.convert`) trước khi upload, áp cho ảnh chính của output cuối. Ảnh `raw` và `extra` luôn là PNG.
- `deadline_s`: thời gian tối đa từ `accept` tới `result`. Quá hạn thì hub gửi `cancel{reason:"timeout"}` (§3.6).

`upload_request` / `upload_grant`:

- Tên file do worker đề xuất, phải khớp `^[a-z0-9][a-z0-9\-]{0,39}\.(png|webp|jpg|json|txt)$`. Quy ước: ảnh chính `o{step}-{i}.{ext}`, ảnh extra `o{step}-{i}-{name}.png` (ví dụ `o1-0-preview.png`), ảnh raw `raw-{i}.png`.
- Hub ghép key `out/{yyyy}/{mm}/{dd}/{job_id}/a{attempt_no}/{name}` (ngày = ngày tạo job, UTC), presign PUT với `Content-Type` đã ký, TTL **15 phút**. File nào vượt `max_output_bytes` (50 MiB) thì cả request bị `upload_denied{reason:"too_large"}`.
- Worker PUT từng file với đúng header trong `headers`, thử lại 3 lần (1 s, 4 s, 16 s). URL hết hạn thì gửi lại `upload_request` cho các file còn thiếu.

`result`:

```json
{"v": 1, "type": "result", "id": "m_…", "ts": 1760000075.0, "attempt_id": "att_01JA…", "job_id": "job_01JA…",
 "outputs": [{"step": 0, "op": "generate",
              "images": [{"key": "out/2026/10/09/job_01JA…/a1/o0-0.png", "content_type": "image/png", "bytes": 1534001,
                          "width": 1024, "height": 1024, "has_alpha": false, "sha256": "9f2c…"}],
              "extra": {}, "text": null, "data": {}}],
 "raw": [],
 "meta": {"revised_prompt": "…", "seeds": [42], "degraded": false, "cold_start": false,
          "model_fingerprint": "mf_3c9a07d2e41b5f60"},
 "timings": {"fetch_s": 0.8, "local_queue_s": 0.0, "run_s": 91.4, "upload_s": 1.2,
             "ops": [{"op": "generate", "class": "edit", "seconds": 91.4, "estimate": 92.0, "kind": "gpu",
                      "calls": [{"seconds": 91.4, "sample_s": 13.1}]}]},
 "worker_version": "0.9.3"}
```

- `cold_start = true` khi lần chạy này phải nạp model khác với `loaded_model` trước đó (executor báo đổi unet).
- `calls[]`: một phần tử cho mỗi lần gọi ComfyUI của op; `sample_s` = khoảng thời gian giữa event progress đầu và cuối của KSampler trong call đó (0 với op không có sampling). Đây là dữ liệu để hub tách chi phí cố định và chi phí theo step (§3.1).
- `meta.model_fingerprint` phải bằng fingerprint của preset trong `assign` (job pipeline: fingerprint của các file đã dùng). Khác nhau (worker vừa đổi model giữa chừng) thì hub vẫn nhận kết quả nhưng ghi đúng giá trị worker báo.
- `outputs[].text` / `data` chứa kết quả không phải ảnh (`enhance_prompt`, `fidelity`), tối đa 256 KiB.

Hub xử lý `result` (theo thứ tự):
1. Attempt không tồn tại → `result_ack{accepted:false, reason:"stale"}`.
2. Attempt đã `succeeded` (gửi trùng) → `result_ack{accepted:true}`, không làm gì thêm (**idempotent**).
3. Mỗi `key` phải bắt đầu bằng prefix của attempt và đã nằm trong `upload_grant`, nếu không → `bad_key`. `HEAD` từng key qua URL presigned: 404 → `missing_object`; `Content-Length` khác `bytes` → `size_mismatch`. Các lỗi này tính như `failure{code:"upload_failed"}`.
4. Trong **một transaction**: `UPDATE jobs SET status='succeeded', winning_attempt_id=?, result=?, finished_at=? WHERE id=? AND status NOT IN (terminal)`. `rowcount = 0` (job đã kết thúc bởi attempt khác hoặc bị huỷ) → attempt `superseded`, trả `accepted:false, reason:"stale"`, xoá ngay object của attempt đó. `rowcount = 1` → attempt `succeeded`, các attempt khác của job đang sống nhận `cancel{reason:"superseded"}`.
5. Gửi `result_ack{accepted:true}`, phát event `succeeded`, ghi stats, gọi webhook.

**Kết quả đầu tiên hợp lệ thắng**, kể cả khi đến từ attempt đã bị coi là `lost` (lease hết hạn), miễn là job chưa kết thúc. Kết quả đến muộn sau khi job đã kết thúc thì bị loại và object bị xoá.

`failure.code` và cách hub xử lý:

| code | Ví dụ gốc trong QIS | Hub làm gì | Tính lỗi cho worker |
|---|---|---|---|
| `invalid_input` | `invalid_request`, `workflow_rejected`, `OpError`, ảnh hỏng | Job `failed` ngay, không retry (client nhận 400) | Không |
| `oom` | `ComfyError.is_oom` sau khi retry `degraded` local đã thất bại | Retry trên worker có `vram_mb` lớn hơn; không còn ai thì `failed oom` | 0.5 |
| `cancelled` | Đã huỷ theo `cancel` | Không làm gì thêm (job đã ở trạng thái hub quyết định) | Không |
| `engine_unavailable` | `ComfyUnavailable` | Retry nơi khác | Có |
| `input_fetch_failed` | GET R2 lỗi, quá thời gian | Retry nơi khác sau 10 s | Không (lỗi lưu trữ) |
| `upload_failed` | PUT R2 thất bại sau khi đã thử lại | Retry nơi khác | Có |
| `timeout` | Job local vượt `timeout` của QIS | Retry nơi khác | Có |
| `internal` | Mọi lỗi khác (`server_error`, `execution_error`, `interrupted`) | Retry nơi khác | Có |

`retryable` trong message chỉ là gợi ý; hub quyết định theo bảng trên.

### 2.7 Mất kết nối, reconnect, in-flight

**Backoff của worker:** full jitter, `delay = random(0, min(60, 1 × 2^k))` giây, `k` tăng sau mỗi lần nối thất bại, reset về 0 khi session sống ổn định ≥ 60 s. Theo close code:

| Close code | Ý nghĩa | Worker làm |
|---|---|---|
| `1000` / `1001` / `1012` | Đóng bình thường, hub restart | Nối lại sau 1–3 s (jitter) |
| `4400` | Thiếu `hello` hoặc `hello` sai | Backoff thường, log lỗi |
| `4401` | Join token sai | State `auth_failed`, thử lại mỗi 5 phút (token mới cần `qis worker join` rồi restart service) |
| `4409` | Bị session khác cùng `worker_id` thay thế | Chờ 60 s rồi mới nối; tray báo "worker_id đang dùng ở nơi khác" nếu lặp lại |
| `4426` | Protocol không hỗ trợ | State `outdated`, thử lại mỗi 10 phút |

**Phía worker khi mất kết nối:** job local vẫn chạy tiếp. Kết quả cuối (`result`/`failure`) được giữ trong outbox RAM và gửi lại sau `welcome`. Upload R2 chỉ cần URL đã cấp; nếu cần URL mới mà chưa có kết nối thì chờ. Output vẫn nằm ở storage local (72 h) nên không mất.

**Phía hub khi session đóng:** attempt của worker đó **không** bị requeue ngay; chúng giữ nguyên tới khi hết lease (≤ 45 s kể từ lần gia hạn cuối). Worker nối lại kịp và liệt kê attempt trong `hello.running` → `welcome.resume[att] = "keep"`, lease gia hạn, job chạy tiếp. Không kịp → attempt `lost`, job về `queued` (`attempts++`). Nếu worker sau đó vẫn gửi `hello` có attempt này: job đã giao cho attempt mới thì `resume = "drop"` (worker huỷ job local, xoá file); job vẫn đang `queued` thì `keep`, hub gỡ job khỏi queue và gắn lại attempt cũ.

**`bye` có `abandon[]`** (stop service): hub đánh `lost` các attempt đó ngay và requeue, không chờ lease.

**Process worker khởi động lại:** `hello.running = []`. Mọi attempt hub đang gán cho worker này (trừ những attempt trong `unacked`) bị `lost` ngay lúc nhận `hello`.

---

## 3. Lập lịch

### 3.1 Thời gian chạy: học riêng từng worker, từng loại op

Số đo thật cho thấy thời gian một job **bị chi phối bởi chi phí cố định**, không phải số step: trên RTX 3060 12 GB, 5 step sampling của turbo 1024² chỉ mất 9–17 s trong tổng 61–70 s; phần còn lại là text encoder 9.4 GB và DiT 7.4 GB thay nhau vào/ra VRAM (không cùng nằm được trong 12 GB) cộng VAE. Edit 1 ref ~92 s. RTX 3050 6 GB (T1) chạy generate chậm hơn ước lượng mặc định của QIS 1.87 lần (~74 s/job). Vì vậy hub **không** dùng bảng theo tier hay một hệ số tốc độ duy nhất, mà học một mô hình hai thành phần cho mỗi cặp (worker, loại op).

**Đặc trưng của một op** (hub tính từ request; với preset thì lấy `ops` từ `hello.presets`, quality và extra ghi đè như `Presets.build`):

```
class   : t2i (generate không ref) · edit (generate có ref) · refine · upscale · outpaint · enhance
mp      = W × H / 1e6          (size "auto" lấy size của preset; edit lấy size ảnh ref đầu)
mode_f  = turbo 1.00 · base 1.05 · base_q8 1.45
steps   = op.steps, hoặc theo quality: low 4 · auto/medium 5 · high 8 · hd 10 (base)
calls   = số lần gọi ComfyUI của op: generate = n, refine/upscale = n_in, outpaint/enhance = 1
x       = khối lượng sampling mỗi call: steps × mp × mode_f (generate, refine) · mp_out (upscale) · 0 (còn lại)
```

**Mô hình** (tham số riêng cho từng worker `w` và class `c`):

```
est_w(op)  = a1[w,c] + (calls − 1) × aN[w,c] + calls × b[w,c] × x
est_w(job) = Σ est_w(op) trên các op GPU + warm_penalty_w(job)
```

- `a1`: chi phí cố định của call đầu (nạp/đổi encoder và DiT, encode prompt, VAE).
- `aN`: chi phí cố định của các call sau trong cùng op (n > 1: ComfyUI giữ được kết quả encode nên thường rẻ hơn `a1`).
- `b`: giây trên một đơn vị sampling (1 step ở 1 MP).
- `warm_penalty_w = 0` nếu `model_key(job) == loaded_model_w`, ngược lại `model_load_s_w` (học từ attempt `cold_start`, khởi đầu 60 s).
- Op CPU (pixelize, remove_bg, compose, fidelity) không vào mô hình GPU; job chỉ có op CPU dùng slot CPU (§3.4).

**Worker đo sẵn phần tách.** Trong `result.timings.ops[]`, mỗi op GPU kèm `calls[]: {seconds, sample_s}`, trong đó `sample_s` là khoảng từ event `progress` đầu tiên tới event cuối của KSampler trong call đó (đo trong `_JobCtx._run`, không tốn thêm gì). Nhờ vậy hub học từng tham số bằng EWMA đơn giản, không cần hồi quy:

```
b  ← EWMA( sample_s / x )                       trên mọi call có x > 0
a1 ← EWMA( seconds − sample_s )                 trên call đầu của op
aN ← EWMA( seconds − sample_s )                 trên các call sau (chỉ khi n > 1)
upscale, enhance, outpaint: a1 ← EWMA(seconds / calls), b ← EWMA(seconds/x) cho upscale
α = 0.2; mẫu nằm ngoài [0.25 × est, 4 × est] chỉ ghi vào thống kê, không cập nhật mô hình
attempt cold_start: phần (seconds − est không gồm warm_penalty) của call đầu đi vào EWMA model_load_s, không vào a1
```

**Giá trị khởi đầu lấy từ chính worker**, không từ bảng tier. `hello.perf_model` mang tham số worker tự tính từ lịch sử local của nó (QIS đã lưu `timings` mọi job trong `jobs.result`; m4 thêm `sample_s`). Worker mới chưa có lịch sử thì suy từ calibration/benchmark local (`PerfS`):

```
a1₀ = overhead_s × calib[op] ,  aN₀ = 0.5 × a1₀ ,  b₀ = s_per_step[mode] / ref_mp × calib[op]
```

Dưới 5 mẫu của class nào thì dashboard gắn nhãn "đang đo" cho class đó.

**"Ảnh chuẩn"** dùng để so worker trên dashboard và để quy đổi: `std_s_w = est_w(t2i, turbo, 1024², n = 1)`. Số đo hiện tại: 3060 ≈ 66 s, 3050 ≈ 74 s.

**Khối lượng tham chiếu của job** (dùng cho fair-share và admission, không phụ thuộc worker nào chạy): `ref_s(job)` = trung vị của `est_w(job)` (không gồm `warm_penalty`) trên các worker có preset đó, tính lúc submit, lưu vào `jobs.ref_s`.

### 3.2 Năng lực từng worker

Ngoài mô hình thời gian ở §3.1, mỗi worker có thêm, lưu trong `workers` và cập nhật sau mỗi attempt:

- **`fail`** (tỉ lệ lỗi do worker), EWMA α = 0.1: thành công → 0; `lost`, `timeout`, `internal`, `engine_unavailable`, `upload_failed` → 1; `oom` → 0.5; `invalid_input`, `cancelled`, `superseded`, `input_fetch_failed` không tính. `reliability = clamp(1 − fail, 0.2, 1)`.
- **Quarantine tự động:** 3 lỗi do worker liên tiếp → không giao việc trong 5 phút, lần sau gấp đôi, tối đa 60 phút; một attempt thành công thì reset. Worker vẫn kết nối và vẫn hiện trên dashboard. Đây không phải "chặn": hub không có thao tác duyệt/chặn worker.

### 3.3 Queue và thứ tự job

Queue bền: mọi job `queued` nằm trong bảng `jobs`; dispatcher giữ index trong RAM (id, key, priority, created_at, đặc trưng op, `ref_s`, model_key, ràng buộc) và nạp lại từ DB khi khởi động. Dispatcher chạy khi có job mới, slot rảnh, worker nối/đổi state, và mỗi 1 s.

Điểm của job (cùng tinh thần DESIGN §5.4, đổi "số job" thành khối lượng tham chiếu `ref_s`):

```
job_score = prio_weight            (interactive 100, batch 0)
          + aging_per_min × phút_đã_chờ          (5)
          − fair_penalty × share(key)            (10)
share(key) = (Σ ref_s các job của key đang assigned/running + Σ ref_s job key đã bắt đầu trong 5 phút gần nhất)
             / median(std_s_w) / key.weight          (đơn vị: "ảnh chuẩn")
```

- Request đồng bộ (OpenAI endpoint) mặc định `interactive`; `/v1/jobs` và `/v1/batches` mặc định `batch`. Key có `priority = batch` thì mọi job của nó hạ về `batch`.
- `key.weight` (mặc định 1) cho phép chia tài nguyên không đều giữa các key.
- Hoà điểm thì job tạo trước đi trước.

### 3.4 Chọn worker (routing)

Với mỗi job theo thứ tự `job_score` giảm dần (quét tối đa 200 job mỗi vòng):

```
E   = worker đủ điều kiện: connected, state=ready (không warming/draining/paused), không quarantine,
      có preset trong hello.presets (job pipeline: có mọi op và mode cần dùng),
      khớp job.model_fingerprint nếu client ghim (§5.1), vram_mb ≥ job.min_vram_mb (sau OOM),
      worker_id ∉ job.excluded_workers
C_w = (backlog_w + est_w(job)) / reliability_w            est_w(job) gồm warm_penalty_w (§3.1)
      backlog_w = Σ max(est_w(attempt) − đã_chạy, 0) trên các attempt đang chạy/prefetch của w
F   = những w ∈ E còn slot trống (gpu rảnh, hoặc gpu bận nhưng prefetch rảnh)
best_any = min C_w trên E   (gồm cả worker đang bận)
w*  = argmin C_w trên F
giao cho w* nếu  C_w* ≤ best_any × (1 + hold_slack)   (hold_slack = 0.25)
           hoặc  job đã chờ ≥ hold_max_s  (interactive 20 s, batch 120 s)
ngược lại giữ job lại trong queue (chờ worker nhanh hơn rảnh)
```

Vì sao worker chậm/không ổn định nhận ít việc hơn:
- `C_w` là thời điểm xong dự kiến trên chính worker đó, theo mô hình học từ số đo thật của nó, chia cho `reliability`: worker chậm chỉ được chọn khi worker nhanh đã có backlog đủ dài; worker lỗi 50% trông chậm gấp đôi.
- Mô hình theo class nên một worker có thể nhanh với t2i nhưng chậm với edit (ref encode trên GPU 6 GB): hub tự chia theo loại việc.
- Luật giữ lại (`hold_slack`) chặn trường hợp worker chậm đang rảnh "giật" một job mà worker nhanh sẽ làm xong sớm hơn dù phải chờ.
- `hold_max_s` chống đói: job không bao giờ bị giữ mãi.

Slot: mỗi worker nhận tối đa `slots.gpu + slots.prefetch` attempt GPU (mặc định 1 + 1). Attempt prefetch được tải input ngay và nằm trong queue local của worker; `backlog_w` khiến hub chỉ dùng slot prefetch khi không có worker rảnh nào tốt hơn. Job chỉ có op CPU dùng `slots.cpu` (mặc định 2), chọn worker theo `cpu_pct` thấp nhất, không ảnh hưởng mô hình GPU.

Worker ở state `warming` (ComfyUI đang khởi động hoặc đang chạy warm-up; trên 3050 mất ~200 s sau mỗi lần start) vẫn kết nối và hiện trên dashboard nhưng **không** thuộc `E` và không tính vào năng lực của cụm ở §3.7.

`accept` không đến trong 10 s → coi như `reject{reason:"busy"}`, cộng 0.5 vào bộ đếm lỗi liên tiếp. `reject` thì thêm worker vào `job.excluded_workers` cho lần thử kế (riêng `busy`/`draining` thì không) và đưa job về đầu queue **không** tăng `attempts`.

### 3.5 Retry và số lần thử

- `job_max_attempts = 3` (đếm attempt đã `accept`). Lần thử kế tiếp tránh worker vừa thất bại nếu còn worker khác đủ điều kiện.
- OOM: đặt `job.min_vram_mb = vram_mb của worker vừa OOM + 1`.
- Hết số lần thử → `failed` với code của lần cuối (`worker_lost` nếu lần cuối là `lost`).
- `input_fetch_failed` lặp 3 lần → `failed storage_unavailable`.

### 3.6 Timeout

`deadline_s = max(300, 4 × est_w(job))` tính tại lúc giao (gồm `warm_penalty_w`). Quá hạn: hub gửi `cancel{reason:"timeout"}`, attempt `failed timeout`, job retry nơi khác. Worker vẫn gửi `failure{code:"cancelled"}` khi dừng xong; message này chỉ để đóng sổ.

### 3.7 Admission (429 / 503)

```
S          = worker connected, state=ready, không quarantine
capacity   = Σ_{w ∈ S} median(std_s) / std_s_w          (số "GPU chuẩn"; worker nhanh tính hơn 1)
est_wait   = (Σ ref_s job queued có điểm ≥ job mới + Σ backlog_w) / capacity
```

| Kiểm tra | Áp cho | Mặc định | Lỗi |
|---|---|---|---|
| Key đã revoke/hết hạn, thiếu scope | tất cả | | 401 / 403 |
| `ref_s(job) > max_job_s` | tất cả | 1800 s | `400 job_too_large` |
| Không worker nào (đã thấy trong 24 h) có preset/op/fingerprint của job | tất cả | | `400 model_not_available` |
| `capacity = 0` (không worker nào ready, kể cả khi chỉ đang warming) | sync | | `503 no_workers`, `Retry-After` = thời gian warm-up còn lại nếu biết, không thì 30 |
| `est_wait + ref_s(job) > sync_max_wait_s` | sync | 600 s | `429 queue_full`, `Retry-After` = phần vượt |
| `rpm` của key | sync + `/v1/jobs` | 120 | `429 rate_limit` |
| Job queued+running của key ≥ `key.max_pending` | tất cả | 50 (key batch nên đặt 5000) | `429 key_limit` |
| Tổng job queued ≥ `queue_max_jobs` | `/v1/jobs`, batch | 20000 | `429 queue_full` |
| `daily_images` của key | tất cả | không giới hạn | `429 key_limit` |
| R2 đang lỗi (health check thất bại) | sync | | `503 storage_unavailable` |

`/v1/jobs` **được nhận khi không có worker nào** (queue bền), job chờ tới `max_queue_age` (interactive 1 h, batch 24 h) rồi `expired`. Thêm header `X-Qis-Queue-Depth`, `X-Qis-Est-Wait` cho mọi response tạo job.

---

## 4. R2

### 4.1 Bucket và key

Một bucket (`QISHUB_R2_BUCKET`, ví dụ `qis`). Key:

| Prefix | Mẫu | Ai ghi |
|---|---|---|
| `in/` | `in/{yyyy}/{mm}/{dd}/{file_id}.{png\|jpg\|webp}` | Hub (stream từ upload của client) |
| `out/` | `out/{yyyy}/{mm}/{dd}/{job_id}/a{attempt_no}/{name}` | Worker, bằng URL PUT presigned |

Ngày = UTC lúc tạo file/job. Có `attempt_no` trong key nên hai attempt chạy đua không bao giờ ghi đè nhau.

### 4.2 Presign và TTL

| URL | Method | TTL | Ghi chú |
|---|---|---|---|
| Input cho worker | GET | 30 phút | Cấp trong `assign` |
| Output cho worker | PUT | 15 phút | Ký kèm `Content-Type`; cấp trong `upload_grant` |
| Kiểm tra của hub | HEAD | 60 s | Hub tự dùng khi commit `result` |
| Trả cho client (`response_format=url`, job view) | GET | `url_ttl_s` (mặc định 3600, tối đa 7 ngày theo giới hạn SigV4, không vượt `expires_at` của file) | Có thể kèm `response-content-disposition` |

Presign tính offline bằng botocore (không gọi mạng). Mọi truy cập thật của hub tới R2 (PUT input, HEAD, GET cho b64, DELETE) đi qua `httpx` với URL presigned hoặc API S3; client và worker không bao giờ thấy credential.

### 4.3 Content type và kích thước

- Input: client gửi PNG/JPEG/WEBP (BMP/GIF/TIFF cũng nhận như QIS), tối đa `max_input_bytes` = 20 MiB và `max_pixels` = 16 MP mỗi ảnh, tối đa 3 ảnh (theo `limits` thấp nhất của các worker). Hub **không decode toàn ảnh**: chỉ `Image.open` đọc header (định dạng, kích thước), spool vào file tạm (RAM ≤ 1 MiB, còn lại ra đĩa), rồi PUT với `Content-Length`. Worker vẫn `normalize_image` như cũ.
- Output: `image/png | image/webp | image/jpeg`, tối đa `max_output_bytes` = 50 MiB mỗi file. `application/json`, `text/plain` cho kết quả không phải ảnh nếu > 256 KiB.

### 4.4 Retention

Kiểm tra thật trên bucket (2026-10-09): với key S3 hiện có, list / put / delete và presigned GET đều chạy, nhưng `GetBucketLifecycleConfiguration` trả **AccessDenied**. Vì vậy:

- `retention_days` (output) mặc định **30**, tối đa 30. `input_retention_days` mặc định **7**. Sửa trên console, có hiệu lực ở lần quét kế tiếp. `retention_hours` của từng job chỉ được **ngắn hơn** mặc định.
- **Lớp chính là sweeper của hub** (§4.5), chỉ dùng các quyền key hiện có (list + delete). Key có ngày ở đầu (`out/{yyyy}/{mm}/{dd}/…`) nên sweeper xoá theo prefix ngày, không phải đọc metadata từng object.
- **Lifecycle rule của bucket là tuỳ chọn, làm tay**: devops tạo trên Cloudflare dashboard (hoặc bằng API token có quyền quản trị bucket), đặt dài hơn retention của hub một ngày để chỉ làm lưới an toàn khi hub ngừng chạy lâu:
  ```json
  {"Rules": [
    {"ID": "qis-out", "Status": "Enabled", "Filter": {"Prefix": "out/"}, "Expiration": {"Days": 31}},
    {"ID": "qis-in",  "Status": "Enabled", "Filter": {"Prefix": "in/"},  "Expiration": {"Days": 8}}]}
  ```
  Trang Settings hiện JSON này để copy. Hub **không** đọc lifecycle lúc khởi động và không coi lỗi AccessDenied là lỗi. Chỉ khi `QISHUB_R2_APPLY_LIFECYCLE=true` (đã có token đủ quyền) hub mới thử `PutBucketLifecycleConfiguration` và ghi kết quả lên Settings.

### 4.5 Sweeper (cơ chế retention chính)

Chạy trong hub, mỗi tác vụ giữ một khoá trong DB (`hub_settings.sweeper_lock`) để không chạy chồng:
- **Mỗi 10 phút (theo DB):** `files` có `expires_at < now AND deleted_at IS NULL` → `DeleteObjects` theo lô ≤ 1000 key → đặt `deleted_at`. Áp cả `retention_hours` ngắn của từng job. Output của attempt `superseded` được xoá ngay khi commit, sweeper chỉ dọn phần sót.
- **Mỗi giờ (theo prefix ngày):** với mỗi ngày `d` cũ hơn `retention_days` (với `out/`) hoặc `input_retention_days` (với `in/`), `ListObjectsV2` prefix `out/{d}/` (`in/{d}/`), xoá theo lô cho tới khi rỗng; ghi ngày đã dọn xong vào `hub_settings.sweeper_done_until` để lần sau bắt đầu từ đó. Cách này bắt cả object không có dòng DB (ví dụ upload của attempt `lost` mà hub chưa từng thấy `result`) và vẫn đúng khi DB bị mất.
- Lần chạy đầu (hoặc khi `sweeper_done_until` trống): list prefix cấp ngày từ ngày cũ nhất có trong bucket.
- Lỗi R2 khi quét: ghi log, thử lại ở lần kế; sau 24 h liên tục lỗi thì Overview hiện cảnh báo "retention không chạy được".
- Mỗi lần quét ghi số object/bytes đã xoá vào `stats_minute` để dashboard hiện dung lượng đã dọn.
- Job có file đã hết hạn: URL trong job view bỏ đi, `GET /v1/files/{id}/content` trả `410 file_expired`.

---

## 5. API public của hub

Base `https://qis.runnow.cc/v1`. Auth `Authorization: Bearer sk-qis-…` (**cùng định dạng key với QIS**, client chỉ cần đổi `base_url`). Lỗi theo định dạng OpenAI như `docs/API.md` §1, thêm các code: `no_workers` (503), `storage_unavailable` (503), `model_not_available` (400), `worker_lost` (500), `file_expired` (410). Header: `X-Request-Id`, `X-Qis-Job-Id`, `X-Qis-Worker` (khi job xong), `Retry-After`, `X-Qis-Queue-Depth`.

### 5.1 OpenAI

| Endpoint | Ghi chú |
|---|---|
| `POST /v1/images/generations` | JSON. Model `ImageGenRequest` giống hệt QIS (`extra="allow"`). Thành job `kind=images.generations`, `payload.request` = toàn bộ trường + extra (bỏ `priority`, `webhook_url`, `retention_hours`, hub tự dùng) |
| `POST /v1/images/edits` | Multipart `image`/`image[]` (1–3), các field khác như QIS (`_coerce` cho giá trị JSON). Hub stream từng ảnh lên `in/`, tạo `files` purpose `input`, `payload.refs` = các `file_id`. `mask` → 400 như QIS |
| `GET /v1/models`, `GET /v1/models/{id}` | Hợp các preset trong `hello.presets` của worker đã kết nối trong 24 h; lọc theo `allowed_models` của key. Thêm `qis.workers` (số worker có preset), `qis.fingerprints[]` (các fingerprint đang có cho preset đó, kèm số worker mỗi loại), `qis.est_s` (= `ref_s` của request mặc định) |

Trường được hỗ trợ: `model, prompt, n (≤ min max_n), size, quality, background, output_format, output_compression, response_format, stream, partial_images, user` và mọi extra của API.md. `model` thiếu thì dùng `default_preset` của hub (setting, mặc định `qwen-image-2.1-turbo`).

Response giống QIS: `{created, data[], background, output_format, size, quality, usage, qis{job_id, model, seeds, timings, degraded, worker{id, name}, model_fingerprint}}`.

**Tái lập kết quả:** cùng seed trên hai worker khác fingerprint (ví dụ turbo Q8 trên 3060 và turbo Q4 tự merge trên 3050) cho ra ảnh **khác nhau**. Mỗi job luôn ghi và trả `worker` + `model_fingerprint`. Client cần tái lập thì gửi `extra_body.model_fingerprint` (lấy từ response trước hoặc `/v1/models`): hub chỉ giao cho worker có đúng fingerprint đó; không còn worker nào như vậy thì `400 model_not_available`. `data[i].revised_prompt` từ `meta.revised_prompt`. `qis.raw` và `qis.extra` **luôn là URL presigned**, kể cả khi `response_format=b64_json` (giới hạn bộ nhớ, xem Q5); gửi `qis_extra_b64: true` để nhận base64 cho cả hai.

### 5.2 Đồng bộ và giới hạn 100 s của Cloudflare

Port nguyên chiến lược của `qis/api/present.py`:
1. Chờ tối đa 60 s (`FIRST_WAIT_S`, gồm cả thời gian chờ trong queue). Xong trong khoảng đó thì trả response bình thường với status thật.
2. Chưa xong: gửi `200 application/json` ngay, mỗi 15 s gửi một khoảng trắng, JSON kết quả tới cuối. Thất bại ở giai đoạn này thì body là `{"error": …}` với status 200 (đánh đổi đã chấp nhận, R2 ở DESIGN §19).
3. `stream: true`: SSE theo OpenAI (`image_generation.partial_image`, `image_generation.completed`), `: ping` mỗi 15 s. Preview đến từ `progress.preview_jpeg_b64` của worker (worker chỉ gửi khi `output.partial_images > 0`, tối đa đúng số đó).
4. Client ngắt kết nối khi job còn `queued` → huỷ job. Khi job đã được giao → để chạy xong; kết quả lấy qua `GET /v1/jobs/{X-Qis-Job-Id}`.

### 5.3 Preset và `model`

- Nguồn chính của preset là `presets.yaml` trên worker (đi theo release QIS). Hub không dựng `JobSpec` từ preset; hub chỉ cần định nghĩa preset để tính đặc trưng op (§3.1) và để lọc worker.
- Các worker khác version có định nghĩa khác nhau cho cùng một preset: hub dùng định nghĩa của worker có version cao nhất cho `/v1/models` và cho đặc trưng op; job chỉ giao cho worker có preset đó (bất kể version).
- Muốn sửa preset toàn cụm: phát hành QIS mới, hoặc sửa `presets.yaml` trên từng worker (dashboard local). Đồng bộ preset từ hub là việc sau (Q3).

### 5.4 Jobs API (batch, bền)

```
POST   /v1/jobs                  → 202 {id, status, position, est_wait_s, links}
GET    /v1/jobs/{id}             → Job
GET    /v1/jobs/{id}/events      → SSE (Last-Event-ID)
DELETE /v1/jobs/{id}             → huỷ
POST   /v1/jobs/{id}/retry       → job mới cùng request
GET    /v1/jobs?status=&batch_id=&limit=&after=
POST   /v1/files                 → upload input (multipart) → {id, width, height, bytes, expires_at}
GET    /v1/files/{id}  ·  GET /v1/files/{id}/content (302 tới URL presigned)  ·  DELETE /v1/files/{id}
POST   /v1/batches               → {jobs:[JobCreate…≤500], webhook_url?, priority?} → 202 {id, job_ids[]}
GET    /v1/batches/{id}          → {id, counts{queued, running, succeeded, failed, canceled}, created_at, finished_at}
GET    /v1/batches/{id}/events   → SSE gộp event của mọi job trong batch
DELETE /v1/batches/{id}          → huỷ mọi job chưa xong
```

`JobCreate` (một trong ba dạng):

```jsonc
{"type": "images.generations", "request": {"model": "pixelart-16bit-64", "prompt": "a knight", "n": 2, "seed": 7}}
{"type": "images.edits", "request": {"model": "qwen-image-2.1-turbo", "prompt": "red cape"}, "images": ["file_01JA…"]}
{"type": "pipeline", "spec": { /* JobSpec DESIGN §6.3, file id là id của hub */ }}
// chung: "priority", "webhook_url", "retention_hours", "metadata" (≤ 4 KiB, trả lại nguyên vẹn)
```

Hub kiểm tra cấu trúc (`type`, model/op có trên worker nào đó, file thuộc key, kích thước); lỗi sâu hơn của op do worker trả về thành `failed invalid_input`. Header `Idempotency-Key` (24 h) như QIS.

`Job`:

```json
{"object": "job", "id": "job_01JA…", "status": "running", "type": "images.generations", "model": "qwen-image-2.1-turbo",
 "priority": "batch", "batch_id": null, "attempts": 1, "worker": {"id": "node_01J9…", "name": "PIXON-3060-01"},
 "progress": {"phase": "running", "op": "generate", "step": 3, "steps": 5, "pct": 0.6},
 "created_at": 1760000000.0, "started_at": 1760000004.2, "finished_at": null, "est_wait_s": 0,
 "result": {"outputs": [{"step": 0, "op": "generate", "images": [{"file_id": "file_…", "url": "https://…", "width": 1024,
            "height": 1024, "bytes": 1534001, "content_type": "image/png"}], "extra": {}, "text": null, "data": {}}],
            "meta": {"revised_prompt": "…", "seeds": [7]}, "timings": {…}},
 "error": null, "metadata": {}}
```

`status` client thấy: `queued | assigned | running | succeeded | failed | canceled | expired` (`assigned` = worker đã nhận, đang tải input hoặc chờ trong queue local; `uploading` gộp vào `running` với `progress.phase = "uploading"`).

Sự kiện SSE: `queued{position, est_wait_s}`, `assigned{worker, attempt_no}`, `progress{…}`, `requeued{reason, attempt_no}`, `succeeded{job}`, `failed{error}`, `canceled`, `expired`, `: ping` mỗi 15 s. Webhook giống API.md §6 (`X-Qis-Signature` HMAC bằng `QISHUB_KEY_PEPPER`-derived secret), batch gửi thêm một webhook `batch.completed` khi job cuối kết thúc.

### 5.5 Định dạng lỗi

```json
{"error": {"message": "no worker is connected", "type": "server_error", "param": null, "code": "no_workers", "job_id": null}}
```

| HTTP | code mới của hub |
|---|---|
| 400 | `model_not_available`, `job_too_large`, `invalid_input` (từ worker, khi còn trả được status) |
| 409 | `canceled` |
| 410 | `file_expired` |
| 429 | `queue_full`, `key_limit`, `rate_limit` |
| 500 | `worker_lost`, `oom`, `upload_failed`, `internal` |
| 503 | `no_workers`, `storage_unavailable` |
| 504 | `timeout`, `expired` |

### 5.6 Đường `b64_json` giới hạn bộ nhớ

Mục tiêu: hub không bao giờ giữ nguyên một ảnh trong RAM; tổng RAM cho b64 bị chặn bởi một hằng số.

1. Job `succeeded` (handler đồng bộ đang chờ, có thể đang gửi keepalive).
2. Tính tổng `bytes` các ảnh `data[]`. Vượt `b64_max_bytes` (mặc định 48 MiB ảnh gốc) → trả lỗi `400 response_too_large` kèm gợi ý dùng `response_format=url` (job vẫn thành công, ảnh lấy qua jobs API).
3. Chờ `b64_sem` (`asyncio.Semaphore(b64_concurrency)`, mặc định **4**). Trong lúc chờ vẫn gửi khoảng trắng mỗi 15 s nếu đã ở chế độ keepalive.
4. Nếu chưa gửi header (đường nhanh < 60 s): mở GET R2 cho ảnh đầu tiên **trước**; lỗi thì trả `503 storage_unavailable` với status thật.
5. Trả `StreamingResponse` sinh JSON theo từng mảnh: `{"created":…,"data":[` → với mỗi ảnh: `{"b64_json":"` → đọc body R2 theo khối **3 × 64 KiB** (bội số của 3 nên các khối base64 nối thẳng được, không có padding giữa chừng) → `base64.b64encode(khối)` → `yield` → `","revised_prompt":…}` → cuối cùng `],"usage":…,"qis":{…}}`.
6. Ảnh kế tiếp chỉ mở sau khi ảnh trước đã đóng. Bộ nhớ mỗi request ≈ 1 khối đọc + 1 khối mã hoá + buffer httpx (< 1 MiB); tổng hub ≤ `b64_concurrency` × ~1 MiB.
7. `finally`: đóng response R2, nhả semaphore. Client ngắt kết nối thì generator bị huỷ và cũng đi qua `finally`.
8. R2 lỗi giữa chừng: không thể sửa status nữa, hub cắt kết nối (client nhận JSON cụt và báo lỗi), log `b64_stream_aborted`.

`response_format=url` không đụng tới đường này: hub chỉ presign GET và trả về, không tải gì.

---

## 6. Schema DB của hub

SQLite WAL, một process ghi. ID là ULID có prefix (dùng chung `new_id` như QIS). Thời gian là `REAL` epoch giây. JSON lưu dạng `TEXT`. Các bảng RBAC của template (`employees`, `roles`, `positions`, `departments`, `mcp_tokens`, `audit_log`) giữ nguyên.

```
api_keys(id PK 'key_…', name, prefix, hash, employee_id FK, scopes JSON, priority[interactive|batch], weight REAL DEFAULT 1,
         max_pending, rpm, daily_images NULL, allowed_models JSON NULL, created_at, last_used_at, expires_at NULL, revoked_at NULL)

workers(id PK 'node_…', name, hostname, version, proto, gpu_name, vram_mb, ram_mb, tier, caps JSON (hello rút gọn: models, ops,
        presets, model_files, limits, slots), state, warm_eta_s NULL, connected INT, session_id, last_ip,
        first_seen_at, last_seen_at, last_heartbeat JSON, loaded_model,
        perf JSON ({class: {a1, aN, b, samples, source}}), std_s REAL, model_load_s REAL, fail_ewma REAL, consecutive_fail INT,
        quarantine_until REAL NULL, quarantine_level INT, admin_paused INT, note)

worker_sessions(id PK 'ses_…', worker_id FK, connected_at, disconnected_at NULL, close_code NULL, close_reason, ip, version)

jobs(id PK 'job_…', api_key_id FK, employee_id NULL, batch_id NULL, type, request JSON, preset, features JSON, ref_s REAL,
     priority, model_fingerprint NULL (ghim của client),
     status[queued|assigned|running|succeeded|failed|canceled|expired], sync INT, response_format,
     attempts INT, current_attempt_id NULL, winning_attempt_id NULL, excluded_workers JSON, min_vram_mb NULL,
     progress JSON, result JSON, error JSON, metadata JSON,
     created_at, first_assigned_at, started_at, finished_at, idempotency_key NULL, webhook_url NULL, retention_hours NULL,
     UNIQUE(api_key_id, idempotency_key))
  INDEX jobs_queue(status, priority, created_at) · jobs_key(api_key_id, created_at) · jobs_batch(batch_id)

job_attempts(id PK 'att_…', job_id FK, attempt_no, worker_id FK, session_id,
             status[offered|accepted|running|uploading|succeeded|failed|lost|rejected|canceled|superseded],
             est_s, model_fingerprint, offered_at, accepted_at, finished_at, lease_expires_at, deadline_at,
             error_code NULL, error JSON NULL, reject_reason NULL, cold_start INT,
             fetch_s, local_queue_s, run_s, upload_s, timings JSON)
  INDEX att_job(job_id) · att_worker(worker_id, finished_at)

job_events(job_id, seq, ts, type, data JSON, PRIMARY KEY(job_id, seq))   -- giữ 200 event cuối mỗi job, xoá sau 7 ngày

files(id PK 'file_…', r2_key, purpose[input|output], content_type, bytes, sha256 NULL, width, height, has_alpha,
      owner_key_id, job_id NULL, attempt_id NULL, created_at, expires_at, deleted_at NULL)
  INDEX files_exp(expires_at) WHERE deleted_at IS NULL

batches(id PK 'bat_…', api_key_id, count, priority, webhook_url, created_at, finished_at NULL, metadata JSON)

usage_daily(api_key_id, day TEXT, jobs, images, ref_s, failed, PRIMARY KEY(api_key_id, day))
stats_minute(ts PK, submitted, succeeded, failed, images, ref_s, queue_depth, workers_connected, workers_busy,
             r2_deleted_objects, r2_deleted_bytes)
worker_stats_minute(worker_id, ts, attempts_ok, attempts_fail, busy_s, ref_s, gpu_util_avg, vram_used_max_mb, temp_max_c,
                    PRIMARY KEY(worker_id, ts))        -- giữ 30 ngày
hub_settings(key PK, value JSON, updated_by, updated_at)
```

Ghi chú:
- Job đã kết thúc giữ 90 ngày (setting), sau đó xoá cả `job_attempts`/`job_events`; `usage_daily` giữ lâu dài.
- Heartbeat không ghi DB mỗi lần: registry giữ trong RAM, ghi `workers.last_heartbeat` mỗi 60 s và gộp vào `worker_stats_minute` mỗi phút.
- **Chuyển Postgres sau này:** chỉ dùng SQL chuẩn (`INSERT … ON CONFLICT`, `UPDATE … RETURNING` đều có ở SQLite ≥ 3.35 và Postgres), không dùng hàm JSON1 trong truy vấn, ID là `TEXT`, truy cập DB qua một module repo duy nhất. Khi chuyển: `pgloader` dữ liệu, đổi driver, đổi `REAL` epoch sang `double precision` (giữ nguyên ngữ nghĩa). Chạy **nhiều instance hub** thì cần thêm: leader election cho dispatcher (advisory lock), sticky session hoặc pub/sub cho WS worker và SSE. Đó là việc riêng, không nằm trong v1.

---

## 7. Dashboard (console pixon-webapp)

Quyền mới trong catalog RBAC (`app/rbac/permissions.py`):

```
qis:workers:read · qis:workers:manage            (pause/resume, quên worker offline)
qis:jobs:read:own · qis:jobs:read:all · qis:jobs:cancel:own · qis:jobs:cancel:all
qis:keys:manage:own · qis:keys:manage:all
qis:settings:manage
```

`own` = job/key của key do chính nhân viên đó sở hữu. Role mặc định "employee" có `qis:jobs:read:own`, `qis:keys:manage:own`.

| View | Nội dung |
|---|---|
| **Overview** | KPI: worker kết nối / tổng, worker đang bận, job queued (interactive / batch), running, throughput (ảnh/phút, 15 phút), tỉ lệ thành công 1 h, latency p50/p95 1 h, ước tính chờ hiện tại, trạng thái R2. Biểu đồ 24 h: ảnh/phút, queue depth, số worker. |
| **Workers** | Bảng (`dataTable()`): tên, state (ready / busy / warming + thời gian còn lại / draining / paused / quarantine / offline), version, GPU + VRAM, util %, VRAM dùng, nhiệt độ, job hiện tại + %, "s/ảnh chuẩn" (`std_s`), avg và p95 thời gian xử lý 24 h, tỉ lệ thành công 24 h, heartbeat cách đây bao lâu, uptime phiên. Menu ⋮: Pause, Resume, Quên worker (chỉ khi offline). Chi tiết: preset có/không có, `model_files` + fingerprint, tham số `a1/aN/b` từng class (nhãn "đang đo" nếu < 5 mẫu), lịch sử attempt, timeline phiên kết nối, biểu đồ `std_s` và nhiệt độ. |
| **Requests** | Mọi job chưa kết thúc + 500 job gần nhất; lọc theo trạng thái, key, worker, preset, batch. Cột: id, key, model, status, worker, progress, chờ, chạy, tạo lúc. Chi tiết: request (prompt đầy đủ), các attempt, event, link output (presigned), nút Huỷ / Retry. |
| **API Keys** | Tạo (hiện một lần), scope, priority, weight, quota, hạn dùng, thu hồi, usage 24 h / 30 ngày. |
| **Settings → Hub** | Trọng số scheduler, `hold_slack`, ngưỡng admission, `retention_days`, `url_ttl_s`, `b64_concurrency`; JSON lifecycle; trạng thái join token (chỉ hiện "đã cấu hình, N token", không hiện giá trị). |

Định nghĩa số liệu:

| Số | Công thức |
|---|---|
| Thời gian xử lý của attempt | `run_s` do worker báo (chạy GPU/CPU local, không gồm tải input, chờ local, upload) |
| avg / p95 của worker | Trên các attempt `succeeded` của worker trong 24 h, theo `run_s`, tách theo class (t2i, edit…); hiện thêm tỉ lệ `run_s / est_s` để thấy mô hình còn lệch bao nhiêu |
| Tỉ lệ thành công worker | `succeeded / (succeeded + failed do worker + lost)` trong 24 h; không tính `invalid_input`, `cancelled`, `superseded`, `rejected` |
| Latency end-to-end | `finished_at − created_at` của job `succeeded` |
| Thời gian chờ | `first_assigned_at − created_at` |
| Throughput | `Σ images` của `stats_minute` theo phút; song song là `Σ ref_s / median(std_s)` (ảnh chuẩn/phút) |
| Fleet utilisation | `Σ busy_s / Σ thời gian kết nối` của worker trong cửa sổ |
| "Ảnh chuẩn" của worker (`std_s`) | `est_w` của t2i turbo 1024², n = 1, theo mô hình §3.1 |

Console lấy dữ liệu bằng polling 2 s (`GET /api/hub/overview`, `/api/hub/workers`, `/api/hub/requests?active=1`) khi tab đang mở. Các endpoint đọc từ registry/dispatcher trong RAM cộng truy vấn có index, không quét bảng lớn.

---

## 8. Phía worker

### 8.1 CLI

| Lệnh | Việc |
|---|---|
| `qis worker join <hub_url> --token <t> [--name N]` | Ghi `[worker]` vào `config.toml`, token vào `config/worker.token` (quyền như `secret.key`), `enabled = true`, restart service. Chấp nhận `https://qis.runnow.cc` và tự đổi thành `wss://…/worker/ws` |
| `qis worker leave` | Drain, gửi `bye{reason:"leave"}`, `enabled = false`, restart service |
| `qis worker status [--json]` | Đọc `GET /admin/api/status` local (key của tray) và in khối `worker` |
| `qis worker drain [--wait]` / `qis worker resume` | `POST /admin/api/worker/drain` / `…/resume` trên gateway local |
| `qis worker run [--hub URL --token T]` | Chạy gateway ở foreground với worker mode bật (dev, test với fake hub) |

Installer: thêm tham số `-HubUrl` / `-JoinToken` (env `QIS_HUB_URL`, `QIS_JOIN_TOKEN`); có thì chạy `qis worker join` ở bước cuối và bỏ qua bước cloudflared.

### 8.2 Cấu hình

```toml
[worker]
enabled = false
hub_url = "wss://qis.runnow.cc/worker/ws"
token_file = "worker.token"      # trong config/
name = ""                         # mặc định hostname
prefetch = 1                      # slot GPU chờ sẵn
cpu_slots = 2
heartbeat_s = 10                  # hub có thể ghi đè qua welcome
accept_local_jobs = false         # true: vẫn nhận job qua /v1 local (dev)
```

`slots.gpu` luôn là 1 (DESIGN D3). Đổi `[worker]` cần restart service.

### 8.3 Trạng thái kết nối và tray

`HubLink` giữ một máy trạng thái, xuất ra `qis/mesh/state.py`:

| `link_state` | Điều kiện |
|---|---|
| `disabled` | `worker.enabled = false` |
| `connecting` | Đang dial hoặc đang chờ backoff (kèm `retry_in_s`) |
| `connected` | Đã nhận `welcome`, `heartbeat_ack` gần nhất < 30 s |
| `disconnected` | Không có session quá 60 s liên tục (vẫn đang thử lại) |
| `auth_failed` | Close `4401` |
| `outdated` | Close `4426` |
| `draining` | Đã yêu cầu drain (vẫn kết nối) |
| `paused` | Hub gửi `pause` |

Tray lấy trạng thái từ chính các endpoint nó đang poll, không cần file riêng:
- `/healthz` (không cần key) thêm `"worker": {"enabled": true, "link": "connected", "hub": "qis.runnow.cc"}`, để tray chưa có admin key vẫn thấy kết nối.
- `/admin/api/status` thêm khối `worker`: `{link, hub_url, worker_id, session_id, connected_since, last_ack_age_s, retry_in_s, last_error, running: [{attempt_id, job_id, phase, pct}], session_done, session_failed}`.

Hiển thị (khi `worker.enabled`):

| Tình huống | Màu icon | Headline / tooltip |
|---|---|---|
| Engine down hoặc service dừng | đỏ | như hiện tại ("Down") |
| Engine đang khởi động | vàng | "Starting / warming up" |
| `connected`, rảnh | xanh lá | "Worker: connected to qis.runnow.cc" |
| `connected`, đang chạy | xanh lá | "Worker: connected · 1 running (62%)" |
| `connecting` | vàng | "Connecting to hub… retry in 12 s" |
| `disconnected` | cam `#ea580c` | "Hub unreachable: <last_error>" |
| `auth_failed` | đỏ | "Join token rejected: run qis worker join" |
| `outdated` | đỏ | "Hub needs a newer QIS: run qis update" |
| `draining` | xanh dương `#2563eb` | "Draining: 1 job left" |
| `paused` | xanh dương | "Paused by hub" |

Cửa sổ trạng thái thêm khung **Hub** (State, Hub, Worker ID, Connected for, Last heartbeat, Jobs this session, Last error). Menu tray thêm "Drain (finish current jobs)" / "Resume" và "Open hub dashboard" (`QISHUB_PUBLIC_URL` lấy từ `hub_url`). Khi worker mode bật, mục "Queue" hiện job của hub đang chạy trên máy này. Logic chọn màu nằm trong `view_model()` (thuần, test được không cần màn hình) như hiện tại.

---

## 9. Ma trận lỗi

| Tình huống | Hub | Worker | Client thấy |
|---|---|---|---|
| Worker chết giữa job (mất điện, kill) | Lease hết sau ≤ 45 s → attempt `lost`, `fail` của worker tăng, job requeue nơi khác | Khởi động lại: huỷ job `kind=hub` cũ, `hello.running=[]` | Job chạy lâu hơn; sync vẫn giữ kết nối nhờ keepalive |
| Mạng rớt ngắn (< 45 s) | Giữ lease, worker nối lại và `resume=keep` | Job local chạy tiếp, gửi lại kết quả từ outbox | Không thấy gì |
| Worker stop service có chủ đích | `bye.abandon` → requeue ngay | Drain tối đa thời gian stop của WinSW rồi bỏ | Job chuyển worker |
| Hub restart | Nạp job từ DB; attempt chưa kết thúc chờ `reconcile_grace_s` = 60 s để worker `hello` nhận lại, quá hạn thì `lost` + requeue | Close 1012/TCP reset → nối lại sau 1–3 s, `hello.running` + `unacked` | Request sync đang mở bị đứt (job vẫn chạy, lấy bằng `X-Qis-Job-Id`); jobs API và SSE nối lại được (`Last-Event-ID`) |
| R2 down: upload input | `503 storage_unavailable` | | Lỗi ngay, thử lại sau |
| R2 down: worker tải input | | `failure input_fetch_failed` | Hub retry sau 10 s; 3 lần → `failed storage_unavailable` |
| R2 down: worker upload output | Giữ attempt (phase `uploading`, heartbeat vẫn gia hạn); health check R2 lỗi thì dừng giao việc mới, sync trả 503 | PUT thử lại 3 lần, rồi xin grant lại mỗi 60 s, tối đa 30 phút; quá thì `failure upload_failed` | Job chậm; nếu quá 30 phút thì retry |
| PUT báo xong nhưng object thiếu/sai size | `result_ack{accepted:false, missing_object}` → như `upload_failed` | Xoá local, chờ lệnh khác | Retry |
| Kết quả trùng (gửi lại sau reconnect) | Idempotent theo `attempt_id`: `accepted:true`, không ghi lại | | |
| Hai attempt cùng xong | Attempt commit trước thắng; attempt sau `superseded`, object bị xoá | Nhận `result_ack{accepted:false, stale}` | Một kết quả duy nhất |
| Kết quả muộn sau khi job đã xong / huỷ | `upload_denied` hoặc `result_ack` stale | Huỷ, xoá local | |
| Client ngắt kết nối khi sync | Job `queued` → huỷ; đã giao → chạy tiếp | `cancel` nếu bị huỷ | Kết quả vẫn có ở jobs API |
| Client huỷ (`DELETE`) | `cancel{reason:client}`, job `canceled` ngay | `node.jobs.cancel` → `/interrupt` ComfyUI, `failure cancelled` | 409 `canceled` cho request đang chờ |
| Worker treo (heartbeat có nhưng không tiến) | Hết `deadline_s` → `cancel timeout` → retry nơi khác, `fail` tăng | | Job chậm |
| Worker OOM | Retry trên worker VRAM lớn hơn; không có → `failed oom` | Đã thử `degraded` local trước khi báo | 500 `oom` nếu hết đường |
| Job lỗi tham số (worker phát hiện) | `failed invalid_input`, không retry | | 400 (hoặc 200 + error nếu đã keepalive) |
| Không có worker nào | Sync: `503 no_workers`; jobs API: nhận, chờ tới `max_queue_age` | | |
| Join token sai / bị xoay | Close 4401 | `auth_failed`, tray đỏ | |
| Hai process cùng `worker_id` (copy data dir) | Session mới thay session cũ (4409), lặp lại → log cảnh báo trên dashboard | Tray báo trùng `worker_id` | |

---

## 10. Chia việc

Ký hiệu: **m3** hub core (repo `qis-hub`), **m4** worker mode (repo `qwen-image-service`), **m5** dashboard hub, **m6** E2E. m3 và m4 chạy song song từ ngày đầu nhờ fixture dùng chung; m5 bắt đầu khi m3 xong API `/api/hub/*`; m6 sau cùng.

### 10.1 Fixture dùng chung (làm đầu tiên, nửa ngày)

1. `qis/mesh/protocol.py` (bản gốc, trong repo QIS): `PROTO`, model Pydantic cho mọi message ở §2.4, enum `FailureCode`, `RejectReason`, `CloseCode`, regex tên file output. Hub copy nguyên file sang `app/hub/protocol.py`, dòng đầu ghi commit nguồn.
2. `tests/fixtures/mesh/*.json`: mỗi loại message một ví dụ hợp lệ (lấy từ §2) cộng vài ví dụ sai. Có ở **cả hai** repo (copy). Test ở cả hai bên: mọi fixture hợp lệ parse được và round-trip giữ nguyên, fixture sai bị từ chối; `sha256(protocol.py)` của hub phải bằng hằng số `PROTOCOL_SHA` ghi trong fixture.
3. **FakeWorker** (repo hub, `tests/fake_worker.py`): client WS asyncio nói đúng protocol, cấu hình được: tham số thời gian thật theo class (`a1, aN, b`, kèm nhiễu) và `perf_model` khai báo trong `hello` (có thể cố tình sai để test việc học), `presets` + fingerprint, `warm_s`, `vram_mb`, kịch bản lỗi (`fail_codes=[…]`), `disconnect_after_s`, `reconnect_after_s`, `slow_upload`, `send_duplicate_result`, `late_result_after_s`, `no_heartbeat`. Upload thật bằng HTTP PUT vào URL presigned (MinIO trong docker, hoặc `moto` server trong process).
4. **FakeHub** (repo QIS, `tests/fake_hub.py`): FastAPI có `/worker/ws` và `GET/PUT /blob/{key}` (URL "presigned" chỉ là URL tới chính fake hub), API kịch bản: `await hub.assign(...)`, `await hub.expect("result", timeout)`, `hub.drop_connection()`, `hub.close(code)`, `hub.blob(key)`. Dùng cùng fake ComfyUI hiện có của QIS, không cần GPU.

### 10.2 m3: hub core

| # | Việc | Nghiệm thu |
|---|---|---|
| 3.1 | Scaffold pixon-webapp (`qis-hub`, `QISHUB_`, port 8101), Docker, `verify_app.py` xanh, thay domain `item` | `docker compose up` trên Docker Desktop; `/health` 200; `check_domain.py` không còn `item` |
| 3.2 | Schema §6 + migration, repo module | Test tạo DB mới và nâng từ bản trước |
| 3.3 | `BlobStore` R2 (presign PUT/GET/HEAD, list theo prefix, delete lô) | Test với MinIO: PUT presigned từ client khác, HEAD đúng size, URL hết hạn bị từ chối; hub khởi động bình thường với key không có quyền lifecycle |
| 3.4 | Gateway WS + registry: token, `hello`/`welcome`, heartbeat, close code, thay session | FakeWorker token sai → 4401; không `hello` 10 s → 4400; hai session cùng id → session cũ 4409 |
| 3.5 | Mô hình thời gian §3.1 + dispatcher: score, routing §3.4, slot, lease, `accept` timeout, reject, retry, OOM, quarantine, timeout, `warming` | (a) 2 FakeWorker có `std_s` thật 66 s và 130 s, 40 job batch → worker nhanh làm ≥ 60%; (a2) FakeWorker khai `perf_model` sai 2× → sau 10 job sai số `run_s/est_s` trung vị trong ±15%; (a3) FakeWorker có `a1` = 50, `b` = 2.5: mô hình học được `a1`, `b` trong ±20% từ `calls[].sample_s`; (a4) job preset `base_q8` không bao giờ giao cho worker không có preset đó; job ghim fingerprint chỉ tới worker khớp; (a5) worker `warming` không nhận job, sync khi chỉ có worker warming → 503 `no_workers`; (b) worker `fail` 50% nhận ít hơn worker khoẻ cùng tốc độ; (c) kill FakeWorker giữa job → job xong ở worker khác sau ≤ 50 s; (d) ngắt 20 s rồi nối lại → `resume keep`, không chạy lại; (e) 3 lỗi liên tiếp → quarantine 5 phút |
| 3.6 | `JobService`: submit, admission §3.7, idempotency, cancel, event bus, SSE, webhook, batches | Không worker: sync → 503 `no_workers`, `/v1/jobs` → 202; vượt `sync_max_wait_s` → 429 có `Retry-After`; `Idempotency-Key` trả lại job cũ |
| 3.7 | Commit kết quả idempotent §2.6 | `result` gửi 2 lần → 1 commit; 2 attempt cùng xong → 1 thắng, object của bên thua bị xoá; result muộn sau `canceled` → stale |
| 3.8 | API OpenAI + `/v1/models` + files + jobs + batches; keepalive; b64 streaming | OpenAI SDK `images.generate` và `images.edit` qua hub + FakeWorker trả đúng dạng; job 120 s qua keepalive không bị cắt; 4 request b64 song song n=8 ảnh 1.5 MB: RSS hub tăng < 50 MB (đo bằng test) |
| 3.9 | API keys (bảng, hash, scope, quota) + CLI tạo key trong container | Key hết hạn → 401; thiếu scope → 403 |
| 3.10 | Sweeper (retention chính) | File có `expires_at` quá hạn bị xoá khỏi MinIO và có `deleted_at`; object mồ côi trong prefix ngày cũ bị xoá ở lần quét theo giờ; xoá DB rồi chạy lại: quét theo prefix vẫn dọn đúng; `sweeper_done_until` tiến đúng |
| 3.11 | Khôi phục khi hub restart | Restart hub khi FakeWorker đang chạy job → worker nối lại, job hoàn tất không chạy lại; job queued còn nguyên |

### 10.3 m4: worker mode (QIS)

| # | Việc | Nghiệm thu |
|---|---|---|
| 4.1 | `[worker]` trong config, đọc token file, chặn route tạo job (`409 worker_mode`) | Test cấu hình; `POST /v1/images/generations` local → 409 khi bật |
| 4.2 | `JobService.submit(internal=True)`, `kind="hub"`, idempotency theo `attempt_id`, huỷ job hub khi khởi động | `assign` trùng → cùng job local; restart node → job hub cũ `canceled` |
| 4.3 | `HubLink`: kết nối, `hello` đủ trường từ `Presets.available`/`ModelSet`; `model_files` + băm nền file ngoài manifest (cache `model_hashes.json`) + `fingerprint` từng preset; `perf_model` tính từ lịch sử `jobs` local (fallback `PerfS`); state `warming` + `warm_eta_s` tới khi `/readyz` warm; heartbeat từ `_gpu_sampler`, backoff + close code, outbox | Với FakeHub: `hello` khớp schema; máy có file turbo tự merge cho fingerprint khác máy dùng file manifest; preset cần mode chưa cấu hình không có trong `presets`; trong lúc warm-up state là `warming`; FakeHub đóng 1012 → nối lại ≤ 3 s; 4401 → `auth_failed`; không nhận gì 30 s → tự reconnect |
| 4.4 | `AttemptRunner`: tải input, thay `file_id`, dựng spec qua `_build()`/`JobSpec`, relay progress (≤ 1/s), convert output format, `upload_request`, PUT, `result` (kèm `timings.ops[].calls[].sample_s` đo trong `_JobCtx._run`, `meta.model_fingerprint`); map lỗi → `failure.code` §2.6 | `sample_s` của mỗi call bằng khoảng giữa progress đầu và cuối (test với fake ComfyUI phát progress có độ trễ cố định); FakeHub + fake ComfyUI: t2i, edit 2 ref, pipeline pixelize, `output_format=webp` → object đúng content type; ComfyUI trả OOM hai lần → `failure oom`; ảnh ref hỏng → `invalid_input` |
| 4.5 | `cancel`, `pause`/`resume`, drain, `bye.abandon` | `cancel` giữa job → ComfyUI nhận interrupt, `failure cancelled` ≤ 5 s; drain → không `accept` thêm, xong job thì `bye drained` |
| 4.6 | Mất kết nối giữa job: job local chạy tiếp, `hello.running`/`unacked`, `resume=drop` | FakeHub rớt 20 s giữa job → kết quả gửi sau reconnect đúng một lần; `drop` → job local huỷ, file local xoá |
| 4.7 | Trạng thái: `/healthz.worker`, `/admin/api/status.worker`, endpoint drain/resume | Test API |
| 4.8 | Tray: `view_model()` theo bảng §8.3, khung Hub, menu Drain/Resume | Unit test `view_model` cho từng `link_state` (không cần màn hình) |
| 4.9 | CLI `qis worker …`, installer `-HubUrl -JoinToken` | `qis worker join` ghi config + token, restart; `leave` tắt và gửi `bye` |

### 10.4 m5: dashboard hub

| # | Việc | Nghiệm thu |
|---|---|---|
| 5.1 | Quyền `qis:*` vào catalog, gán mặc định cho role | Employee chỉ thấy job/key của mình; admin thấy tất cả |
| 5.2 | `/api/hub/overview`, `/workers`, `/workers/{id}`, `/requests`, `/requests/{id}`, pause/resume/forget, settings | Số liệu khớp công thức §7 trên dữ liệu seed cố định (test so số) |
| 5.3 | View Overview, Workers, Requests, API Keys, Settings → Hub theo DESIGN v2 | Với 3 FakeWorker đang chạy: worker hiện đúng state, %, s/ảnh chuẩn; tắt một FakeWorker → thành offline trong ≤ 15 s; Pause trên console → worker không nhận job mới |
| 5.4 | Manual + Changelog theo template | `verify_app.py` xanh |

### 10.5 m6: E2E

| # | Kịch bản | Nghiệm thu |
|---|---|---|
| 6.1 | Hub trên Docker Desktop + MinIO, 1 worker thật (RTX 3060) nối LAN | OpenAI SDK t2i 1024² qua hub: thời gian ≈ thời gian local + ≤ 5 s; `url` mở được ảnh; `b64_json` decode đúng |
| 6.2 | Hub prod sau Cloudflare tunnel `qis.runnow.cc` + R2 thật, 2 worker: RTX 3060 12 GB (T2, turbo Q8) và RTX 3050 6 GB (T1, turbo Q4 tự merge, không có `base_q8`) | 100 job batch trộn t2i/edit: cả hai worker cùng chạy, phân chia theo `std_s` học được; preset cần `base_q8` chỉ chạy trên 3060; restart service 3050 → `warming` ~200 s, không nhận job trong lúc đó; mỗi job trả đúng `worker` + `model_fingerprint`; request sync 150 s không bị 524 |
| 6.3 | Chaos | Rút mạng worker giữa job → job hoàn tất ở worker kia; restart container hub giữa job → không mất job, không chạy trùng; tắt service worker → `bye` và requeue ngay |
| 6.4 | Retention (không có lifecycle rule, key S3 hiện có) | Đặt `retention_days = 1` trên bucket test: sweeper xoá object cũ theo prefix ngày; URL của file đã xoá → 410 |
| 6.5 | Tray | Rút mạng: tray chuyển vàng → cam trong ≤ 70 s; cắm lại → xanh; đổi token sai → đỏ "Join token rejected" |
| 6.6 | Tải | 500 job/giờ trong 2 giờ với 2–3 worker: RSS hub ổn định, không job nào kẹt ở `assigned` quá `deadline_s` |

---

## 11. Quyết định mở

Các lựa chọn dưới đã được chốt tạm để hai bên code được ngay; người dùng có thể muốn xem lại.

| # | Quyết định đã chọn | Phương án khác |
|---|---|---|
| Q1 | Chỉ WebSocket, **không** có HTTP long-poll fallback | Thêm `/worker/poll` nếu có mạng chặn WS |
| Q2 | `worker_id = node_id` có sẵn; xác thực chỉ bằng join token dùng chung, không ký ed25519 | Ký `hello` bằng khoá trong `node.json` để chống giả `worker_id` |
| Q3 | Preset lấy từ `presets.yaml` của worker; hub không sửa preset | Hub giữ preset và đẩy xuống worker trong `welcome` |
| Q4 | Kết quả hợp lệ đầu tiên thắng, kể cả từ attempt đã `lost` nếu job chưa kết thúc | Bỏ mọi kết quả của attempt đã hết lease (đơn giản hơn, phí GPU hơn) |
| Q5 | Với `b64_json`, `qis.raw`/`qis.extra` trả URL (trừ khi `qis_extra_b64: true`) | Giữ đúng hành vi QIS (base64 hết), chấp nhận response to gấp đôi |
| Q6 | Worker chuyển định dạng output (webp/jpeg) trước khi upload | Luôn PNG, hub/CDN chuyển |
| Q7 | Mỗi worker 1 slot GPU + 1 prefetch | Không prefetch (hub linh hoạt hơn, GPU nghỉ lâu hơn giữa job) |
| Q8 | Retention: output 30 ngày, input 7 ngày; sweeper của hub là cơ chế chính, lifecycle rule (31/8 ngày) chỉ là lưới an toàn do devops tạo tay | Một con số chung cho cả hai; cấp token quản trị bucket để hub tự áp lifecycle |
| Q9 | Hub chỉ kiểm size bằng HEAD, không kiểm sha256 trên R2 | Ký `x-amz-checksum-sha256` vào URL PUT nếu R2 hỗ trợ ổn định |
| Q10 | Worker khởi động lại thì bỏ job hub đang dở (hub giao lại) | Lưu ánh xạ attempt vào DB local và chạy tiếp |
| Q11 | Request sync bị ngắt khi job còn `queued` thì huỷ; đã giao thì chạy tiếp | Luôn chạy tiếp, hoặc luôn huỷ |
| Q12 | Số admission: `sync_max_wait_s` 600, `max_pending` key 50, `queue_max_jobs` 20000, `max_job_s` 1800, `rpm` 120 | Đo lại sau m6 |
| Q13 | Thêm `/v1/batches` (≤ 500 job/lần) ngoài `/v1/jobs` | Chỉ `/v1/jobs` |
| Q14 | Console polling 2 s thay vì SSE | SSE `/api/hub/events` |
| Q15 | API key là bảng riêng (`sk-qis-…`) thay vì service account của template | Dùng access key/secret của template (client OpenAI phải đổi cách gọi) |
| Q16 | Hub không tự chạy op CPU (pixelize…), mọi job đều qua worker | Hub chạy op CPU-only cho đỡ worker |
| Q17 | `/mcp` của hub (tool sinh ảnh cho Claude) chưa làm ở v1 | Port `qis/api/mcp.py` sang hub sau m6 |
| Q18 | Mô hình thời gian 3 tham số (`a1`, `aN`, `b`) theo (worker, class), học bằng EWMA từ `sample_s` worker đo; số ref của edit không có hệ số riêng (gộp vào `a1` của class `edit`) | Thêm hệ số theo số ref, hoặc hồi quy nhiều biến khi có đủ dữ liệu |
| Q19 | **Chưa** làm affinity theo prompt: không gom các job cùng prompt (khác seed) về một worker | Bật `prompt_affinity`: job có cùng `sha1(prompt + refs)` với job vừa chạy/đang chờ trên worker `w` được tính `aN` thay cho `a1` trong `est_w` (ComfyUI giữ được kết quả encode, đỡ nạp lại encoder 9.4 GB). Trên máy 12 GB mỗi job tiết kiệm phần lớn chi phí cố định; nên đo trước rồi mới bật |
| Q20 | Ghim fingerprint chỉ khi client yêu cầu (`extra_body.model_fingerprint`); mặc định job đi tới bất kỳ worker nào có preset, nên cùng seed có thể ra ảnh khác | Mặc định ghim theo fingerprint phổ biến nhất của preset (ít worker hơn, kết quả ổn định hơn) |
| Q21 | Băm sha256 file model ngoài manifest ở nền trên worker (vài phút cho file 5–8 GB, chỉ một lần) | Chỉ dùng `name:bytes` (nhanh, nhưng hai file cùng tên cùng size khác nội dung sẽ trùng fingerprint) |
