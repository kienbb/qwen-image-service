# Roadmap: Qwen Image Service

*Version 0.1 · 2026-09-29 · Owner: Kiên*

Tài liệu liên quan: [DESIGN](DESIGN.md) (thiết kế kỹ thuật) · [BRD](BRD.md) · [PRD](PRD.md) · [Implementation Plan](IMPLEMENTATION-PLAN.md) · [Tasks](TASKS.md)

> **Giả định lập lịch:** 1 dev full-time (có AI hỗ trợ), làm từ thứ Hai đến thứ Sáu, bắt đầu 30/09/2026, không tính nghỉ lễ.
> Các mốc ngày là **kế hoạch dự kiến**, sẽ cập nhật lại khi xong mỗi milestone.

> **Trạng thái 2026-09-29:** đã làm xong code cho M0–M7; 41/53 task hoàn tất (xem [TASKS](TASKS.md)). Tất cả được kiểm chứng trên máy tham chiếu RTX 3050:
> - bộ test (golden, API trên fake ComfyUI, admin, MCP, load 100 client);
> - E2E trên GPU thật qua OpenAI SDK;
> - installer Windows chạy từ đầu đến cuối vào thư mục sạch (`-NoService`), sau đó chạy doctor, benchmark và `qis update`;
> - `install.sh` chạy trên WSL Ubuntu.
>
> Còn lại trước khi gắn tag v1.0:
> 1. Chạy thử service (WinSW/systemd) trên máy có quyền admin.
> 2. Chạy thử qua Cloudflare Tunnel thật.
> 3. Benchmark các tier T2–T5 trên GPU lớn.
> 4. Host script cài đặt công khai (R2/Pages).
> 5. Gắn tag `v1.0.0`.
>
> Lịch bên dưới là kế hoạch ban đầu, giữ lại để đối chiếu.

---

## 1. Tổng quan theo giai đoạn

| Giai đoạn | Milestone | Thời gian | Bản phát hành | Kết quả người dùng thấy được |
|---|---|---|---|---|
| **P1: Core API** | M0 · M1 · M2 | 30/09 → 23/10 (3,5 tuần) | `v0.1.0-alpha` | Gọi OpenAI SDK từ máy khác qua Cloudflare Tunnel ra ảnh; có queue, trả 429 khi đầy |
| **P2: Tools & Pipeline** | M3 | 26/10 → 05/11 (1,5 tuần) | `v0.2.0-alpha` | Mọi tool gọi được qua API; chạy pipeline nhiều bước; theo dõi tiến độ qua SSE; có webhook và MCP |
| **P3: Dashboard** | M4 · M5 | 06/11 → 27/11 (3 tuần) | `v0.3.0-beta` | Quản trị toàn bộ trên web: đăng nhập, queue, key, user, model, playground |
| **P4: Installer & Release** | M6 · M7 | 30/11 → 22/12 (3,5 tuần) | **`v1.0.0`** | Một lệnh cài lên máy Windows hoặc Linux có NVIDIA; tự chọn model theo máy; cập nhật có rollback |
| **P5: Mesh** | *Deferred* | từ 01/2027 | `v2.x` | Nhiều node chia tải cho nhau (thiết kế riêng trong `docs/MESH.md`) |

## 2. Timeline

```
Tuần (bắt đầu)  09/28  10/05  10/12  10/19  10/26  11/02  11/09  11/16  11/23  11/30  12/07  12/14  12/21
M0 Engine        ██████▌
M1 Scheduler            ███████▌
M2 OpenAI API                  ██████▌ ◆ v0.1-alpha (23/10)
M3 Tools/Pipe                          ██████████▌ ◆ v0.2-alpha (05/11)
M4 Auth/Admin                                    ▐█████
M5 Dashboard                                           ███████████▌ ◆ v0.3-beta (27/11)
M6 Installer Win                                                   ██████████
M7 Linux/Release                                                             ██████████ ◆ v1.0 (22/12)
```

| Milestone | Bắt đầu | Kết thúc | Task | Mốc kiểm chứng (tóm tắt) |
|---|---|---|---|---|
| M0 Scaffolding + Engine | 30/09 | 06/10 | QIS-001 → 007 | Golden test xanh; supervisor tự dựng lại ComfyUI trong ≤ 30 s |
| M1 Queue + Scheduler | 07/10 | 15/10 | QIS-008 → 013 | 30 job đồng thời: đúng số job vào queue, phần còn lại nhận 429; crash và restart không mất job |
| M2 OpenAI API + Jobs + Files | 16/10 | 23/10 | QIS-014 → 019 | OpenAI SDK chạy qua tunnel; job 3 phút không bị lỗi 524 |
| M3 Tools + Pipeline + Streaming | 26/10 | 05/11 | QIS-020 → 026 | Pipeline 5 bước chạy trong 1 request; Claude gọi được qua `/mcp` |
| M4 Auth + Admin API | 06/11 | 12/11 | QIS-027 → 031 | Phân quyền theo role đúng; đổi setting có hiệu lực ngay |
| M5 Dashboard SPA | 13/11 | 27/11 | QIS-032 → 037 | Playwright smoke xanh ở màn 1280 px và 390 px |
| M6 Installer Windows + Profiles | 30/11 | 10/12 | QIS-038 → 043 | VM Windows sạch: 1 lệnh là cài xong, gọi được API qua tunnel |
| M7 Linux + Release + Hardening | 11/12 | 22/12 | QIS-044 → 048 | Ubuntu 22.04/24.04 cài được; update rồi rollback đúng; tag v1.0.0 |

## 3. Nội dung từng bản phát hành

### v0.1.0-alpha (23/10/2026): "gọi được từ xa"
- `/v1/images/generations`, `/v1/images/edits`, `/v1/models` tương thích OpenAI SDK.
- `/v1/jobs` (tạo, xem, huỷ, retry), `/v1/files`, `/v1/node/info`, `/healthz`, `/readyz`, `/metrics`.
- Queue có admission control, fair-share, affinity; phục hồi khi ComfyUI crash hoặc gateway restart.
- API key tạo bằng CLI (`qis key create`). Keepalive để vượt giới hạn 100 s của Cloudflare.
- **Chưa có:** dashboard và installer. Muốn chạy phải `uv run` bằng tay trên máy dev.

### v0.2.0-alpha (05/11/2026): "đủ bộ công cụ"
- Op `refine`, `upscale` (esrgan/nearest/lanczos), `outpaint`, `pixelize`, `remove-bg`, `compose`, `fidelity`, `enhance_prompt`.
- Pipeline nhiều bước trong một job; SSE tiến độ và preview; `stream`/`partial_images` theo chuẩn OpenAI.
- Webhook có ký HMAC; retention và URL có ký; MCP endpoint.
- `gen.py` và `tools/*.py` chuyển thành client của API, giữ nguyên flag.

### v0.3.0-beta (27/11/2026): "vận hành bằng web"
- Đăng nhập (username hoặc email + password), 3 role, CSRF, khoá tài khoản khi đăng nhập sai nhiều lần, audit log.
- Dashboard: Tổng quan, Jobs, Playground, Queue & Limits, Settings, Models, API Keys, Users, Logs, System.
- Runtime settings có hiệu lực ngay, không cần restart.

### v1.0.0 (22/12/2026): "cài một lệnh"
- `irm … | iex` (Windows) và `curl … | bash` (Linux), tự detect phần cứng, chọn tier T0–T5 cùng bộ model và upscaler.
- Manifest `models.yaml` có kiểm sha256; file Viggle Q4/Q8/BF16 bake sẵn.
- Chạy thành service (WinSW/systemd), tích hợp cloudflared, benchmark sau khi cài.
- CLI `qis` (status, doctor, update có rollback, models, profile, admin, key, backup).
- Tài liệu `docs/API.md` và `docs/OPERATIONS.md`; đã soát bảo mật và load test.

## 4. Đường găng và phụ thuộc

```
M0 engine ──► M1 scheduler ──► M2 API ──► M3 tools/pipeline ──┐
                                   │                          ├──► M7 release v1.0
                                   └──► M4 admin API ──► M5 dashboard ──┤
                                                                        │
                    (song song được từ M2) hardware.py + manifest ──► M6 installer ──┘
```

- **Đường găng:** M0 → M1 → M2 → M4 → M5 → M7. Dashboard là khối lớn nhất (3 tuần).
- **Làm song song được:** `hardware.py` và bake model (QIS-038, QIS-039) không phụ thuộc UI, có thể kéo lên làm xen kẽ trong M3–M5 nếu cần rút ngắn thời gian.
- **Phụ thuộc bên ngoài:**
  - Qwen phản hồi về license. Gửi mail trước M6 vì việc này quyết định HF repo để public hay private.
  - Cần domain và Cloudflare zone cho `get.<domain>` và hostname tunnel trước M2 (test) và M7 (phát hành).
  - Cần thuê GPU cloud 12/16/24 GB theo giờ cho QIS-040.
  - Cần một VM Windows sạch và một VM Ubuntu sạch có GPU cho M6 và M7.

## 5. Cổng chất lượng mỗi milestone

Một milestone chỉ được coi là xong khi đạt đủ các điều kiện sau:
1. Dòng **Verification** của milestone trong [Implementation Plan](IMPLEMENTATION-PLAN.md) đã được kiểm chứng và có ghi chép (log, ảnh chụp màn hình hoặc báo cáo test).
2. CI xanh: ruff, unit, golden, integration với fake ComfyUI.
3. Từ M2 trở đi: e2e GPU chạy trên máy tham chiếu (RTX 3050 6 GB) không bị chậm đi quá 5% so với lần chạy trước.
4. Đã cập nhật trạng thái trong [TASKS](TASKS.md) và [DESIGN](DESIGN.md) nếu thiết kế thay đổi.

## 6. Sau v1.0 (backlog có thứ tự)

| Ưu tiên | Hạng mục | Lý do | Task |
|---|---|---|---|
| 1 | **Thiết kế mesh (P5)**: overlay mạng, discovery, định tuyến theo ETA cộng affinity, peer token ed25519, đồng bộ scope `cluster`, `RemoteNodeExecutor` | Mục tiêu dài hạn: mỗi máy cài là một node và cả mesh chia nhau xử lý request | QIS-049 |
| 2 | BlobStore S3/R2 | Điều kiện cần để mesh chia sẻ file giữa các node | QIS-052 |
| 3 | SSO (Google Workspace OIDC / Cloudflare Access JWT) | Bỏ password riêng khi có nhiều người vận hành | QIS-051 |
| 4 | Gộp batch trên GPU ≥ 16 GB | Tăng throughput khi có nhiều request cùng prompt | QIS-050 |
| 5 | Nhiều GPU trên một máy | Máy trạm có 2 GPU trở lên | QIS-053 |
| – | Hỗ trợ GPU không phải NVIDIA (ROCm, Apple MPS) | Chỉ làm khi có nhu cầu thật | – |

### Phác thảo P5: Mesh (để bàn sau)
Đã được chuẩn bị sẵn từ P1–P4 (xem DESIGN §15): `node_id` + khoá ed25519, ULID, interface `Executor`, blob lưu theo sha256, setting chia scope `node`/`cluster`, và `/v1/node/info` làm hợp đồng capability.
Các giai đoạn dự kiến:
1. **Mesh-α:** danh sách peer khai báo tay; node nhận request có thể đẩy job sang peer có ETA thấp nhất; API key vẫn khai báo riêng ở từng node.
2. **Mesh-β:** discovery tự động, đồng bộ user/key/preset, lưu file chung trên R2.
3. **Mesh-GA:** một hostname chung cho cả cụm, cân bằng tải theo tier và model đang nạp, dashboard xem toàn cụm.

## 7. Rủi ro ảnh hưởng tiến độ

| Rủi ro | Tác động lên lịch | Phương án |
|---|---|---|
| Dashboard phình ra ngoài 8 trang | M5 trễ 1–2 tuần | Giữ đúng danh sách trang; trang mới đưa vào backlog |
| License không cho phân phối lại GGUF đã bake | M6 thêm 2–3 ngày | Dùng repo HF private + `HF_TOKEN`, hoặc để installer bake tại máy (`qis models bake`) |
| Tier T2–T5 chạy không như bảng ước tính | M6 thêm 1–2 ngày | Benchmark GPU cloud sớm (có thể kéo lên làm trong M3) |
| ComfyUI cần nâng version giữa dự án | 1–3 ngày | Giữ pin 0.37.0 cho tới v1.0; chỉ nâng khi có lỗi bắt buộc |

## 8. Lịch cập nhật tài liệu

- Cuối mỗi milestone: cập nhật trạng thái trong `TASKS.md` và `IMPLEMENTATION-PLAN.md` (sinh lại từ `docs/planning/plan.json`), và chỉnh ngày trong roadmap này.
- Thay đổi kiến trúc: sửa `DESIGN.md` trước, code sau.
