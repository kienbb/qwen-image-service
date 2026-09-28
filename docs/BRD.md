# BRD — Qwen Image Service

*Version 0.1 · 2026-09-29*

## 1. Tổng quan dự án

**Tên dự án:** Qwen Image Service  
**Mục đích:** Biến pipeline Qwen-Image 2.1 đang chạy trên một máy thành service cài một lệnh lên máy NVIDIA bất kỳ, gọi được qua API tương thích OpenAI từ mọi nơi.

## 2. Vấn đề kinh doanh

**Vấn đề cốt lõi:** Pipeline sinh asset hiện chỉ chạy bằng CLI trên đúng một máy (RTX 3050): muốn dùng phải ngồi trước máy đó, mỗi lần một lệnh, không ai khác gọi được, và dựng lại trên máy khác mất nửa ngày cài ComfyUI, node, model bằng tay.

**Pain points:**

- Chỉ gọi được từ máy local qua gen.py và tools/*.py; không có API để tool, game editor hay agent khác gọi
- Không có queue: hai lệnh chạy cùng lúc tranh GPU, dễ OOM; không biết phải chờ bao lâu
- Cài lên máy mới phải làm tay: ComfyUI đúng version, 2 custom node, bake Viggle GGUF (~25 phút, 45 GB tạm), tải ~15 GB model
- Không biết nên chọn bản quant và upscaler nào cho máy có VRAM khác 6 GB
- Muốn đổi tham số (steps, queue, giữ file) phải sửa code; không có giao diện quản trị
- custom node viggle_turbo.py chỉ nằm trong thư mục portable bị gitignore, mất máy là mất

**Chi phí hiện tại:** Ước tính (giả định): dựng lại môi trường trên máy mới ≈ 4–6 giờ thủ công; mỗi người muốn dùng phải nhờ người giữ máy chạy hộ; GPU nhàn rỗi phần lớn thời gian vì chỉ một người dùng được.

## 3. Mục tiêu

- Một lệnh cài (irm|iex trên Windows, curl|bash trên Linux) đưa máy NVIDIA sạch đến trạng thái gọi được API trong ≤ 60 phút, không thao tác tay ngoài trả lời prompt
- Client OpenAI SDK chỉ đổi base_url và api_key là sinh được ảnh (images.generate / images.edit)
- Mọi tool hiện có (generate, edit, enhance, refine, upscale, outpaint, pixelize, remove-bg, compose, fidelity) gọi được qua API
- Queue nhận nhiều request cùng lúc, từ chối bằng 429 + Retry-After khi đầy, không mất job khi restart
- Dashboard web chỉnh được queue, quota, model, key, user mà không sửa file hay restart
- Kiến trúc node sẵn sàng ghép mesh (node_id, Executor, blob sha256, /v1/node/info) mà không phải viết lại

## 4. Ngoài phạm vi (v1)

- GPU AMD, Intel, Apple Silicon và inference trên CPU
- Nhiều GPU trên một máy
- Mesh nhiều node, đồng bộ user/key giữa node (để P5)
- Nhận workflow ComfyUI thô từ client
- Billing, thanh toán, đăng ký tài khoản tự phục vụ
- SSO/OIDC (chỉ để sẵn interface)

## 5. Chỉ số thành công

- Cài mới trên 1 VM Windows sạch có NVIDIA: ≤ 60 phút tới /readyz = 200, không lỗi, trước 22/12/2026
- 30 request đồng thời trên máy 6 GB: 100% nhận 202/200 hoặc 429 có Retry-After, 0 job mất sau khi kill ComfyUI giữa chừng
- Overhead gateway ≤ 5% so với gọi ComfyUI trực tiếp (đo turbo 768², 10 lần)
- Chạy qua Cloudflare Tunnel: 0 lỗi 524 với job 3 phút (enhance) trong 20 lần thử
- Golden test: workflow JSON của engine mới giống hệt gen.py/tools cũ ở 100% case

## 6. Personas

- Owner / người vận hành node — cần cài nhanh, chọn đúng model theo máy, chỉnh queue và theo dõi GPU qua web
- Game dev / artist dùng tool nội bộ — cần gọi sinh sprite, edit, upscale từ script hoặc editor bằng API chuẩn
- Agent AI (Claude qua MCP, pipeline tự động) — cần API ổn định, có job id, SSE tiến độ và webhook

## 7. Phạm vi dự án

- **Trong phạm vi:** Gateway API (OpenAI-compat + jobs + tools), queue/scheduler, ComfyUI supervisor, dashboard, auth, installer Windows/Linux, tích hợp Cloudflare Tunnel
- **Ngoài phạm vi:** Non-NVIDIA, multi-GPU, mesh, billing, SSO
- **Người dùng:** 1 owner vận hành; client là tool nội bộ, script và agent qua API key
- **Nền tảng đích:** Windows 10/11 x64, Ubuntu 22.04/24.04; NVIDIA ≥ 6 GB VRAM, RAM ≥ 32 GB
- **Thời gian dự kiến:** ≈ 12 tuần, 30/09/2026 → 22/12/2026 (giả định 1 dev full-time có AI hỗ trợ)

## 8. Rủi ro chính

| Rủi ro | Khả năng | Tác động | Giảm thiểu |
|---|---|---|---|
| License Qwen Research chỉ cho phi thương mại; có thể không được phân phối lại GGUF đã bake | Cao | Cao | Hiển thị commercial_ok trên dashboard và /v1/models; repo HF để private + HF_TOKEN; giữ đường bake tại máy (qis models bake); gửi mail xin license trước M6. |
| Cloudflare cắt request im lặng > 100 s (524) với job base/enhance | Cao | TB | Keepalive khoảng trắng/SSE ping 15 s ngay từ M2; test bắt buộc job 3 phút qua tunnel trong M2. |
| ComfyUI đổi API/node khi nâng version làm hỏng workflow | TB | Cao | Pin commit ComfyUI 0.37.0 + ComfyUI-GGUF; golden test + e2e GPU chạy trước mỗi lần nâng pin. |
| Dashboard (M5) phình to làm trễ installer | TB | TB | M5 chỉ làm 8 trang đã chốt; trang phụ vào Deferred; installer M6 không phụ thuộc UI (dùng CLI qis). |
| Chỉ có máy 6 GB để test; tier T2–T5 chưa kiểm chứng | Cao | TB | Thuê GPU cloud theo giờ (12/16/24 GB) trong M6 để chạy benchmark từng tier trước khi chốt manifest. |
