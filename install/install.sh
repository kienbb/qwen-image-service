#!/usr/bin/env bash
# Qwen Image Service installer for Linux (x86_64, NVIDIA driver installed). Run as root:
#   curl -fsSL https://<host>/install.sh | sudo bash
#   curl -fsSL https://<host>/install.sh | sudo bash -s -- --tier T2 --yes --tunnel-token <token>
#
# Installs uv + Python into --root (default /opt/qis), fetches the app, then runs `qis setup`, which detects the
# GPU, lets you pick the model profile, installs ComfyUI + PyTorch + models, a systemd unit and optionally
# cloudflared. Prompts read from /dev/tty so `curl | bash` stays interactive. Safe to re-run.
set -euo pipefail

ROOT="${QIS_ROOT:-/opt/qis}"
REPO="${QIS_REPO:-https://github.com/kienbb/qwen-image-service.git}"
REF="${QIS_REF:-master}"
GITHUB_TOKEN="${QIS_GITHUB_TOKEN:-}"
SERVICE_USER="${QIS_SERVICE_USER:-qis}"
SETUP_ARGS=()
NO_SERVICE=0

usage() {
  sed -n '2,8p' "$0" 2>/dev/null || true
  cat <<'EOF'
Options: --root DIR  --tier T0..T5  --variant recommended|quality|fast|custom  --port N  --comfy-port N
         --models-dir DIR  --reuse-models DIR  --admin-user U  --admin-email E  --admin-password P
         --tunnel-token T  --public-url URL  --hf-token T  --torch-index cu130|cu128|cu126
         --repo URL  --ref REF  --github-token T  --service-user U  --lan  --yes  --no-service
         --skip-models  --skip-benchmark  --force
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) ROOT="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --github-token) GITHUB_TOKEN="$2"; shift 2 ;;
    --service-user) SERVICE_USER="$2"; shift 2 ;;
    --no-service) NO_SERVICE=1; SETUP_ARGS+=("$1"); shift ;;
    --lan|--yes|-y|--skip-models|--skip-benchmark|--force) SETUP_ARGS+=("$1"); shift ;;
    --tier|--variant|--port|--comfy-port|--models-dir|--reuse-models|--admin-user|--admin-email|--admin-password|\
    --tunnel-token|--public-url|--hf-token|--torch-index) SETUP_ARGS+=("$1" "$2"); shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option $1"; usage; exit 2 ;;
  esac
done
[[ -n "${QIS_TIER:-}" ]] && SETUP_ARGS+=(--tier "$QIS_TIER")
[[ -n "${QIS_TUNNEL_TOKEN:-}" ]] && SETUP_ARGS+=(--tunnel-token "$QIS_TUNNEL_TOKEN")
[[ -n "${QIS_ADMIN_PASSWORD:-}" ]] && SETUP_ARGS+=(--admin-password "$QIS_ADMIN_PASSWORD")
[[ -n "${HF_TOKEN:-}" ]] && SETUP_ARGS+=(--hf-token "$HF_TOKEN")
[[ -n "${QIS_YES:-}" ]] && SETUP_ARGS+=(--yes)

step() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    \033[33m! %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mERROR: %s\033[0m\n' "$*"; exit 1; }

echo "Qwen Image Service installer -> $ROOT"

step "Preflight"
[[ "$(uname -s)" == "Linux" ]] || fail "this script is for Linux; use install.ps1 on Windows"
[[ "$(uname -m)" == "x86_64" ]] || fail "x86_64 is required"
if [[ $EUID -ne 0 && $NO_SERVICE -eq 0 ]]; then fail "run as root (sudo) to install the systemd service, or pass --no-service"; fi
for c in curl tar; do command -v "$c" >/dev/null || fail "$c is required"; done
if command -v nvidia-smi >/dev/null; then
  info "GPU: $(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader | head -1)"
else
  warn "nvidia-smi not found: install the NVIDIA driver first; only the tools-only profile (T0) will be offered"
fi
mkdir -p "$ROOT/bin"
free_gb=$(df -BG --output=avail "$ROOT" | tail -1 | tr -dc '0-9')
info "free space: ${free_gb} GB"

step "uv (Python manager)"
export UV_INSTALL_DIR="$ROOT/bin" UV_NO_MODIFY_PATH=1 UV_PYTHON_INSTALL_DIR="$ROOT/python" UV_CACHE_DIR="$ROOT/cache/uv"
if [[ ! -x "$ROOT/bin/uv" ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$ROOT/bin/uv"
info "$("$UV" --version)"

step "Application ($REF)"
APP="$ROOT/app"
AUTH_REPO="$REPO"
if [[ -n "$GITHUB_TOKEN" && "$REPO" == https://github.com/* ]]; then AUTH_REPO="${REPO/https:\/\//https://x-access-token:$GITHUB_TOKEN@}"; fi
if command -v git >/dev/null; then
  if [[ -d "$APP/.git" ]]; then
    git -C "$APP" fetch --tags origin -q
    git -C "$APP" checkout -q --force "$REF"
    git -C "$APP" reset -q --hard "origin/$REF" 2>/dev/null || true
  else
    git clone -q --branch "$REF" "$AUTH_REPO" "$APP" || fail "git clone failed (private repo? pass --github-token)"
    git -C "$APP" remote set-url origin "$REPO"
  fi
else
  slug="${REPO#https://github.com/}"; slug="${slug%.git}"
  hdr=(-H "User-Agent: qis-installer"); [[ -n "$GITHUB_TOKEN" ]] && hdr+=(-H "Authorization: Bearer $GITHUB_TOKEN")
  rm -rf "$APP" && mkdir -p "$APP"
  curl -fsSL "${hdr[@]}" "https://api.github.com/repos/$slug/tarball/$REF" | tar xz -C "$APP" --strip-components=1
  warn "installed from a tarball: install git to use 'qis update'"
fi

step "Python environment"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  "$UV" python install 3.13
  "$UV" venv --python 3.13 "$ROOT/.venv"
fi
"$UV" pip install --python "$ROOT/.venv/bin/python" -e "$APP"

if [[ $NO_SERVICE -eq 0 && "$SERVICE_USER" != "root" ]]; then
  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$ROOT" --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
  for g in video render; do getent group "$g" >/dev/null && usermod -aG "$g" "$SERVICE_USER"; done
  SETUP_ARGS+=(--service-user "$SERVICE_USER")
fi

step "qis setup"
export PYTHONIOENCODING=utf-8
if [[ -r /dev/tty ]] && [[ -t 1 || -t 2 ]]; then
  "$ROOT/.venv/bin/qis" setup --root "$ROOT" "${SETUP_ARGS[@]}" < /dev/tty
else
  "$ROOT/.venv/bin/qis" setup --root "$ROOT" --yes "${SETUP_ARGS[@]}"
fi
