# Vận hành: Qwen Image Service

## 1. Cài đặt

Yêu cầu:
- Máy NVIDIA có tối thiểu 6 GB VRAM. Nên có 32 GB RAM, vì text encoder 9,4 GB được stream từ RAM.
- Driver NVIDIA từ bản 570 trở lên (từ 580 thì dùng được PyTorch cu130).
- Khoảng 30–60 GB ổ trống tuỳ tier.

**Windows** (mở PowerShell bằng quyền Administrator):
```powershell
irm https://<host>/install.ps1 | iex
# hoặc có tham số:
& ([scriptblock]::Create((irm https://<host>/install.ps1))) -Root D:\qis -Tier T2 -Yes -TunnelToken <token>
```

**Linux** (Ubuntu 22.04/24.04, cần root):
```bash
curl -fsSL https://<host>/install.sh | sudo bash
curl -fsSL https://<host>/install.sh | sudo bash -s -- --root /opt/qis --tier T2 --yes --tunnel-token <token>
```

Nếu repo để private, thêm `-GithubToken <PAT>` (hoặc `--github-token`) để installer clone được mã nguồn.

Installer lần lượt làm các việc sau:
1. Kiểm tra hệ thống: GPU, driver, dung lượng ổ trống, quyền admin.
2. Cài uv và Python 3.13 vào thư mục `<root>`.
3. Clone app vào `<root>/app`.
4. Chạy `qis setup` để hoàn tất phần còn lại:
   - Detect phần cứng, gợi ý tier (T0–T5) và biến thể (recommended/quality/fast/custom).
   - Tải ComfyUI 0.37.0 và ComfyUI-GGUF theo commit đã pin.
   - Cài PyTorch bản CUDA phù hợp với driver.
   - Tải model: có resume khi đứt mạng và kiểm tra sha256. File đã có sẵn trong các thư mục chỉ định bằng `--reuse-models` sẽ được dùng lại.
   - Ghi `config/config.toml`, tạo tài khoản admin và API key đầu tiên.
   - Đăng ký service (WinSW trên Windows, systemd trên Linux) và cài cloudflared nếu có token.
   - Chờ node sẵn sàng rồi chạy benchmark. Kết quả benchmark dùng để đặt `max_pending` và các con số ETA.

Chạy lại installer là an toàn: bước nào đã xong thì được bỏ qua, file model tải dở được tải tiếp.

Cấu trúc thư mục sau khi cài:
```
<root>/app        mã nguồn (git)          <root>/.venv     môi trường Python
<root>/comfyui    ComfyUI đã pin          <root>/models    model (diffusion_models, text_encoders, vae, upscale_models)
<root>/config     config.toml, presets.yaml
<root>/data       qis.db, blobs/, logs/, backups/, comfy/ (thư mục input/output của ComfyUI), secret.key, node.json
<root>/bin        uv, qis-service.exe (WinSW), cloudflared.exe
```

## 2. Chọn tier

| Tier | VRAM | Model | Tốc độ ước tính (turbo, 768 px) |
|---|---|---|---|
| T0 | < 6 GB / không có GPU | Chỉ Real-ESRGAN, chạy các tool CPU | – |
| T1 | 6–8 GB | turbo Q4_K_M + base Q4_K_M | ~30 s/ảnh (đo trên RTX 3050) |
| T2 | 10–12 GB | turbo Q8_0 + base Q4_K_M | ~20 s |
| T3 | 16 GB | turbo Q8_0 + base Q4/Q8 | ~14 s |
| T4 | 24 GB | như T3, text encoder nằm luôn trong VRAM | ~8 s |
| T5 | ≥ 32 GB | như T4, còn dư chỗ cho batch | ~5 s |

- Chỉ số của T1 là đo thực tế; T2–T5 là ước tính. Benchmark sau khi cài sẽ cập nhật số thật cho máy của bạn.
- Đổi tier sau khi cài: vào dashboard, mục **Models**, bấm "Áp dụng profile", hoặc chạy `qis models profile T2` rồi `qis models download <id>`. Sau đó restart service.
- **License:** toàn bộ weights Qwen-Image 2.1 (base, bản turbo của Viggle, các GGUF) theo Qwen Research License, **chỉ được dùng phi thương mại**. Real-ESRGAN theo BSD-3.

## 3. Cloudflare Zero Trust Tunnel

1. Vào Cloudflare Zero Trust, mục Networks > Tunnels, tạo tunnel rồi lấy token.
2. Chạy `qis tunnel <token>`, hoặc truyền `-TunnelToken` ngay lúc cài.
3. Thêm Public Hostname, ví dụ `img.example.com`, trỏ tới service `http://127.0.0.1:8080`.
4. Mở dashboard, vào **Settings** > nhóm `node`, đặt `public_url` = `https://img.example.com`. URL có chữ ký và link trả về cho client sẽ dùng hostname này.
5. Vào Zero Trust > Access > Applications, tạo 2 policy cho hostname trên:
   - **Bypass** cho các path `/v1/*`, `/mcp`, `/healthz`, `/readyz`. Các path này đã được bảo vệ bằng API key.
   - **Allow (email của bạn)** cho `/admin*` và `/docs`. Dashboard lúc đó có 2 lớp bảo vệ: Access trước, rồi đến tài khoản đăng nhập của node.
6. Kiểm tra từ một máy khác:
   ```bash
   curl https://img.example.com/healthz
   curl https://img.example.com/v1/models -H "Authorization: Bearer sk-qis-..."
   ```

Lưu ý:
- Gateway chỉ nghe `127.0.0.1`, nên không cần mở port firewall. ComfyUI cũng chỉ nghe loopback và không bao giờ lộ ra ngoài.
- Cloudflare cắt request khi server không trả dữ liệu trong khoảng 100 giây. Gateway đã tự gửi keep-alive (xem `docs/API.md`, mục 1). Client dùng lâu dài nên chuyển sang `/v1/jobs` kết hợp SSE.
- Upload qua Cloudflare bị giới hạn khoảng 100 MB. Mặc định node chỉ nhận ảnh tối đa 20 MB (`inputs.max_bytes`).

## 4. Hằng ngày

```bash
qis status                     # trạng thái gateway + readiness
qis doctor                     # kiểm tra driver, torch/CUDA, ComfyUI, node, model, ổ đĩa, cloudflared
qis logs -f                    # log gateway;  qis logs --comfy -f  để xem log ComfyUI
qis service restart|stop|start|status
qis key create --name unity    # tạo API key (key chỉ hiện một lần)
qis admin reset-password --username admin
qis settings get queue         # xem setting runtime (sửa trên dashboard, có hiệu lực ngay)
qis db backup                  # sao lưu DB (node cũng tự sao lưu hằng ngày, giữ 7 bản)
qis benchmark --key <admin:* key> --apply
```

Hàng đợi (dashboard > **Queue & Limits**):

| Setting | Ý nghĩa |
|---|---|
| `queue.max_pending` | Số job GPU tối đa đang chờ. Vượt thì client nhận `429 queue_full` |
| `queue.max_wait_s` | Nếu tổng thời gian chờ ước tính vượt ngưỡng này, job mới cũng bị từ chối |
| `keys.default_max_pending`, `keys.default_rpm` | Quota mặc định cho mỗi key. Từng key có thể đặt quota riêng |
| `scheduler.*` | Trọng số chia lượt: priority, aging (job chờ lâu được cộng điểm), affinity (ưu tiên job dùng model đang nạp), fair-share giữa các key |
| `job.max_cost_s` | Job nào ước tính tốn quá số giây GPU này thì bị từ chối ngay |

Thêm vài điểm:
- Có thể tạm dừng hàng đợi trên dashboard, ví dụ để bảo trì. Job mới vẫn được nhận vào hàng đợi, chỉ chưa chạy.
- `cpu.workers` cần restart mới có hiệu lực.

## 5. Cập nhật và gỡ cài đặt

```bash
qis update --check                 # xem có bản mới không
qis update                         # sao lưu DB, git checkout, cài lại, migrate, restart; nếu /readyz không lên trong 3 phút thì tự rollback
qis update --ref v1.1.0
qis uninstall                      # gỡ service, giữ lại file
qis uninstall --purge [--purge-models]
```

## 6. Xử lý sự cố

| Triệu chứng | Kiểm tra |
|---|---|
| `/readyz` trả 503, `comfy=crashed` | Xem `qis logs --comfy`. Supervisor tự restart ComfyUI với backoff. Nếu lỗi do thiếu model, chạy `qis doctor` |
| `missing_nodes` khác rỗng | Custom node chưa được chép vào ComfyUI. Restart service (supervisor sẽ chép lại), hoặc chạy lại `qis setup` |
| Job lỗi `oom` | Node đã tự thử lại 1 lần với text encoder chạy trên CPU. Nếu vẫn lỗi: tăng `comfy.reserve_vram` trong config.toml, giảm `size`, hoặc chọn tier thấp hơn |
| Rất chậm, GPU gần 100% dù không có job | Kiểm tra ứng dụng khác đang chiếm VRAM (game, trình duyệt, Unity) |
| 524 từ Cloudflare | Client đang gọi request đồng bộ nhưng đã tắt `api.sync_keepalive`. Bật lại setting này, hoặc dùng `/v1/jobs` |
| Quên mật khẩu admin | `qis admin reset-password --username admin` (chạy trên chính máy node) |
| Port 8188 bị chiếm | Một ComfyUI cũ còn sống. Supervisor tự tắt nó qua `data/comfy/comfyui.pid`, hoặc đổi `comfy.port` |

Thư mục log: `<root>/data/logs`. Gồm `gateway.log`, `comfyui.log`, và log của WinSW khi chạy trên Windows.

## 7. Bảo mật

- API key chỉ được lưu dạng hash (HMAC với pepper riêng của node). Key hiện đúng một lần lúc tạo.
- Mật khẩu hash bằng argon2id. Đăng nhập sai 5 lần thì tài khoản bị khoá 15 phút; ngoài ra có giới hạn 20 lần đăng nhập mỗi phút cho mỗi IP.
- Cookie phiên có `HttpOnly`, `SameSite=Lax`, và `Secure` khi truy cập qua HTTPS. Mọi request thay đổi dữ liệu phải có `X-CSRF-Token` và Origin hợp lệ.
- Tài khoản admin đầu tiên chỉ tạo được ngay trên máy node (qua localhost), không tạo được qua tunnel.
- Ảnh đầu vào bị giới hạn dung lượng và số pixel, được decode lại thành PNG sạch để bỏ metadata. Tải ảnh từ URL mặc định tắt; khi bật chỉ nhận https và chặn IP nội bộ.
- Node không có endpoint nào nhận workflow ComfyUI thô từ client.
- `data/secret.key` và `data/node.json` là bí mật của node. Sao lưu chúng cùng `qis.db`.
