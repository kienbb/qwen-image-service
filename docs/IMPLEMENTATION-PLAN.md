# Implementation Plan — Qwen Image Service

*Version 0.1 (Draft) · 2026-09-29*  
Legend: ✅ Done ⏳ Planned 💤 Backlog  
Progress: Done 41/53 (77%)

## MILESTONE 0 — SCAFFOLDING + ENGINE (✅ done)

Dựng khung repo và tách engine khỏi gen.py/tools; gateway khởi động và điều khiển được ComfyUI.

| ID | Task | Owner | Priority | Status |
|---|---|---|---|---|
| M0-1 | Khung repo uv/ruff/pytest + vendor custom node | Kiên | Urgent | ✅ |
| M0-2 | Tách engine/workflows.py + golden test | Kiên | Urgent | ✅ |
| M0-3 | ComfyClient HTTP + WebSocket | Kiên | Urgent | ✅ |
| M0-4 | ComfySupervisor: spawn, health, restart, warm-up | Kiên | Urgent | ✅ |
| M0-5 | Config loader + app skeleton FastAPI | Kiên | High | ✅ |

**Verification:** uv run qis-gateway khởi động ComfyUI, /readyz = 200 sau warm-up; golden test workflow mới == gen.py/tools cũ xanh; kill ComfyUI thì supervisor tự dựng lại trong ≤ 30 s.

## MILESTONE 1 — QUEUE + SCHEDULER (✅ done)

Lõi xử lý đồng thời: DB, job model, admission, dispatcher, executor và phục hồi lỗi.

| ID | Task | Owner | Priority | Status |
|---|---|---|---|---|
| M1-1 | Schema SQLite + Alembic + ULID | Kiên | Urgent | ✅ |
| M1-2 | Cost model + admission control | Kiên | Urgent | ✅ |
| M1-3 | Dispatcher: priority, aging, affinity, fair-share | Kiên | Urgent | ✅ |
| M1-4 | LocalComfyExecutor + CpuPool | Kiên | Urgent | ✅ |
| M1-5 | Huỷ, timeout, retry OOM/crash, phục hồi khi restart | Kiên | High | ✅ |
| M1-6 | Fake ComfyUI + bộ test integration scheduler | Kiên | High | ✅ |

**Verification:** Test với fake ComfyUI: 30 job đồng thời → đúng max_pending vào queue, phần còn lại 429 + Retry-After; fair-share 2 key chạy xen kẽ; kill ComfyUI giữa job → job chạy lại và thành công; restart gateway → job queued vẫn còn.

## MILESTONE 2 — OPENAI API + JOBS + FILES (✅ done)

Mở API ra ngoài: images generations/edits theo chuẩn OpenAI, job API, file, API key, qua Cloudflare Tunnel.

| ID | Task | Owner | Priority | Status |
|---|---|---|---|---|
| M2-1 | Blob store sha256 + /v1/files | Kiên | Urgent | ✅ |
| M2-2 | API key: tạo bằng CLI, hash, middleware, scope | Kiên | Urgent | ✅ |
| M2-3 | Preset + /v1/images/generations, /edits, /v1/models | Kiên | Urgent | ✅ |
| M2-4 | /v1/jobs + keepalive chống 524 | Kiên | Urgent | ✅ |
| M2-5 | /v1/node/info, healthz/readyz, metrics | Kiên | High | ✅ |
| M2-6 | E2E GPU qua Cloudflare Tunnel | Kiên | High | ✅ |

**Verification:** Từ máy khác, openai.OpenAI(base_url=https://<tunnel>/v1).images.generate(model='pixelart-16bit-64') trả ảnh; job enhance 3 phút qua tunnel không bị 524; key sai nhận 401 đúng định dạng lỗi OpenAI.

## MILESTONE 3 — TOOLS + PIPELINE + STREAMING (✅ done)

Đưa mọi tool thành op, chạy pipeline nhiều bước, stream tiến độ, webhook, MCP; gen.py thành client.

| ID | Task | Owner | Priority | Status |
|---|---|---|---|---|
| M3-1 | Op refine, upscale (esrgan/nearest/lanczos) | Kiên | Urgent | ✅ |
| M3-2 | Op outpaint tổng hợp | Kiên | High | ✅ |
| M3-3 | Op CPU: pixelize, remove-bg, compose, fidelity | Kiên | High | ✅ |
| M3-4 | Pipeline executor ($prev, $step, map n) | Kiên | Urgent | ✅ |
| M3-5 | SSE job + stream/partial_images OpenAI | Kiên | High | ✅ |
| M3-6 | Webhook, retention, URL có ký | Kiên | Normal | ✅ |
| M3-7 | /mcp endpoint + gen.py thành client | Kiên | Normal | ✅ |

**Verification:** Một request pipeline enhance → generate(n=4) → refine → upscale → pixelize trả đủ 4 bộ output; SSE nhận progress + preview; webhook nhận chữ ký HMAC hợp lệ; Claude gọi được generate_image qua /mcp; gen.py cũ chạy lại đúng các lệnh trong README qua API.

## MILESTONE 4 — AUTH + ADMIN API (✅ done)

Nền backend cho dashboard: user, session, role, runtime settings, admin API và sự kiện realtime.

| ID | Task | Owner | Priority | Status |
|---|---|---|---|---|
| M4-1 | User, session, argon2id, CSRF, lockout | Kiên | Urgent | ✅ |
| M4-2 | Runtime settings có schema + áp dụng nóng | Kiên | Urgent | ✅ |
| M4-3 | Admin API: users, keys, jobs, models, system | Kiên | High | ✅ |
| M4-4 | SSE /admin/api/events + NVML + stats_minute | Kiên | High | ✅ |
| M4-5 | Audit log | Kiên | Normal | ✅ |

**Verification:** Test: viewer gọi PUT /admin/api/settings nhận 403, admin nhận 200 và max_pending mới áp dụng cho request kế tiếp; thiếu X-CSRF-Token nhận 403; sai password 5 lần bị khoá 15 phút.

## MILESTONE 5 — DASHBOARD SPA (✅ done)

Giao diện web dựng mới cho vận hành node hằng ngày.

| ID | Task | Owner | Priority | Status |
|---|---|---|---|---|
| M5-1 | Khung SPA: Vite, router, layout, theme, login | Kiên | Urgent | ✅ |
| M5-2 | Trang Tổng quan + Jobs | Kiên | Urgent | ✅ |
| M5-3 | Trang Playground | Kiên | High | ✅ |
| M5-4 | Trang Queue & Limits + Settings (form từ schema) | Kiên | High | ✅ |
| M5-5 | Trang Models, API Keys, Users | Kiên | High | ✅ |
| M5-6 | Trang Logs, System + test Playwright | Kiên | Normal | ✅ |

**Verification:** Playwright smoke: đăng nhập → đổi max_pending → tạo key → chạy playground ra ảnh → huỷ job → xem log; mọi trang render ở 1280px và 390px, sáng/tối.

## MILESTONE 6 — INSTALLER WINDOWS + MODEL PROFILES (⏳ planned)

Một lệnh irm | iex cài xong node trên Windows, chọn đúng bộ model theo phần cứng.

| ID | Task | Owner | Priority | Status |
|---|---|---|---|---|
| M6-1 | hardware.py + chọn tier T0–T5 | Kiên | Urgent | ✅ |
| M6-2 | Bake Viggle Q8/BF16 + manifest models.yaml | Kiên | Urgent | ✅ |
| M6-3 | Benchmark tier trên GPU cloud 12/16/24 GB | Kiên | High | ⏳ |
| M6-4 | install.ps1: preflight, uv, ComfyUI pin, tải model | Kiên | Urgent | ✅ |
| M6-5 | WinSW service + cloudflared + benchmark sau cài | Kiên | Urgent | ⏳ |
| M6-6 | CLI qis + doctor | Kiên | High | ✅ |

**Verification:** Trên VM Windows 11 sạch có GPU NVIDIA: một lệnh cài → service tự chạy sau reboot → /readyz = 200 → gọi API qua hostname tunnel thành công → dashboard đăng nhập được; chạy lại installer không phá cài đặt cũ.

## MILESTONE 7 — LINUX + RELEASE + HARDENING (⏳ planned)

Hỗ trợ Linux, quy trình phát hành, cập nhật có rollback, tài liệu vận hành; chốt v1.0.

| ID | Task | Owner | Priority | Status |
|---|---|---|---|---|
| M7-1 | install.sh + systemd | Kiên | Urgent | ⏳ |
| M7-2 | qis update có rollback + backup DB | Kiên | High | ✅ |
| M7-3 | CI build release + host script trên R2/Pages | Kiên | High | ⏳ |
| M7-4 | Soát bảo mật + load test | Kiên | High | ✅ |
| M7-5 | docs/API.md, docs/OPERATIONS.md, phát hành v1.0 | Kiên | Normal | ⏳ |

**Verification:** curl|bash trên Ubuntu 22.04 và 24.04 sạch cài thành công; qis update lên bản mới rồi rollback khi /readyz fail; CI tạo release có hash; docs/API.md và docs/OPERATIONS.md đủ để người khác tự cài và gọi API.

