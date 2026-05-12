#!/usr/bin/env bash
# Boardman: hardware-aware Ollama model recommendations (logic aligned with
# deepiri-platform/diri-cyrex/scripts/llm/check-ollama-models.sh) plus install check.
#
# Usage:
#   ./scripts/check-ollama-models.sh              # hardware summary + recommendations + verify LLM_MODEL
#   ./scripts/check-ollama-models.sh --pull       # pull LLM_MODEL if missing
#   ./scripts/check-ollama-models.sh --pull MODEL
#   ./scripts/check-ollama-models.sh --hw-only    # only detection + model table (no docker/HTTP check)
#
# Env: OLLAMA_HOST (default http://127.0.0.1:11434) when not using Docker.

set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

DEFAULT_CONTAINER="deepiri-ollama-boardman"
DEFAULT_MODEL="llama3:8b"
OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"

PULL=false
PULL_ARG=""
HW_ONLY=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pull)
      PULL=true
      shift
      if [[ $# -gt 0 && "$1" != --* ]]; then
        PULL_ARG="$1"
        shift
      fi
      ;;
    --hw-only)
      HW_ONLY=true
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Boardman Ollama: hardware-aware model hints + LLM_MODEL check.
  ./scripts/check-ollama-models.sh
  ./scripts/check-ollama-models.sh --pull
  ./scripts/check-ollama-models.sh --pull <model>
  ./scripts/check-ollama-models.sh --hw-only
Env: OLLAMA_HOST when not using Docker.
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

# --- Copied / aligned with cyrex check-ollama-models.sh (categorize_model + RAM/VRAM helpers) ---

detect_system_ram() {
  if [ "$OS_TYPE" = "macos" ]; then
    RAM_BYTES=$(sysctl -n hw.memsize 2>/dev/null || echo "0")
    SYSTEM_RAM_GB=$((RAM_BYTES / 1024 / 1024 / 1024))
  elif [ "$OS_TYPE" = "linux" ]; then
    RAM_KB=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}' || echo "0")
    SYSTEM_RAM_GB=$((RAM_KB / 1024 / 1024))
  else
    SYSTEM_RAM_GB=0
  fi
  if [ "$SYSTEM_RAM_GB" -lt 1 ]; then
    SYSTEM_RAM_GB=0
  fi
}

detect_gpu_vram() {
  GPU_VRAM_GB=0
  if [ "$HAS_NVIDIA_GPU" = true ] && command_exists nvidia-smi; then
    VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -n 1 | awk '{print $1}' | tr -d ' ' || echo "0")
    if [ -n "$VRAM_MB" ] && [ "$VRAM_MB" != "0" ] && [ "$VRAM_MB" != "" ]; then
      GPU_VRAM_GB=$((VRAM_MB / 1024))
    fi
  elif [ "$HAS_APPLE_SILICON" = true ]; then
    GPU_VRAM_GB=$SYSTEM_RAM_GB
  fi
  if [ "$GPU_VRAM_GB" -lt 1 ]; then
    GPU_VRAM_GB=0
  fi
}

# Returns: recommended | usable | marginal | no
categorize_model() {
  local model_name=$1
  local ram_gb=$2
  local vram_gb=$3
  local setup="unknown"
  if { [ "$ram_gb" -ge 32 ] || [ "$ram_gb" -ge 30 ]; } && { [ "$vram_gb" -ge 16 ] || [ "$vram_gb" -ge 15 ]; }; then
    setup="setup5"
  elif [ "$ram_gb" -ge 32 ] && [ "$vram_gb" -ge 10 ]; then
    setup="setup4"
  elif [ "$ram_gb" -ge 32 ] && [ "$vram_gb" -ge 8 ]; then
    setup="setup3"
  elif [ "$vram_gb" -ge 15 ]; then
    setup="setup5"
  elif [ "$ram_gb" -ge 16 ] && [ "$vram_gb" -ge 10 ]; then
    setup="setup2"
  elif [ "$ram_gb" -ge 16 ] && [ "$vram_gb" -ge 8 ]; then
    setup="setup1"
  elif [ "$ram_gb" -ge 16 ] || [ "$vram_gb" -ge 8 ]; then
    setup="basic"
  else
    setup="minimal"
  fi

  case "$model_name" in
    "llama3.2:1b"|"llama3.2:3b"|"gemma2:2b"|"phi3:mini")
      if [ "$ram_gb" -ge 8 ]; then
        echo "recommended"
      else
        echo "usable"
      fi
      ;;
    "mistral:7b"|"neural-chat:7b"|"qwen2.5:7b"|"gemma:7b"|"yi:6b"|"openchat:7b"|"zephyr:7b"|"nous-hermes:7b"|"mythomax:7b"|"dolphin-mistral:7b"|"orca-mini:7b")
      if [ "$setup" = "setup5" ] || [ "$setup" = "setup4" ] || [ "$setup" = "setup3" ] || [ "$setup" = "setup2" ]; then
        echo "recommended"
      elif [ "$vram_gb" -ge 8 ] && [ "$ram_gb" -ge 16 ]; then
        echo "recommended"
      elif [ "$vram_gb" -ge 8 ] || [ "$ram_gb" -ge 16 ]; then
        echo "usable"
      elif [ "$ram_gb" -ge 8 ]; then
        echo "marginal"
      else
        echo "no"
      fi
      ;;
    "llama3:8b"|"llama3.1:8b")
      if [ "$setup" = "setup5" ] || [ "$setup" = "setup4" ] || [ "$setup" = "setup3" ] || [ "$setup" = "setup2" ]; then
        echo "recommended"
      elif [ "$setup" = "setup1" ]; then
        echo "usable"
      elif [ "$ram_gb" -ge 16 ]; then
        echo "marginal"
      else
        echo "no"
      fi
      ;;
    "gemma2:9b"|"yi:9b")
      if [ "$setup" = "setup5" ] || [ "$setup" = "setup4" ] || [ "$setup" = "setup3" ] || [ "$setup" = "setup2" ]; then
        echo "recommended"
      elif [ "$setup" = "setup1" ]; then
        echo "usable"
      elif [ "$ram_gb" -ge 32 ]; then
        echo "marginal"
      else
        echo "no"
      fi
      ;;
    "mistral-nemo:12b"|"falcon:11b")
      if [ "$setup" = "setup5" ] || [ "$setup" = "setup4" ] || [ "$setup" = "setup3" ] || [ "$setup" = "setup2" ]; then
        echo "recommended"
      elif [ "$ram_gb" -ge 32 ] && [ "$vram_gb" -ge 8 ]; then
        echo "usable"
      else
        echo "marginal"
      fi
      ;;
    "vicuna:13b"|"openhermes:13b")
      if [ "$setup" = "setup5" ] || [ "$setup" = "setup4" ] || [ "$setup" = "setup3" ]; then
        echo "recommended"
      elif [ "$ram_gb" -ge 32 ]; then
        echo "usable"
      else
        echo "marginal"
      fi
      ;;
    "gemma2:27b")
      if [ "$setup" = "setup5" ]; then
        echo "recommended"
      elif [ "$ram_gb" -ge 32 ] && [ "$vram_gb" -ge 10 ]; then
        echo "marginal"
      else
        echo "no"
      fi
      ;;
    "mixtral:8x7b")
      if [ "$setup" = "setup5" ] || [ "$setup" = "setup4" ]; then
        echo "marginal"
      else
        echo "no"
      fi
      ;;
    "llama3.1:70b")
      if [ "$vram_gb" -ge 48 ]; then
        echo "marginal"
      else
        echo "no"
      fi
      ;;
    "codellama:7b"|"deepseek-coder:6.7b"|"qwen2.5-coder:7b"|"starcoder2:7b"|"wizardcoder:7b")
      if [ "$setup" = "setup5" ] || [ "$setup" = "setup4" ] || [ "$setup" = "setup3" ] || [ "$setup" = "setup2" ]; then
        echo "recommended"
      elif [ "$vram_gb" -ge 8 ] && [ "$ram_gb" -ge 16 ]; then
        echo "recommended"
      elif [ "$vram_gb" -ge 8 ] || [ "$ram_gb" -ge 16 ]; then
        echo "usable"
      else
        echo "marginal"
      fi
      ;;
    "codellama:13b"|"wizardcoder:13b")
      if [ "$setup" = "setup5" ] || [ "$setup" = "setup4" ] || [ "$setup" = "setup3" ] || [ "$setup" = "setup2" ]; then
        echo "recommended"
      elif [ "$ram_gb" -ge 32 ]; then
        echo "usable"
      else
        echo "marginal"
      fi
      ;;
    "phi3:medium")
      if [ "$setup" = "setup5" ] || [ "$setup" = "setup4" ] || [ "$setup" = "setup3" ] || [ "$setup" = "setup2" ]; then
        echo "usable"
      elif [ "$setup" = "setup1" ]; then
        echo "usable"
      elif [ "$ram_gb" -ge 32 ]; then
        echo "marginal"
      else
        echo "no"
      fi
      ;;
    *)
      if [ "$setup" = "setup5" ]; then
        echo "recommended"
      elif [ "$setup" = "setup4" ] || [ "$setup" = "setup3" ]; then
        echo "usable"
      elif [ "$vram_gb" -ge 8 ] && [ "$ram_gb" -ge 16 ]; then
        echo "usable"
      else
        echo "marginal"
      fi
      ;;
  esac
}

# Curated catalog (model|approx size|note) -- same family as cyrex; Boardman default called out
MODEL_LIST=(
  "llama3:8b|~4.7GB|Boardman default in docker-compose / .env.example"
  "llama3.1:8b|~4.7GB|Newer Llama 3.1 8B"
  "mistral:7b|~4.1GB|Strong general 7B"
  "llama3.2:1b|~1.3GB|Smallest, fastest"
  "llama3.2:3b|~2.0GB|Small balanced"
  "llama3.1:70b|~40GB|Large (48GB+ VRAM only)"
  "mistral-nemo:12b|~7.0GB|Mistral 12B-class"
  "mixtral:8x7b|~26GB|MoE, heavy"
  "gemma2:2b|~1.4GB|Small Google"
  "gemma2:9b|~5.4GB|Mid-size Gemma 2"
  "gemma2:27b|~16GB|Large Gemma 2"
  "gemma:7b|~4.6GB|Gemma 7B"
  "phi3:mini|~2.3GB|Small Phi-3"
  "phi3:medium|~7.0GB|Phi-3 medium"
  "codellama:7b|~3.8GB|Code 7B"
  "codellama:13b|~7.3GB|Code 13B"
  "deepseek-coder:6.7b|~4.1GB|Code-focused"
  "qwen2.5:7b|~4.4GB|Qwen 7B"
  "qwen2.5-coder:7b|~4.4GB|Qwen coder 7B"
  "neural-chat:7b|~4.1GB|Conversational"
  "yi:6b|~3.8GB|Yi 6B"
  "yi:9b|~5.4GB|Yi 9B"
  "openchat:7b|~4.1GB|OpenChat"
  "zephyr:7b|~4.1GB|Zephyr"
  "nous-hermes:7b|~4.1GB|Nous Hermes"
  "mythomax:7b|~4.1GB|MythoMax"
  "dolphin-mistral:7b|~4.1GB|Dolphin Mistral"
  "orca-mini:7b|~4.1GB|Orca Mini"
  "vicuna:13b|~7.3GB|Vicuna 13B"
  "falcon:11b|~6.0GB|Falcon 11B"
  "openhermes:13b|~7.3GB|OpenHermes 13B"
  "starcoder2:7b|~4.1GB|StarCoder2"
  "wizardcoder:7b|~4.1GB|WizardCoder 7B"
  "wizardcoder:13b|~7.3GB|WizardCoder 13B"
)

detect_hardware() {
  OS_TYPE="unknown"
  if [[ "${OSTYPE:-}" == darwin* ]]; then
    OS_TYPE="macos"
  elif [[ "${OSTYPE:-}" == linux-gnu* ]] || grep -qEi "(Microsoft|WSL)" /proc/version 2>/dev/null; then
    OS_TYPE="linux"
  fi

  HAS_NVIDIA_GPU=false
  HAS_APPLE_SILICON=false
  HAS_CPU_ONLY=false
  SYSTEM_RAM_GB=0
  GPU_VRAM_GB=0
  SETUP_CATEGORY="unknown"

  if [ "$OS_TYPE" = "macos" ]; then
    if sysctl -n machdep.cpu.brand_string 2>/dev/null | grep -qi "Apple"; then
      HAS_APPLE_SILICON=true
    else
      HAS_CPU_ONLY=true
    fi
  fi

  if [ "$OS_TYPE" = "linux" ]; then
    if command_exists nvidia-smi && nvidia-smi >/dev/null 2>&1; then
      HAS_NVIDIA_GPU=true
    elif command_exists lspci && [ "$(lspci 2>/dev/null | grep -ci nvidia || true)" -gt 0 ]; then
      HAS_NVIDIA_GPU=true
    fi
  fi

  if [ "$HAS_NVIDIA_GPU" = false ] && [ "$HAS_APPLE_SILICON" = false ] && [ "$OS_TYPE" != "macos" ]; then
    HAS_CPU_ONLY=true
  fi

  detect_system_ram
  detect_gpu_vram

  if { [ "$SYSTEM_RAM_GB" -ge 32 ] || [ "$SYSTEM_RAM_GB" -ge 30 ]; } && { [ "$GPU_VRAM_GB" -ge 16 ] || [ "$GPU_VRAM_GB" -ge 15 ]; }; then
    SETUP_CATEGORY="setup5"
  elif [ "$SYSTEM_RAM_GB" -ge 32 ] && [ "$GPU_VRAM_GB" -ge 10 ]; then
    SETUP_CATEGORY="setup4"
  elif [ "$SYSTEM_RAM_GB" -ge 32 ] && [ "$GPU_VRAM_GB" -ge 8 ]; then
    SETUP_CATEGORY="setup3"
  elif [ "$GPU_VRAM_GB" -ge 15 ]; then
    SETUP_CATEGORY="setup5"
  elif [ "$SYSTEM_RAM_GB" -ge 16 ] && [ "$GPU_VRAM_GB" -ge 10 ]; then
    SETUP_CATEGORY="setup2"
  elif [ "$SYSTEM_RAM_GB" -ge 16 ] && [ "$GPU_VRAM_GB" -ge 8 ]; then
    SETUP_CATEGORY="setup1"
  elif [ "$SYSTEM_RAM_GB" -ge 16 ] || [ "$GPU_VRAM_GB" -ge 8 ]; then
    SETUP_CATEGORY="basic"
  else
    SETUP_CATEGORY="minimal"
  fi
}

print_hardware_summary() {
  echo "Hardware detection (for model fit -- same tiers as cyrex script)"
  echo "================================================================"
  echo "OS: $OS_TYPE"
  if [ "$HAS_NVIDIA_GPU" = true ]; then
    echo "GPU: NVIDIA (nvidia-smi or PCI)"
    if command_exists nvidia-smi; then
      nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -n 3 | sed 's/^/  /' || true
    fi
  elif [ "$HAS_APPLE_SILICON" = true ]; then
    echo "GPU: Apple Silicon (unified memory ~= system RAM for sizing)"
  elif [ "$HAS_CPU_ONLY" = true ]; then
    echo "GPU: CPU-only or undetected (Ollama still runs, slower)"
  fi
  echo "System RAM (approx): ${SYSTEM_RAM_GB} GB"
  echo "VRAM / unified memory estimate: ${GPU_VRAM_GB} GB"
  echo "Setup tier: $SETUP_CATEGORY (setup5=best, minimal=tightest)"
  echo ""
}

rating_word() {
  case "$1" in
    recommended) echo "best fit" ;;
    usable) echo "ok" ;;
    marginal) echo "tight / slow" ;;
    no) echo "not recommended" ;;
    *) echo "$1" ;;
  esac
}

print_model_recommendations() {
  local want="$1"
  echo "Model catalog vs your hardware (categorize_model -- cyrex-aligned)"
  echo "=================================================================="
  printf "%-26s %-10s %-18s %s\n" "MODEL" "~SIZE" "FIT" "NOTES"
  printf "%-26s %-10s %-18s %s\n" "-------------------------" "----------" "------------------" "-----"

  # macOS ships bash 3.2 (no associative arrays), so dedupe with a delimiter string.
  local seen_models="|"
  local line model_name size_note desc cat word mark
  for line in "${MODEL_LIST[@]}"; do
    IFS='|' read -r model_name size_note desc <<< "$line"
    if [[ "$seen_models" == *"|$model_name|"* ]]; then
      continue
    fi
    seen_models="${seen_models}${model_name}|"
    cat="$(categorize_model "$model_name" "$SYSTEM_RAM_GB" "$GPU_VRAM_GB")"
    word="$(rating_word "$cat")"
    mark=""
    if [[ -n "$want" && "$model_name" == "$want" ]]; then
      mark="  <- LLM_MODEL"
    fi
    printf "%-26s %-10s %-18s %s%s\n" "$model_name" "$size_note" "$word ($cat)" "$desc" "$mark"
  done
  echo ""

  if [[ -n "$want" ]]; then
    local wcat
    wcat="$(categorize_model "$want" "$SYSTEM_RAM_GB" "$GPU_VRAM_GB")"
    echo "Your configured LLM_MODEL: $want"
    echo "  Fit on this machine: $(rating_word "$wcat") ($wcat)"
    if [ "$wcat" = "marginal" ] || [ "$wcat" = "no" ]; then
      echo "  Consider a smaller model (e.g. llama3.2:3b, phi3:mini) or more RAM/VRAM."
    fi
    echo ""
  fi
}

# --- .env LLM_MODEL ---

llm_from_env() {
  local f="$ROOT/.env"
  [[ -f "$f" ]] || return 0
  local line
  line="$(grep -E '^[[:space:]]*LLM_MODEL=' "$f" | tail -n 1 || true)"
  [[ -n "$line" ]] || return 0
  local v="${line#*=}"
  v="${v%\"}"
  v="${v#\"}"
  v="${v%\'}"
  v="${v#\'}"
  echo "$v"
}

WANTED="${PULL_ARG:-}"
if [[ -z "$WANTED" ]]; then
  WANTED="$(llm_from_env)"
fi
WANTED="${WANTED:-$DEFAULT_MODEL}"
WANTED="$(printf '%s' "$WANTED" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;s/\r$//')"

detect_hardware
print_hardware_summary
print_model_recommendations "$WANTED"

if [ "$HW_ONLY" = true ]; then
  exit 0
fi

echo "Ollama install check"
echo "--------------------"
echo "Expected model: $WANTED"
echo ""

resolve_container() {
  local name="$1"
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$name"; then
    echo "$name"
    return 0
  fi
  local alt
  alt="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -i ollama | head -n 1 || true)"
  if [[ -n "$alt" ]]; then
    echo "Note: container '$name' not running; using '$alt'." >&2
    echo "$alt"
    return 0
  fi
  return 1
}

list_models_docker() {
  local c="$1"
  docker exec "$c" ollama list 2>/dev/null || true
}

list_models_http() {
  curl -sfS "${OLLAMA_HOST%/}/api/tags" 2>/dev/null | \
    python3 -c 'import json,sys
d=json.load(sys.stdin)
for m in d.get("models") or []:
    n=m.get("name") or m.get("model")
    if n: print(n)
' 2>/dev/null || true
}

model_in_list() {
  local want="$1"
  local list_out="$2"
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    local first="${line%%[[:space:]]*}"
    [[ "$first" == "NAME" ]] && continue
    if [[ "$first" == "$want" ]]; then
      return 0
    fi
  done <<< "$list_out"
  return 1
}

model_in_names() {
  local want="$1"
  local names="$2"
  while IFS= read -r n; do
    [[ "$n" == "$want" ]] && return 0
  done <<< "$names"
  return 1
}

CONTAINER=""
if command_exists docker; then
  CONTAINER="$(resolve_container "$DEFAULT_CONTAINER" || true)"
fi

LIST_TEXT=""
MODE=""

if [[ -n "$CONTAINER" ]]; then
  MODE="docker:$CONTAINER"
  echo "Endpoint: docker exec $CONTAINER"
  LIST_TEXT="$(list_models_docker "$CONTAINER")"
else
  echo "No boardman Ollama container ($DEFAULT_CONTAINER). Trying $OLLAMA_HOST ..."
  if curl -sfS "${OLLAMA_HOST%/}/api/tags" >/dev/null 2>&1; then
    MODE="http"
    echo "Endpoint: $OLLAMA_HOST"
    LIST_TEXT="$(list_models_http)"
  else
    echo "ERROR: Ollama not reachable (docker compose up -d ollama, or ollama serve)." >&2
    exit 1
  fi
fi

echo ""
echo "Installed models:"
if [[ "$MODE" == http* ]]; then
  if [[ -z "$(echo "$LIST_TEXT" | tr -d '[:space:]')" ]]; then
    echo "  (none reported)"
  else
    echo "$LIST_TEXT" | sed 's/^/  /'
  fi
else
  echo "$LIST_TEXT" | sed 's/^/  /'
fi
echo ""

FOUND=1
if [[ "$MODE" == http* ]]; then
  model_in_names "$WANTED" "$LIST_TEXT" && FOUND=0 || true
else
  model_in_list "$WANTED" "$LIST_TEXT" && FOUND=0 || true
fi

if [[ "$FOUND" -eq 0 ]]; then
  echo "OK: '$WANTED' is installed."
  exit 0
fi

echo "MISSING: '$WANTED' is not installed."
if [[ "$PULL" != true ]]; then
  echo ""
  echo "Pull:"
  [[ -n "$CONTAINER" ]] && echo "  docker exec -it $CONTAINER ollama pull $WANTED"
  echo "  ollama pull $WANTED"
  echo "Or: ./scripts/check-ollama-models.sh --pull"
  exit 1
fi

echo ""
echo "Pulling $WANTED ..."
if [[ -n "$CONTAINER" ]]; then
  docker exec "$CONTAINER" ollama pull "$WANTED"
else
  if command_exists ollama; then
    ollama pull "$WANTED"
  else
    echo "ERROR: no container and no ollama CLI." >&2
    exit 1
  fi
fi

echo ""
if [[ -n "$CONTAINER" ]]; then
  LIST_TEXT="$(list_models_docker "$CONTAINER")"
  if model_in_list "$WANTED" "$LIST_TEXT"; then
    echo "OK: '$WANTED' is now available."
    exit 0
  fi
else
  NAMES_HTTP="$(list_models_http)"
  if model_in_names "$WANTED" "$NAMES_HTTP"; then
    echo "OK: '$WANTED' is now available."
    exit 0
  fi
fi

echo "ERROR: pull finished but '$WANTED' still not listed." >&2
exit 1
