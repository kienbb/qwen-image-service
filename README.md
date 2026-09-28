# Qwen Image Service: Qwen-Image 2.1 thành một node API cài một lệnh

Service chạy trên máy NVIDIA, dùng Qwen-Image 2.1 (GGUF, Viggle turbo) qua ComfyUI. Gồm:
- **API tương thích OpenAI**: `/v1/images/generations`, `/v1/images/edits`, `/v1/models`, gọi được bằng OpenAI SDK.
- **Job/pipeline API**: generate/edit, enhance, refine, upscale (Real-ESRGAN), outpaint, pixelize, remove-bg, compose, fidelity; theo dõi tiến độ qua SSE, có webhook.
- **MCP endpoint** cho Claude và các agent khác.
- **Hàng đợi** có admission control (trả 429 khi đầy), ưu tiên, chia lượt công bằng giữa các key, tự retry khi OOM hoặc crash, không mất job khi restart.
- **Dashboard web** `/admin`: đăng nhập, chỉnh setting có hiệu lực ngay, quản lý key, user và model, playground.
- **Installer một lệnh** (`irm | iex` trên Windows, `curl | bash` trên Linux): tự detect GPU và gợi ý bộ model phù hợp (tier T0–T5), đăng ký service, tích hợp Cloudflare Tunnel.

Tài liệu: [DESIGN](docs/DESIGN.md) · [API](docs/API.md) · [OPERATIONS](docs/OPERATIONS.md) · [PRD](docs/PRD.md) · [ROADMAP](docs/ROADMAP.md) · [TASKS](docs/TASKS.md)

## Cài đặt

```powershell
# Windows (PowerShell chạy bằng quyền Administrator)
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/kienbb/qwen-image-service/master/install/install.ps1))) -GithubToken <PAT>
```
```bash
# Linux
curl -fsSL https://raw.githubusercontent.com/kienbb/qwen-image-service/master/install/install.sh | sudo bash -s -- --github-token <PAT>
```
Repo đang để private nên installer cần token (`<PAT>`) để clone. Link `raw.githubusercontent.com` của repo private cũng phải kèm token; cách đơn giản hơn là tải file script về máy trước rồi chạy. Các tuỳ chọn khác xem [OPERATIONS §1](docs/OPERATIONS.md).

Gọi API sau khi cài:
```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="sk-qis-...")
img = client.images.generate(model="pixelart-16bit-64", prompt="a knight with a blue cape")
```

## Phát triển

```bash
uv venv --python 3.13 && uv pip install -e ".[dev]"
uv run pytest                         # golden workflow, API trên fake ComfyUI, admin, MCP
uv run qis key create --config .qis-dev/config.toml --name dev
uv run qis serve --config .qis-dev/config.toml --attach     # dùng ComfyUI đang chạy sẵn ở :8188
cd web && npm install && npm run dev  # dashboard (proxy tới :8080)
```

## CLI cũ (`gen.py`, `tools/`)

`gen.py` và `tools/{upscale,refine,outpaint}.py` giờ là client của service: giữ nguyên các flag nhưng gọi API thay vì gọi thẳng ComfyUI. Chúng đọc 2 biến môi trường:
- `QIS_URL`: mặc định `http://127.0.0.1:8080`.
- `QIS_API_KEY`: API key.

Kết quả vẫn nằm ở `output/<tên>/`, với tên file `raw_<i>.png`, `sprite_<grid>px_<i>.png`, `preview_<i>.png`.

## Ghi chú về chất lượng và prompt (từ bản CLI)

Service phải đang chạy (service đã cài, hoặc `uv run qis serve`) và biến môi trường `QIS_API_KEY` đã được đặt. Sau đó:

```bash
python gen.py "a knight with a blue cape and a sword"                       # text -> image, turbo 5 step
python gen.py "health potion, red liquid in a glass flask" --grid 32        # icon 32x32
python gen.py "a golden key with a red gem" --transparent                    # nền trong suốt native (RGBA)
python gen.py "a standing orc warrior" --aspect 2:3                         # khung dọc cho nhân vật đứng
python gen.py "a goblin archer" --enhance --count 4                          # viết lại prompt bằng rewriter chính thức, rồi gen 4 seed
python gen.py "make his armor gold and his cape red" --ref output/x/raw_00001_.png            # edit
python gen.py "Give the knight a wooden kite shield with a lion emblem" --ref knight.png      # thêm đồ vật: mô tả bằng chữ
python gen.py "The knight from <image1> holding the shield from <image2>" --ref a.png --ref b.png  # multi-ref (kém ổn định)
python gen.py "forest slime monster" --count 4                               # batch 4 ảnh
```

Kết quả nằm ở `output/<tên>/` gồm ba file:
- `raw_*.png`: ảnh render gốc (mặc định khoảng 768×768 px).
- `sprite_<grid>px_*.png`: sprite ở kích thước thật, ví dụ 64×64, đã ép palette và tách nền trong suốt.
- `preview_*.png`: sprite phóng to kiểu nearest-neighbor lên 256 px để xem.

Tuỳ chọn hay dùng:

| Flag | Ý nghĩa |
|---|---|
| `--steps 4..8` | Mặc định 5. Để 4 là nhanh nhất nhưng hơi nhiễu; 6–8 đẹp hơn một chút |
| `--size 768 / 1024` | Độ phân giải render, mặc định 768. Model tối ưu ở khoảng 1 MP (1024), nhưng sau khi pixel hoá thì không thấy đẹp hơn mà lại chậm hơn khoảng 20% |
| `--aspect 2:3` | Tỉ lệ khung. Khung dọc giúp nhân vật đứng chiếm nhiều pixel hơn |
| `--enhance` | Dùng rewriter chính thức của Qwen-Image 2.1 để viết lại prompt (xem phần dưới) |
| `--grid 32/48/64/128/256` | Cạnh dài của sprite (đơn vị: pixel art) |
| `--colors N` / `--palette pico8\|sweetie16\|db32` | Palette tự động N màu, hoặc dùng palette retro cố định |
| `--style 16bit\|32bit\|none` | Tiền tố phong cách: SNES/Genesis hoặc GBA/PS1 |
| `--transparent` | Yêu cầu Qwen tự sinh nền trong suốt |
| `--keep-bg` / `--no-crop` | Giữ nguyên nền / không tự crop theo chủ thể |
| `--mode base --steps 10` | Model gốc không turbo, dùng khi cần đối chiếu |
| `--dump-workflow f.json` | Xuất workflow để kéo thả vào giao diện web ComfyUI |

Nếu chỉ muốn pixel hoá một ảnh có sẵn: `python_embeded/python.exe pixelart/pixelize.py img.png --grid 64 --remove-bg`.

## Benchmark trên máy này (768×768, i5-14600KF, RTX 3050 6 GB, 64 GB RAM)

| Cấu hình | s/step | Tổng thời gian (prompt mới) | Chất lượng |
|---|---|---|---|
| **turbo** (Viggle bake vào GGUF), 5 step, encoder trên GPU | 3.8 | **~30 s** | ≈ base 25 step |
| turbo, 4 step | 3.8 | ~25 s | Có lấm tấm nhiễu, đôi khi sai màu chi tiết |
| base Q4_K_M, 10 step | 4.0 | ~45 s | Tốt |
| base Q4_K_M, 25 step, encoder trên CPU | 4.0 | ~200 s | Chuẩn gốc |
| base, 5 step | 4.0 | – | Hỏng |
| LoRA Viggle nạp lúc chạy (unmerged / stock loader) | 36 / 16 | 100–265 s | Tốt nhưng quá chậm do thiếu VRAM (đã bỏ) |
| LoRA Pruna 8 step nạp lúc chạy | 16 | ~130 s | Tốt, là bản v0.1 (đã bỏ) |

Ghi chú:
- Chỉ lần chạy đầu sau khi mở ComfyUI là chậm hơn (thêm khoảng 60–90 s để load model). Chạy lại cùng prompt thì bỏ qua được bước encode.
- Tắt bớt app đang dùng GPU (Unity, LM Studio, Chrome…) sẽ giúp model nằm trọn trong VRAM, nhờ đó nhanh hơn nữa.

## Prompt enhancer (`--enhance`)

Đây là ý tưởng lấy từ workflow ComfyUI của Viggle. Viggle turbo được chưng cất trên các prompt dài, giàu mô tả, do rewriter chính thức của Qwen-Image 2.1 viết. Workflow của Viggle cho chính text encoder Qwen3-VL (đã có sẵn trên máy, không cần tải thêm) chạy system prompt chính thức để viết lại prompt, lưu ở `prompts/pe_t2i_system.txt` và `prompts/pe_edit_system.txt`. Với edit, rewriter được nhìn thấy các ảnh tham chiếu.

- **Tốn thời gian:** khoảng 80–140 s cho mỗi prompt mới, vì máy này chỉ sinh được khoảng 2 token/s. Kết quả được cache trong `output/pe_cache.json`, nên gen thêm seed với cùng prompt sẽ không phải viết lại.
- **Hiệu quả thực tế:** với text→image, ảnh bám mô tả sát hơn (không tự thêm khiên như ảnh không enhance) và tự chọn khung phù hợp (ví dụ 2:3 cho nhân vật), nhưng mức cải thiện không lớn. Với multi-reference thì chỉ giúp một phần, xem mục "Giới hạn đã biết".
- **Nên dùng khi:** chốt một prompt quan trọng để gen hàng loạt seed (`--enhance --count 8`). Không nên bật cho kiểu thử nhanh từng prompt.

## Vì sao chọn Viggle bake vào GGUF

- Viggle turbo và Pruna đều là **LoRA** chưng cất từ Qwen-Image 2.1, không phải model riêng. Chúng không làm giảm VRAM.
- Nạp LoRA lúc chạy trên card 6 GB làm mỗi step chậm đi 4–9 lần, vì một phần model bị đẩy ra RAM và LoRA phải áp lại ở mỗi step.
- Cách xử lý: cộng LoRA vào trọng số BF16 ở độ chính xác fp32 (`tools/merge_lora.py`), rồi quantize lại đúng công thức Q4_K_M của file gốc bằng stable-diffusion.cpp. File ra có cùng dung lượng, cùng layout và cùng tốc độ với base, nhưng chỉ cần 5 step.
- Chọn Viggle vì: bản v0.2.1 cho chất lượng sát base (theo model card và kết quả test ở trên), chạy được 4–8 step, và hỗ trợ edit với 1–3 ảnh tham chiếu. Pruna v0.1 tự ghi là "chưa bằng base" và chỉ có hai mức cố định 5/8 step.

Khi Viggle ra bản LoRA mới, bake lại như sau (mất khoảng 25 phút, cần khoảng 45 GB ổ trống tạm thời). File BF16 và file LoRA đã được xoá để tiết kiệm ổ đĩa, nên phải tải lại trước:

```bash
curl -L -o downloads/qwen_image_2.1_bf16.safetensors https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_bf16.safetensors
curl -L -o downloads/<lora>.safetensors https://huggingface.co/Viggle/Qwen-Image-2.1-viggle-turbo/resolve/main/<lora>.safetensors
ComfyUI_windows_portable/python_embeded/python.exe tools/merge_lora.py downloads/qwen_image_2.1_bf16.safetensors <lora>.safetensors downloads/merged_f32.safetensors
tools/sdcpp/sd-cli.exe -M convert -m downloads/merged_f32.safetensors -o ComfyUI_windows_portable/ComfyUI/models/diffusion_models/qwen-image-2.1-viggle-Q4_K_M.gguf --type q4_K --tensor-type-rules "attn\.to_v\.=q6_K,img_mlp\.out\.=q6_K,img_in\.=bf16,txt_in\.=bf16,norm_q\.=bf16,norm_k\.=bf16"
```

## Giới hạn đã biết

- **Multi-reference kém ổn định**: nên gọi ảnh bằng `<image1>`, `<image2>` và đặt ảnh có khung hình mong muốn ở vị trí đầu tiên (kích thước ảnh ra theo image 1). Model hay nhân đôi vật thể, làm mất chi tiết nhân vật, hoặc chỉ vẽ lại image 2. Kết quả thay đổi theo seed; `--enhance` giúp được một phần (đã hết nhân đôi khiên) nhưng vẫn chưa ổn định. Bản base 20 step cũng lỗi y hệt, và model card của Viggle cũng ghi đây là giới hạn đã biết.
  **Cách nên dùng:** edit một ảnh và mô tả món đồ bằng chữ, ví dụ `"Give the knight a wooden kite shield with a golden lion emblem. Keep his armor, cape and pose unchanged." --ref knight.png`. Cách này ra kết quả tốt và ổn định ở mọi seed đã thử.
- Sprite nhỏ (32 px) mất chi tiết. Với nhân vật nên dùng 48–64 px, còn tile/cảnh thì 128–256 px.
- **Giấy phép**: Qwen-Image 2.1 cùng mọi bản phái sinh (GGUF, Viggle, Pruna) đều theo *Qwen Research License*, **chỉ cho phép dùng phi thương mại**. Muốn đưa asset vào game thương mại phải xin license riêng từ Qwen (`model-business@notice.qwencloud.com`).

## Cấu trúc

```
qis/                    service (gateway FastAPI + engine)
  engine/workflows.py   dựng workflow ComfyUI (golden test so với gen.py cũ)
  engine/ops.py         các op của pipeline         engine/comfy/   client HTTP+WS và supervisor ComfyUI
  scheduler.py          hàng đợi: admission, dispatcher, retry, phục hồi sau restart
  api/                  OpenAI, jobs/files/tools, admin, MCP, health
  comfy_nodes/          custom node đi kèm (pixelart_retro, viggle_turbo)
  data/                 presets.yaml, models.yaml (manifest + tier), comfy.lock.json, system prompt của enhancer
  setup.py doctor.py    installer (Python), service, cập nhật
web/                    dashboard React (build ra qis/web/dist)
install/                install.ps1, install.sh
gen.py, tools/          CLI (client của API) + merge_lora.py (bake LoRA → GGUF)
tests/                  pytest (fake ComfyUI, golden workflow)
```
