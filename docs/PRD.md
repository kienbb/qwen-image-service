# PRD — Qwen Image Service

*Version 0.1 · 2026-09-29*

## 1. Tính năng — API TƯƠNG THÍCH OPENAI

**1.1 POST /v1/images/generations** — Nhận model (preset), prompt, n 1–8, size WxH/auto, quality, background transparent, output_format, response_format b64_json/url. Tham số mở rộng qua extra_body (seed, steps, grid, palette…).

**1.2 POST /v1/images/edits** — Multipart 1–3 ảnh ref + prompt, mask tuỳ chọn. Ảnh đầu quyết định khung output.

**1.3 Stream + partial images** — stream:true trả SSE theo sự kiện image_generation.partial_image / completed của OpenAI, preview latent tối đa 3 ảnh.

**1.4 GET /v1/models** — Liệt kê preset khả dụng trên profile hiện tại, kèm est_s và cờ commercial_ok.

**1.5 Chống 524 Cloudflare** — Request đồng bộ gửi keepalive mỗi 15 s; job ước tính > 90 s có thể trả 400 use_async kèm job_id khi tắt keepalive.

## 2. Tính năng — JOB, PIPELINE VÀ TOOLS

**2.1 Job API bất đồng bộ** — POST /v1/jobs trả 202 kèm position và eta_s; GET, DELETE (huỷ, interrupt nếu đang chạy), retry, liệt kê theo key. Hỗ trợ Idempotency-Key 24 h.

**2.2 Pipeline nhiều op** — Một job gồm chuỗi op (enhance, generate, refine, upscale, outpaint, pixelize…) nối bằng $prev/$step[i]; n>1 tự map qua các op sau.

**2.3 Tools endpoint** — /v1/tools/{op} cho từng op đơn lẻ; op CPU (pixelize, remove-bg, compose, fidelity) chạy ngoài queue GPU.

**2.4 SSE tiến độ + webhook** — Sự kiện queued/started/progress/preview/op_done/succeeded/failed, nối lại bằng Last-Event-ID; webhook ký HMAC, retry 3 lần.

**2.5 Files** — Upload ảnh một lần dùng lại bằng file_id; lưu theo sha256; URL có ký hết hạn; tự dọn theo retention.

**2.6 MCP endpoint** — /mcp Streamable HTTP với các tool generate_image, edit_image, pixelize, upscale, refine, outpaint, get_job, list_presets.

## 3. Tính năng — QUEUE VÀ SCHEDULER

**3.1 Admission control** — Từ chối 429 queue_full khi số job chờ ≥ max_pending hoặc tổng ETA > max_wait_s; 429 key_limit khi vượt quota key; 400 khi job vượt max_cost_s.

**3.2 Dispatcher công bằng** — Chấm điểm priority + aging + model affinity − fair-share theo key; chỉ đẩy 1 job (+1 prefetch) xuống ComfyUI.

**3.3 Phục hồi lỗi** — OOM retry 1 lần ở cấu hình nhẹ hơn; ComfyUI crash tự restart và chạy lại job (tối đa 2 lần); restart gateway không mất job.

**3.4 Giới hạn theo máy** — max_pending mặc định = target_wait_s / avg_job_s từ benchmark lúc cài; ETA cập nhật EWMA theo thời gian chạy thật.

## 4. Tính năng — DASHBOARD VÀ XÁC THỰC

**4.1 Đăng nhập** — Username hoặc email + password (argon2id), session cookie + CSRF, khoá 15 phút sau 5 lần sai; role admin/operator/viewer.

**4.2 Tổng quan & Jobs** — GPU, VRAM, nhiệt độ, queue, job đang chạy kèm preview; bảng job lọc, huỷ, retry, xem output.

**4.3 Playground** — Form generate/edit/tool/pipeline, kéo thả ref, xem progress trực tiếp, copy lệnh curl/Python tương ứng.

**4.4 Queue, Settings, Models** — Chỉnh max_pending, trọng số scheduler, retention, public URL… có hiệu lực ngay; xem/tải/đổi profile model, chạy benchmark.

**4.5 API Keys & Users** — Tạo key (hiện 1 lần), scope, priority, quota, hạn dùng, thu hồi; quản lý user và role.

**4.6 Logs & System** — Tail log gateway/ComfyUI, restart ComfyUI/service, kiểm tra và áp dụng update, sao lưu DB.

## 5. Tính năng — CÀI ĐẶT VÀ VẬN HÀNH

**5.1 Installer một lệnh** — install.ps1 (irm|iex) và install.sh (curl|bash), có chế độ không tương tác qua flag/biến môi trường; chạy lại an toàn (idempotent).

**5.2 Detect phần cứng + chọn profile** — Đọc GPU/VRAM/driver/compute cap, RAM, ổ đĩa; đề xuất tier T0–T5 và bộ model + upscaler; hiện dung lượng tải, thời gian/ảnh, license.

**5.3 Tải model có kiểm tra** — Theo manifest models.yaml: resume, sha256, HF_TOKEN, dùng lại file đã có.

**5.4 Service + Cloudflare Tunnel** — Đăng ký WinSW/systemd tự khởi động; tuỳ chọn --tunnel-token cài cloudflared trỏ vào 127.0.0.1.

**5.5 CLI qis** — status, logs, doctor, benchmark, update (có rollback), models, profile, admin, key, db backup, uninstall.

## Kiến trúc hệ thống

- Gateway: Python 3.12, FastAPI + uvicorn, một process, bind 127.0.0.1:8080; ra Internet qua cloudflared
- Engine: ComfyUI 0.37.0 (pin commit) chạy subprocess chỉ nghe loopback, giám sát bởi ComfySupervisor; custom node ComfyUI-GGUF, pixelart_retro, viggle_turbo
- Scheduler: queue trong gateway, dispatcher chấm điểm, Executor interface (LocalComfyExecutor ở v1), CpuPool cho op CPU
- Lưu trữ: SQLite WAL (SQLAlchemy + Alembic) cho metadata; blob store theo sha256 trên filesystem
- Frontend: React + Vite + TypeScript + Tailwind + shadcn/ui, build static serve tại /admin
- Cài đặt: uv + torch cu128/cu126, WinSW (Windows) / systemd (Linux), manifest models.yaml, gói release trên GitHub Releases + R2

## Data model (core entities)

- Node — id, name, public_url, pubkey
- User — id, email, username, pw_hash, role, disabled, locked_until
- Session — id(hash), user_id, csrf, expires_at, revoked
- ApiKey — id, prefix, hash, scopes, priority, max_pending, rpm, daily_images, allowed_models, expires_at, revoked_at
- Job — id, api_key_id, origin_node, exec_node, kind, spec, status, priority, attempts, error, result, eta_s, idempotency_key, webhook_url
- JobEvent — job_id, seq, type, data
- File / Blob — file_id, sha256, mime, width, height, has_alpha, purpose, expires_at / sha256, path, refcount
- Setting — scope (node|cluster), key, value; PromptCache — key, prompt, ratio; Audit — actor, action, target

## Lộ trình

- **Phase 1 — Core API (P1)** — Engine tách khỏi gen.py, ComfyUI supervisor, queue/scheduler, OpenAI images API, jobs, files, API key
- **Phase 2 — Tools & Pipeline (P2)** — Mọi tool thành op, pipeline nhiều bước, SSE/partial, webhook, retention, MCP, gen.py thành client
- **Phase 3 — Dashboard (P3)** — Auth, admin API, runtime settings, SPA 8+ trang
- **Phase 4 — Installer & Release (P4)** — Detect phần cứng, manifest + tier, install.ps1/install.sh, service, tunnel, update/rollback, CI release
- **Phase 5 — Mesh (P5)** — Nhiều node chia tải, định tuyến theo ETA, đồng bộ cluster settings (thiết kế riêng docs/MESH.md)

## Phân quyền

| Permission | Ý nghĩa | Mặc định |
|---|---|---|
| images:generate | Gọi /v1/images/generations và op generate | Mọi API key |
| images:edit | Gọi /v1/images/edits và op edit/outpaint | Mọi API key |
| tools:* | Gọi /v1/tools/* (refine, upscale, pixelize…) | Mọi API key |
| files:* | Upload, đọc, xoá file của chính key | Mọi API key |
| jobs:read | Xem job của chính key | Mọi API key |
| admin:* | Gọi admin API bằng key (automation) | Không; chỉ cấp tay |
| role admin | Toàn quyền dashboard: setting, model, user, key | Tài khoản tạo lúc cài |
| role operator | Quản lý job, key của mình, playground; không sửa hệ thống | Không |
| role viewer | Chỉ xem dashboard | Không |
