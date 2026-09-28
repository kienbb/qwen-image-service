# API: Qwen Image Service

Base URL: `https://<hostname>/v1`, hoặc `http://127.0.0.1:8080/v1` khi gọi ngay trên node. Tài liệu OpenAPI tự sinh nằm ở `/docs`.

## 1. Xác thực và lỗi

- Mọi request `/v1/*` gửi kèm header `Authorization: Bearer sk-qis-…`. Tạo key trên dashboard (mục **API Keys**) hoặc bằng lệnh `qis key create --name <tên>`.
- Mỗi key có các **scope** sau:
  - `images:generate`: sinh ảnh.
  - `images:edit`: sửa ảnh từ ảnh tham chiếu.
  - `tools:*`: refine, upscale, outpaint, pixelize, remove-bg, compose, fidelity.
  - `files:*`: upload và đọc file.
  - `jobs:read`: đọc job, danh sách model, thông tin node.
  - `admin:*`: gọi admin API, dùng cho tự động hoá.
- Lỗi trả về đúng định dạng OpenAI, nên OpenAI SDK tự đọc được:
  ```json
  {"error": {"message": "queue is full (20 jobs waiting)", "type": "rate_limit_error", "param": null, "code": "queue_full"}}
  ```

| HTTP | code | Ý nghĩa |
|---|---|---|
| 400 | `invalid_request`, `invalid_pipeline`, `invalid_image`, `job_too_large`, `workflow_rejected` | Tham số sai |
| 401 | `missing_api_key`, `invalid_api_key`, `expired_api_key` | Thiếu key, key sai hoặc key hết hạn |
| 403 | `insufficient_scope`, `model_not_allowed`, `file_forbidden` | Key không đủ quyền |
| 429 | `queue_full`, `key_limit`, `rate_limit` | Hàng đợi đầy hoặc key vượt quota. Có header `Retry-After` (giây) và `X-Qis-Queue-Depth` |
| 500 | `execution_error`, `oom`, `server_error` | Lỗi khi chạy job |
| 503 | `engine_unavailable` | ComfyUI chưa sẵn sàng |
| 504 | `timeout`, `expired`, `still_running` | Job quá thời gian cho phép |

Mọi response đều có header `X-Request-Id` và `X-Qis-Node`. Các endpoint tạo job trả thêm `X-Qis-Job-Id`.

**Request chạy lâu (Cloudflare Tunnel):** Cloudflare cắt request nếu server không trả dữ liệu trong khoảng 100 giây.
- Endpoint đồng bộ trả kết quả bình thường nếu job xong trong 60 giây đầu.
- Nếu job lâu hơn, server gửi status `200` ngay rồi gửi một ký tự khoảng trắng mỗi 15 giây, JSON kết quả đến sau cùng. JSON hợp lệ cho phép khoảng trắng đứng trước, nên OpenAI SDK vẫn đọc được. **Nếu job thất bại ở giai đoạn này, body là `{"error": …}` nhưng status vẫn là 200.**
- Với job dài, nên dùng `stream: true` hoặc `POST /v1/jobs`.

## 2. Tương thích OpenAI

### Python (OpenAI SDK)
```python
from openai import OpenAI
client = OpenAI(base_url="https://img.example.com/v1", api_key="sk-qis-...")

r = client.images.generate(model="pixelart-16bit-64", prompt="a knight with a blue cape", n=2, size="768x768",
                           extra_body={"seed": 42, "palette": "db32"})
sprite_b64 = r.data[0].b64_json
extra = r.model_dump()["qis"]          # job_id, seeds, timings, raw (ảnh render gốc), extra.preview

r = client.images.edit(model="qwen-image-2.1-turbo", image=open("knight.png", "rb"),
                       prompt="Make his cape red. Keep everything else unchanged.")
```

### curl
```bash
curl https://img.example.com/v1/images/generations \
  -H "Authorization: Bearer $QIS_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen-image-2.1-turbo","prompt":"a cozy tavern interior","size":"1024x768","response_format":"url"}'

curl https://img.example.com/v1/images/edits -H "Authorization: Bearer $QIS_API_KEY" \
  -F model=qwen-image-2.1-turbo -F prompt="give him a wooden shield" -F "image[]=@knight.png"
```

### `POST /v1/images/generations`
| Trường | Cách xử lý |
|---|---|
| `model` | Tên preset (xem `GET /v1/models`). Mặc định là `api.default_preset` |
| `prompt` | Mô tả ảnh cần sinh. Preset `pixelart-*` tự bọc thêm style và câu hướng dẫn khung hình |
| `n` | 1–8. Seed của các ảnh là liên tiếp nhau |
| `size` | `WxH` bất kỳ, tự làm tròn về bội số của 32, hoặc `auto` |
| `quality` | `low` = turbo 4 step, `medium`/`auto` = mặc định của preset, `high` = turbo 8 step, `hd` = model base 10 step |
| `background` | `transparent` = ảnh RGBA gốc của Qwen-Image 2.1 |
| `output_format`, `output_compression` | `png` (mặc định), `webp`, `jpeg` |
| `response_format` | `b64_json` (mặc định) hoặc `url` (URL có chữ ký, hết hạn sau `storage.url_ttl_s`) |
| `stream`, `partial_images` | SSE theo định dạng OpenAI: `image_generation.partial_image`, rồi `image_generation.completed` |
| **extra** | `seed`, `steps`, `negative`, `cfg`, `enhance`, `style` (`16bit`/`32bit`/`none`), `aspect`, `mode`, `template` (`raw`/`asset`), `grid`, `colors`, `palette`, `dither`, `keep_bg`, `no_crop`, `preview`, `priority` (`interactive`/`batch`), `webhook_url`, `retention_hours` |

Response có đúng dạng OpenAI (`created`, `data[]`, `usage`) cộng thêm trường `qis`:
```json
{"qis": {"job_id": "job_…", "model": "pixelart-16bit-64", "seeds": [42, 43],
         "timings": [{"op": "generate", "seconds": 31.2}, {"op": "pixelize", "seconds": 0.2}],
         "raw": [{"b64_json": "…"}], "extra": {"preview": [{"b64_json": "…"}]}, "degraded": false}}
```
Với preset pixel art, `data[i]` là sprite kích thước thật, `qis.raw` là ảnh render gốc, còn `qis.extra.preview` là bản phóng to của sprite.

### `POST /v1/images/edits`
Gửi dạng multipart:
- `image` hoặc `image[]`: 1–3 ảnh. Ảnh đầu tiên quyết định khung hình của kết quả.
- `prompt`, `model`, `n`, `size`, `quality`, …
- Các tham số extra gửi thành field của form. Giá trị số, boolean và mảng viết dưới dạng JSON.

Chưa hỗ trợ `mask` (inpaint). Cách thay thế là mô tả thay đổi bằng lời trong prompt.

### `GET /v1/models`
Liệt kê các preset mà node đang chạy được với bộ model đã cài. Mỗi preset có thêm trường `qis`, gồm `description`, `ops`, `est_s` và `commercial_ok`.

| Preset | Pipeline |
|---|---|
| `qwen-image-2.1-turbo` | generate (turbo 5 step, 1024 px) |
| `qwen-image-2.1-base` | generate (model base, 10 step) |
| `qwen-image-2.1-hq` | generate (turbo 8 step), sau đó Real-ESRGAN x2 |
| `pixelart-16bit-64`, `pixelart-32bit-128`, `pixelart-icon-32` | generate (style pixel), sau đó pixelize |

Admin chỉnh danh sách preset trên dashboard; file gốc là `config/presets.yaml`.

## 3. Job API (bất đồng bộ, pipeline nhiều bước)

```
POST   /v1/jobs                    → 202 {id, status, position, eta_s, …}
GET    /v1/jobs/{id}               → job (status, progress, result.outputs[].images[].url, error)
GET    /v1/jobs/{id}/events        → SSE: queued, started, op_started, progress, preview, op_done, succeeded|failed|canceled
DELETE /v1/jobs/{id}               → huỷ (job đang chờ sẽ bị gỡ khỏi hàng đợi; job đang chạy sẽ bị interrupt)
POST   /v1/jobs/{id}/retry         → job mới với cùng spec
GET    /v1/jobs?status=&limit=&after=
```
Header `Idempotency-Key` có hiệu lực 24 giờ: gửi lại cùng key sẽ nhận lại đúng job cũ, không tạo job mới. Khi kết nối lại SSE, gửi `Last-Event-ID` để nhận lại các sự kiện đã bỏ lỡ.

### Pipeline
```json
{
  "inputs": {"hero": "file_01…"},
  "ops": [
    {"op": "enhance_prompt", "prompt": "a goblin archer", "template": "asset", "style": "16bit"},
    {"op": "generate", "prompt": "$prev.text", "n": 4, "size": 768, "refs": ["$inputs.hero"]},
    {"op": "refine", "image": "$prev", "scale": 1.5},
    {"op": "upscale", "image": "$prev", "method": "esrgan", "scale": 2},
    {"op": "pixelize", "image": "$step[1]", "grid": 64, "palette": "db32"}
  ],
  "outputs": ["$step[3]", "$step[4]"],
  "priority": "batch",
  "webhook_url": "https://example.com/hook",
  "retention_hours": 168
}
```
- Cách tham chiếu giữa các bước:
  - `$inputs.<tên>`: file trong `inputs`.
  - `$prev`: ảnh của bước ngay trước. `$step[i]`: ảnh của bước thứ i.
  - `$step[i].preview`: ảnh preview của bước pixelize.
  - `$prev.text`: prompt do bước trước sinh ra; chỉ dùng được cho các trường chữ.
  - Có thể ghi thẳng file id.
- Ảnh đầu vào nhận 4 dạng: file id, data URL, base64, hoặc URL https (chỉ khi bật `inputs.allow_url`).
- Khi một bước sinh `n > 1` ảnh, các bước xử lý ảnh phía sau chạy lần lượt trên từng ảnh.
- Nếu chỉ muốn chạy một preset: `{"preset": "pixelart-icon-32", "prompt": "a sword", "params": {"n": 2, "palette": "db32"}}`.

### Các op
| op | Chạy trên | Tham số chính |
|---|---|---|
| `generate` | GPU | `prompt`, `refs` (tối đa 3 ảnh, dùng để edit), `mode` (`turbo`/`base`/`base_q8`), `steps`, `n`, `seed`, `width`/`height` hoặc `size`+`aspect`, `negative`, `cfg`, `template`, `style`, `transparent`, `enhance` |
| `enhance_prompt` | GPU | `prompt`, `refs`, `template`, `style`. Trả `text` và `data.ratio`; kết quả được cache |
| `refine` | GPU | `image`, `prompt` (để trống thì dùng prompt của bước trước), `width`/`height`/`scale`, `engine`, `nodes`, `denoise`, `steps`, `seed` |
| `upscale` | GPU/CPU | `image`, `width`/`height`/`scale`, `method` (`esrgan`/`nearest`/`lanczos`), `model` (`realesrgan-x2`/`-x4`) |
| `outpaint` | GPU | `image`, `width`, `height`, `prompt`, `anchor_y`, `draft`, `steps`, `seed`, `feather`, `scene`, `refine_nodes` |
| `pixelize` | CPU | `image`, `grid`, `colors`, `palette` (`adaptive`/`pico8`/`sweetie16`/`db32`), `downscale`, `dither`, `crop`, `remove_bg`, `preview_size` |
| `remove_bg` | CPU | `image`, `tolerance` |
| `compose` | CPU | `ui`, `model`, `top`, `bottom`, `max_width` |
| `fidelity` | CPU | `reference`, `images`. Trả `data.scores[]` gồm `struct`, `hue`, `scale`, `x`, `y` |

Job chỉ gồm op CPU chạy ở hàng đợi CPU riêng, không phải chờ GPU.

## 4. Tools (gọi một op)
```
POST /v1/tools/{enhance-prompt|refine|upscale|outpaint|pixelize|remove-bg|compose|fidelity}[?async=1]
```
- Body gửi dạng JSON (ảnh ghi bằng file id, data URL hoặc base64) hoặc multipart (ảnh là file đính kèm).
- Mặc định request chờ đến khi có kết quả. Thêm `?async=1` để nhận ngay `202` cùng job.
- Gửi `"response_format": "b64_json"` để nhận ảnh dạng base64 trong response.
```bash
curl https://img.example.com/v1/tools/pixelize -H "Authorization: Bearer $QIS_API_KEY" \
  -F image=@render.png -F grid=48 -F palette=db32
```

## 5. Files
```
POST   /v1/files (multipart: file, purpose=input) → {id, width, height, has_alpha, sha256, expires_at}
GET    /v1/files/{id}           GET /v1/files/{id}/content[?format=webp|jpeg]           DELETE /v1/files/{id}
```
URL có chữ ký (`…/content?exp=…&sig=…`) mở được mà không cần API key cho đến khi hết hạn.

Thời gian lưu mặc định:
- File input: 24 giờ.
- File output: 72 giờ.
- File trung gian: 1 giờ.

## 6. Webhook
Khi job kết thúc, server gửi `POST` tới `webhook_url` với body `{"type": "job.succeeded", "job": {...}}` và header `X-Qis-Signature: sha256=<hex>`. Chữ ký là `HMAC-SHA256(node secret, body)`. Nếu webhook trả lỗi, server thử lại tối đa 3 lần.

## 7. MCP (Claude và các agent khác)
- Endpoint là `POST /mcp` (transport Streamable HTTP), xác thực bằng API key giống `/v1`.
- Các tool có sẵn: `generate_image`, `edit_image`, `pixelize`, `upscale`, `refine`, `outpaint`, `remove_background`, `get_job`, `list_models`.
- Ảnh nhỏ (như sprite) được trả kèm trực tiếp trong response; mọi ảnh đều có kèm URL có chữ ký.

Thêm vào Claude Code:
```bash
claude mcp add --transport http qwen-image https://img.example.com/mcp --header "Authorization: Bearer sk-qis-..."
```

## 8. Node
| Endpoint | Nội dung |
|---|---|
| `GET /healthz` | Gateway còn chạy hay không |
| `GET /readyz` | `200` khi ComfyUI sẵn sàng, model đã nạp và đủ custom node; ngược lại `503` |
| `GET /v1/node/info` | Thông tin node dạng hợp đồng có version: `node_id`, GPU, model, preset, trạng thái hàng đợi, hiệu năng. Mesh sau này sẽ đọc endpoint này |
| `GET /metrics` | Prometheus. Chỉ gọi được từ loopback, hoặc bằng key có scope `admin:*` |
