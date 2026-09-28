# Tasks — Qwen Image Service

| ID | Issue Name | PIC | Priority | Status | Deadline | Sprint | Type | Difficulty | Milestone |
|---|---|---|---|---|---|---|---|---|---|
| QIS-001 | Dựng khung repo với uv, ruff, pytest và cấu trúc thư mục | Kiên | 1 - Urgent | Done | 9/30/2026 | 1 - Research And Plan | Task | 4 - Easy | M0 |
| QIS-002 | Đưa custom node viggle_turbo và pixelart_retro vào repo | Kiên | 1 - Urgent | Done | 9/30/2026 | 1 - Research And Plan | Task | 4 - Easy | M0 |
| QIS-003 | Tách engine/workflows.py từ gen.py và tools | Kiên | 1 - Urgent | Done | 10/2/2026 | 2 - Cooking | Task | 3 - Moderate | M0 |
| QIS-004 | Viết golden test so workflow mới với code cũ | Kiên | 1 - Urgent | Done | 10/2/2026 | 3 - Evaluating And Improve | Task | 3 - Moderate | M0 |
| QIS-005 | Viết ComfyClient async dùng HTTP và WebSocket | Kiên | 1 - Urgent | Done | 10/5/2026 | 2 - Cooking | Task | 3 - Moderate | M0 |
| QIS-006 | Dựng ComfySupervisor quản lý vòng đời ComfyUI | Kiên | 1 - Urgent | Done | 10/6/2026 | 2 - Cooking | Task | 2 - Hard | M0 |
| QIS-007 | Dựng app skeleton FastAPI và config loader | Kiên | 2 - High | Done | 10/6/2026 | 2 - Cooking | Task | 4 - Easy | M0 |
| QIS-008 | Thiết kế schema SQLite với Alembic và ULID | Kiên | 1 - Urgent | Done | 10/8/2026 | 2 - Cooking | Task | 3 - Moderate | M1 |
| QIS-009 | Viết cost model và admission control | Kiên | 1 - Urgent | Done | 10/9/2026 | 2 - Cooking | User Story | 3 - Moderate | M1 |
| QIS-010 | Dựng dispatcher với priority, aging, affinity, fair-share | Kiên | 1 - Urgent | Done | 10/12/2026 | 2 - Cooking | User Story | 2 - Hard | M1 |
| QIS-011 | Viết interface Executor, LocalComfyExecutor và CpuPool | Kiên | 1 - Urgent | Done | 10/13/2026 | 2 - Cooking | Task | 3 - Moderate | M1 |
| QIS-012 | Xử lý huỷ, timeout, retry và phục hồi khi restart | Kiên | 2 - High | Done | 10/14/2026 | 2 - Cooking | User Story | 2 - Hard | M1 |
| QIS-013 | Dựng fake ComfyUI và bộ test integration scheduler | Kiên | 2 - High | Done | 10/15/2026 | 3 - Evaluating And Improve | Task | 3 - Moderate | M1 |
| QIS-014 | Dựng blob store sha256 và API /v1/files | Kiên | 1 - Urgent | Done | 10/16/2026 | 2 - Cooking | User Story | 3 - Moderate | M2 |
| QIS-015 | Làm API key: tạo bằng CLI, hash, middleware, scope, quota | Kiên | 1 - Urgent | Done | 10/19/2026 | 2 - Cooking | User Story | 3 - Moderate | M2 |
| QIS-016 | Làm preset và endpoint /v1/images/generations, /edits, /v1/models | Kiên | 1 - Urgent | Done | 10/21/2026 | 2 - Cooking | User Story | 2 - Hard | M2 |
| QIS-017 | Làm /v1/jobs và keepalive chống lỗi 524 | Kiên | 1 - Urgent | Done | 10/22/2026 | 2 - Cooking | User Story | 3 - Moderate | M2 |
| QIS-018 | Làm /v1/node/info, readyz và /metrics | Kiên | 2 - High | In Progress | 10/22/2026 | 2 - Cooking | Task | 4 - Easy | M2 |
| QIS-019 | Kiểm thử E2E trên GPU qua Cloudflare Tunnel | Kiên | 2 - High | Done | 10/23/2026 | 3 - Evaluating And Improve | Task | 3 - Moderate | M2 |
| QIS-020 | Làm op refine và upscale | Kiên | 1 - Urgent | Done | 10/27/2026 | 2 - Cooking | User Story | 3 - Moderate | M3 |
| QIS-021 | Làm op outpaint tổng hợp | Kiên | 2 - High | Done | 10/29/2026 | 2 - Cooking | User Story | 2 - Hard | M3 |
| QIS-022 | Làm op CPU: pixelize, remove-bg, compose, fidelity | Kiên | 2 - High | Done | 10/29/2026 | 2 - Cooking | User Story | 4 - Easy | M3 |
| QIS-023 | Dựng pipeline executor nhiều op | Kiên | 1 - Urgent | Done | 11/2/2026 | 2 - Cooking | User Story | 2 - Hard | M3 |
| QIS-024 | Làm SSE sự kiện job và stream/partial_images kiểu OpenAI | Kiên | 2 - High | Done | 11/3/2026 | 2 - Cooking | User Story | 3 - Moderate | M3 |
| QIS-025 | Làm webhook, retention và URL có ký | Kiên | 3 - Normal | Done | 11/4/2026 | 2 - Cooking | User Story | 3 - Moderate | M3 |
| QIS-026 | Làm /mcp và chuyển gen.py thành client API | Kiên | 3 - Normal | Done | 11/5/2026 | 2 - Cooking | User Story | 3 - Moderate | M3 |
| QIS-027 | Làm user, session, argon2id, CSRF và khoá đăng nhập | Kiên | 1 - Urgent | Done | 11/9/2026 | 2 - Cooking | User Story | 3 - Moderate | M4 |
| QIS-028 | Làm runtime settings có schema và áp dụng nóng | Kiên | 1 - Urgent | Done | 11/10/2026 | 2 - Cooking | User Story | 3 - Moderate | M4 |
| QIS-029 | Làm admin API cho users, keys, jobs, models, system | Kiên | 2 - High | Done | 11/11/2026 | 2 - Cooking | User Story | 2 - Hard | M4 |
| QIS-030 | Làm SSE admin, đọc NVML và thống kê theo phút | Kiên | 2 - High | Done | 11/12/2026 | 2 - Cooking | Task | 3 - Moderate | M4 |
| QIS-031 | Ghi audit log cho thao tác quản trị | Kiên | 3 - Normal | Done | 11/12/2026 | 2 - Cooking | Task | 4 - Easy | M4 |
| QIS-032 | Dựng khung SPA: Vite, router, layout, theme, trang đăng nhập | Kiên | 1 - Urgent | Done | 11/16/2026 | 2 - Cooking | Task | 3 - Moderate | M5 |
| QIS-033 | Làm trang Tổng quan và Jobs | Kiên | 1 - Urgent | Done | 11/18/2026 | 2 - Cooking | User Story | 3 - Moderate | M5 |
| QIS-034 | Làm trang Playground | Kiên | 2 - High | Done | 11/20/2026 | 2 - Cooking | User Story | 3 - Moderate | M5 |
| QIS-035 | Làm trang Queue & Limits và Settings từ schema | Kiên | 2 - High | Done | 11/23/2026 | 2 - Cooking | User Story | 3 - Moderate | M5 |
| QIS-036 | Làm trang Models, API Keys và Users | Kiên | 2 - High | Done | 11/25/2026 | 2 - Cooking | User Story | 3 - Moderate | M5 |
| QIS-037 | Làm trang Logs, System và test Playwright | Kiên | 3 - Normal | Done | 11/27/2026 | 3 - Evaluating And Improve | Task | 3 - Moderate | M5 |
| QIS-038 | Viết hardware.py và logic chọn tier T0–T5 | Kiên | 1 - Urgent | Done | 11/30/2026 | 2 - Cooking | Task | 3 - Moderate | M6 |
| QIS-039 | Bake Viggle Q8/BF16 và viết manifest models.yaml | Kiên | 1 - Urgent | Done | 12/2/2026 | 2 - Cooking | Task | 3 - Moderate | M6 |
| QIS-040 | Chạy benchmark từng tier trên GPU cloud | Kiên | 2 - High | In Progress | 12/3/2026 | 3 - Evaluating And Improve | Task | 3 - Moderate | M6 |
| QIS-041 | Viết install.ps1: preflight, uv, ComfyUI pin, tải model | Kiên | 1 - Urgent | Done | 12/7/2026 | 2 - Cooking | User Story | 2 - Hard | M6 |
| QIS-042 | Đăng ký WinSW service, cloudflared và benchmark sau cài | Kiên | 1 - Urgent | In Progress | 12/9/2026 | 2 - Cooking | User Story | 3 - Moderate | M6 |
| QIS-043 | Làm CLI qis và lệnh doctor | Kiên | 2 - High | Done | 12/10/2026 | 2 - Cooking | Task | 3 - Moderate | M6 |
| QIS-044 | Viết install.sh và unit systemd cho Linux | Kiên | 1 - Urgent | In Progress | 12/14/2026 | 2 - Cooking | User Story | 3 - Moderate | M7 |
| QIS-045 | Làm qis update có rollback và backup DB | Kiên | 2 - High | In Progress | 12/16/2026 | 2 - Cooking | User Story | 2 - Hard | M7 |
| QIS-046 | Dựng CI build release và host script cài đặt | Kiên | 2 - High | In Progress | 12/17/2026 | 2 - Cooking | Task | 3 - Moderate | M7 |
| QIS-047 | Soát bảo mật và load test trước v1.0 | Kiên | 2 - High | Done | 12/18/2026 | 3 - Evaluating And Improve | Task | 3 - Moderate | M7 |
| QIS-048 | Viết docs/API.md, docs/OPERATIONS.md và phát hành v1.0 | Kiên | 3 - Normal | In Progress | 12/22/2026 | 3 - Evaluating And Improve | Task | 4 - Easy | M7 |
| QIS-049 | Thiết kế mesh nhiều node (docs/MESH.md) | Kiên | 3 - Normal | — | — | 1 - Research And Plan | User Story | 1 - Extremely Hard | Deferred |
| QIS-050 | Gộp batch nhiều job cùng prompt trên GPU lớn | Kiên | 4 - Low | — | — | 1 - Research And Plan | User Story | 3 - Moderate | Deferred |
| QIS-051 | Đăng nhập SSO qua OIDC hoặc Cloudflare Access JWT | Kiên | 4 - Low | — | — | 1 - Research And Plan | User Story | 3 - Moderate | Deferred |
| QIS-052 | Hỗ trợ lưu trữ S3/R2 cho BlobStore | Kiên | 4 - Low | — | — | 1 - Research And Plan | User Story | 3 - Moderate | Deferred |
| QIS-053 | Hỗ trợ nhiều GPU trên một máy | Kiên | 4 - Low | — | — | 1 - Research And Plan | User Story | 3 - Moderate | Deferred |

## Mô tả chi tiết

**QIS-001 — Dựng khung repo với uv, ruff, pytest và cấu trúc thư mục**  
Tạo pyproject.toml, uv.lock, thư mục server/ engine/ web/ install/ cli/ tests/, CI lint+test trên GitHub Actions. Xong khi uv run pytest và ruff chạy xanh trên CI.

**QIS-002 — Đưa custom node viggle_turbo và pixelart_retro vào repo**  
Chuyển viggle_turbo.py (đang chỉ nằm trong ComfyUI portable) và pixelart_retro vào engine/comfy_nodes/, ghi comfy.lock pin ComfyUI 0.37.0 (73c9bad) và ComfyUI-GGUF (373048b).

**QIS-003 — Tách engine/workflows.py từ gen.py và tools**  
Hàm thuần t2i, edit, enhance, upscale, refine và đuôi pixelize; hằng số sang constants.py; tên file model lấy từ profile. Xong khi gen.py cũ vẫn chạy khi import từ engine.

**QIS-004 — Viết golden test so workflow mới với code cũ**  
Snapshot JSON workflow của gen.py/tools cũ cho ≥ 20 tổ hợp tham số (t2i, edit 1–3 ref, base, turbo, transparent, pixelize, refine, upscale); engine mới phải khớp 100%.

**QIS-005 — Viết ComfyClient async dùng HTTP và WebSocket**  
httpx + websockets: upload ảnh, submit prompt, nhận progress/executing/executed và preview binary, interrupt, lấy output. Thay polling /history. Xong khi chạy được 1 job thật và nhận đủ sự kiện.

**QIS-006 — Dựng ComfySupervisor quản lý vòng đời ComfyUI**  
Spawn ComfyUI loopback với reserve-vram theo config, health check 5 s, restart có backoff, xoay log, warm-up 256² 1 step, trạng thái starting/ready/crashed. Xong khi kill process thì tự lên lại ≤ 30 s.

**QIS-007 — Dựng app skeleton FastAPI và config loader**  
config.toml + biến môi trường, lifespan khởi động supervisor, request id, log JSON, /healthz, /readyz. Xong khi uv run qis-gateway lên và /readyz phản ánh trạng thái ComfyUI.

**QIS-008 — Thiết kế schema SQLite với Alembic và ULID**  
Bảng nodes, jobs, job_events, files, blobs, api_keys, settings, prompt_cache, audit, stats_minute theo DESIGN §4; WAL mode; ID ULID có prefix; migration đầu tiên chạy sạch trên DB rỗng.

**QIS-009 — Viết cost model và admission control**  
Ước lượng giây GPU theo op, steps, megapixel, model; từ chối 429 queue_full/key_limit kèm Retry-After, 400 khi vượt max_cost_s. Unit test cho từng nhánh.

**QIS-010 — Dựng dispatcher với priority, aging, affinity, fair-share**  
Chấm điểm theo DESIGN §5.4, trọng số đọc từ settings; chỉ giữ 1 job chạy + 1 prefetch ở ComfyUI; tính ETA và position cho từng job. Unit test thứ tự chọn job.

**QIS-011 — Viết interface Executor, LocalComfyExecutor và CpuPool**  
Protocol capabilities/estimate/run/cancel; LocalComfyExecutor chạy op GPU qua ComfyClient và phát sự kiện; CpuPool ProcessPool cho pixelize. Xong khi generate + pixelize chạy qua executor.

**QIS-012 — Xử lý huỷ, timeout, retry và phục hồi khi restart**  
DELETE job gọi /interrupt; timeout theo cost; OOM retry 1 lần với cấu hình nhẹ (degraded); crash retry ≤ 2; khởi động lại gateway đưa job running về queued.

**QIS-013 — Dựng fake ComfyUI và bộ test integration scheduler**  
Server giả mô phỏng queue, progress WS, lỗi OOM, crash; test 30 job đồng thời, fair-share 2 key, cancel, crash-retry, restart-recovery chạy trên CI không cần GPU.

**QIS-014 — Dựng blob store sha256 và API /v1/files**  
Lưu theo nội dung + refcount; validate ảnh (byte, pixel, decode lại bằng Pillow); POST/GET/DELETE /v1/files và /content. Test ảnh bomb bị từ chối.

**QIS-015 — Làm API key: tạo bằng CLI, hash, middleware, scope, quota**  
Key sk-qis-…, lưu sha256+pepper, prefix; qis key create/revoke; middleware Bearer, kiểm scope, max_pending/rpm theo key; lỗi 401/403 đúng định dạng OpenAI.

**QIS-016 — Làm preset và endpoint /v1/images/generations, /edits, /v1/models**  
presets.yaml; map trường OpenAI (size, quality, background, n, response_format, output_format) và extra_body sang JobSpec; response đúng dạng OpenAI + trường qis. Test bằng openai SDK.

**QIS-017 — Làm /v1/jobs và keepalive chống lỗi 524**  
POST/GET/DELETE/list/retry job, Idempotency-Key; request đồng bộ gửi khoảng trắng mỗi 15 s, tắt được bằng api.sync_keepalive (khi đó trả 400 use_async).

**QIS-018 — Làm /v1/node/info, readyz và /metrics**  
node_id + ed25519 sinh lần đầu; node/info trả GPU, profile, preset, queue, perf; metrics Prometheus chỉ cho loopback hoặc token. [Trạng thái 2026-09-29: Chưa chạy qua Cloudflare Tunnel thật (chờ token của owner); keep-alive đã có test tự động.]

**QIS-019 — Kiểm thử E2E trên GPU qua Cloudflare Tunnel**  
Dựng tunnel thử, gọi từ máy khác bằng openai SDK: generate mỗi preset, edit, job enhance 3 phút (không 524), 30 request đồng thời. Ghi kết quả vào tests/e2e/REPORT.md. Cắt bản v0.1-alpha.

**QIS-020 — Làm op refine và upscale**  
refine turbo (sigma nodes) và base (denoise); upscale esrgan x2/x4 + Lanczos về đúng WxH, nearest cho pixel art, lanczos thuần. Kết quả khớp tools/refine.py và tools/upscale.py.

**QIS-021 — Làm op outpaint tổng hợp**  
Port tools/outpaint.py: canvas gợi ý, edit, upscale, refine, paste-back pixel gốc có feather; chạy trong một job, các bước con là sub-op trong cùng executor.

**QIS-022 — Làm op CPU: pixelize, remove-bg, compose, fidelity**  
Bọc pixelart/pixelize.py, compose_screenshot.py, model_fidelity.py thành op chạy trong CpuPool, không vào queue GPU; /v1/tools/{op} đồng bộ.

**QIS-023 — Dựng pipeline executor nhiều op**  
Resolve $inputs, $prev, $step[i]; n>1 map qua op sau; outputs chọn bước; cost cộng dồn; op pixelize đứng ngay sau generate được gộp vào cùng workflow ComfyUI.

**QIS-024 — Làm SSE sự kiện job và stream/partial_images kiểu OpenAI**  
/v1/jobs/{id}/events với Last-Event-ID; ping 15 s; preview latent2rgb; stream:true ở images API phát image_generation.partial_image/completed.

**QIS-025 — Làm webhook, retention và URL có ký**  
Webhook ký HMAC, retry 3 lần backoff; task dọn file hết hạn và blob refcount 0 mỗi 10 phút; hạn mức ổ đĩa và 507; URL ?exp&sig dùng public_url.

**QIS-026 — Làm /mcp và chuyển gen.py thành client API**  
MCP Streamable HTTP với 8 tool; gen.py và tools/*.py gọi API (giữ nguyên flag trong README). Xong khi Claude Code gọi được generate_image và các lệnh README chạy qua API. Cắt v0.2.

**QIS-027 — Làm user, session, argon2id, CSRF và khoá đăng nhập**  
Login bằng username hoặc email, cookie HttpOnly/Secure/SameSite, CSRF header + kiểm Origin, khoá 15 phút sau 5 lần sai, role admin/operator/viewer, qis admin create/reset-password, AuthProvider interface.

**QIS-028 — Làm runtime settings có schema và áp dụng nóng**  
Schema Pydantic cho queue.*, scheduler.*, cpu.*, api.*, inputs.*, storage.*, job.*; scope node/cluster; thay đổi phát sự kiện để scheduler/admission đọc lại ngay; export JSON Schema cho form.

**QIS-029 — Làm admin API cho users, keys, jobs, models, system**  
/admin/api/* theo DESIGN §6.9 kèm kiểm role; system: restart ComfyUI, tail log, version, backup DB; models: liệt kê, tải (progress), xoá, đổi profile, benchmark.

**QIS-030 — Làm SSE admin, đọc NVML và thống kê theo phút**  
/admin/api/events phát trạng thái node, queue, job, GPU (pynvml 2 s khi có người xem, 30 s khi không); ghi stats_minute cho biểu đồ 24 h.

**QIS-031 — Ghi audit log cho thao tác quản trị**  
Ghi đăng nhập, tạo/thu hồi key, đổi setting, đổi profile, huỷ job người khác kèm actor, IP; API lọc theo người và ngày.

**QIS-032 — Dựng khung SPA: Vite, router, layout, theme, trang đăng nhập**  
React + TS + Tailwind + shadcn/ui, TanStack Query/Router, client API có CSRF, layout sidebar, sáng/tối, login và buộc đổi password lần đầu; gateway serve web/dist tại /admin.

**QIS-033 — Làm trang Tổng quan và Jobs**  
Thẻ GPU/VRAM/nhiệt độ, queue, job đang chạy kèm preview realtime, biểu đồ 24 h; bảng Jobs lọc, chi tiết timeline, xem output, huỷ, retry, tải.

**QIS-034 — Làm trang Playground**  
Form theo preset/op, kéo thả ref, xem progress + preview, gallery kết quả, nút copy curl và Python tương đương với request vừa chạy.

**QIS-035 — Làm trang Queue & Limits và Settings từ schema**  
Form sinh từ JSON Schema, validate, nhãn restart required, hiển thị trạng thái tunnel và public URL. Xong khi đổi max_pending trên web có hiệu lực ngay.

**QIS-036 — Làm trang Models, API Keys và Users**  
Models: danh sách, license/commercial_ok, tải có progress, đổi profile, benchmark. Keys: tạo (hiện 1 lần), quota, thu hồi, usage. Users: tạo, khoá, reset, role.

**QIS-037 — Làm trang Logs, System và test Playwright**  
Tail log có lọc, restart, update, backup; smoke test Playwright luồng login → setting → key → playground → huỷ job ở 1280px và 390px. Cắt v0.3-beta.

**QIS-038 — Viết hardware.py và logic chọn tier T0–T5**  
Đọc nvidia-smi (tên, VRAM, driver, compute cap), RAM, CPU, ổ trống trên Windows/Linux; không dùng AdapterRAM; trả tier + cảnh báo (RAM < 32 GB, driver cũ). Dùng chung cho installer, doctor, dashboard.

**QIS-039 — Bake Viggle Q8/BF16 và viết manifest models.yaml**  
Bake bằng tools/merge_lora.py + sd-cli; đẩy lên repo HF (private nếu license chưa rõ); manifest có url, sha256, bytes, min_vram, license, commercial_ok và profile T0–T5.

**QIS-040 — Chạy benchmark từng tier trên GPU cloud**  
Thuê GPU 12/16/24 GB theo giờ, chạy qis benchmark cho mỗi profile, chốt reserve_vram, batch và thời gian/ảnh hiển thị trong installer. Ghi vào docs/BENCHMARK.md. [Trạng thái 2026-09-29: Chưa thuê GPU cloud: số T2–T5 vẫn là ước tính; benchmark tự chạy sau khi cài sẽ đo số thật.]

**QIS-041 — Viết install.ps1: preflight, uv, ComfyUI pin, tải model**  
Chạy được qua irm|iex và scriptblock có tham số; prompt chọn profile; uv + Python 3.12 + torch cu128/cu126; ComfyUI + node pin; tải model resume + sha256 + dùng lại file có sẵn; idempotent.

**QIS-042 — Đăng ký WinSW service, cloudflared và benchmark sau cài**  
Service qis-gateway tự khởi động và restart; --TunnelToken cài cloudflared service; đợi readyz, chạy benchmark, đặt max_pending; in tổng kết URL, admin, lệnh test. [Trạng thái 2026-09-29: Đã viết WinSW + cloudflared; chưa chạy trên máy có quyền Administrator.]

**QIS-043 — Làm CLI qis và lệnh doctor**  
status, start/stop/restart, logs, doctor (driver, torch thấy GPU, node bắt buộc, sha256 model, ổ đĩa, port, cloudflared), benchmark, models, profile, admin, key, db backup, uninstall. Kiểm thử trên VM Windows sạch.

**QIS-044 — Viết install.sh và unit systemd cho Linux**  
Đọc prompt từ /dev/tty, chế độ --yes khi không có tty, user qis, systemd hardening, cloudflared; kiểm thử trên Ubuntu 22.04 và 24.04 sạch có GPU. [Trạng thái 2026-09-29: install.sh đã chạy thành công trên WSL Ubuntu (--no-service); unit systemd chưa chạy thử.]

**QIS-045 — Làm qis update có rollback và backup DB**  
Tải release, kiểm hash, giải nén app.new, backup DB, migration, đổi thư mục nguyên tử, restart; rollback nếu readyz không lên trong 3 phút. Test cả nhánh thành công và rollback. [Trạng thái 2026-09-29: qis update đã kiểm chứng; nhánh rollback khi có service chưa chạy thử.]

**QIS-046 — Dựng CI build release và host script cài đặt**  
GitHub Actions: build web, đóng gói server+engine+install, SHA-256 (+ minisign), tạo release theo tag; publish install.ps1/install.sh lên Cloudflare Pages/R2 ở get.<domain>. [Trạng thái 2026-09-29: CI (lint + test Ubuntu/Windows + build web + release theo tag) đã xanh; chưa host script lên R2/Pages.]

**QIS-047 — Soát bảo mật và load test trước v1.0**  
Đối chiếu DESIGN §11 (ComfyUI loopback, bomb, SSRF, traversal, header giả, key trong log); load test 100 request đồng thời 30 phút trên máy 6 GB; không rò bộ nhớ, không job mất.

**QIS-048 — Viết docs/API.md, docs/OPERATIONS.md và phát hành v1.0**  
API: ví dụ OpenAI SDK, curl, jobs, pipeline, SSE, lỗi. Operations: cài, tunnel + Access policy, cập nhật, sao lưu, xử lý sự cố. Cập nhật README; tag v1.0.0. [Trạng thái 2026-09-29: docs/API.md, OPERATIONS.md xong; chưa gắn tag v1.0.0.]

**QIS-049 — Thiết kế mesh nhiều node (docs/MESH.md)**  
Chọn overlay mạng, discovery, định tuyến theo ETA + affinity, peer token ed25519, đồng bộ scope cluster (user/key/preset), RemoteNodeExecutor. Chưa xếp lịch — bắt đầu sau v1.0.

**QIS-050 — Gộp batch nhiều job cùng prompt trên GPU lớn**  
Gộp job cùng preset/prompt/size khác seed thành batch_size k khi VRAM ≥ 16 GB; đo throughput trước khi bật mặc định.

**QIS-051 — Đăng nhập SSO qua OIDC hoặc Cloudflare Access JWT**  
Cài AuthProvider cho Google Workspace OIDC và tin Cf-Access-Jwt-Assertion; ánh xạ email sang role.

**QIS-052 — Hỗ trợ lưu trữ S3/R2 cho BlobStore**  
Backend BlobStore cho S3-compatible (Cloudflare R2), URL presigned; cần cho mesh để chia sẻ file giữa node.

**QIS-053 — Hỗ trợ nhiều GPU trên một máy**  
Một ComfyUI cho mỗi GPU (--cuda-device N), mỗi GPU một LocalComfyExecutor, dispatcher chọn executor rảnh.

