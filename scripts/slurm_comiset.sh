#!/usr/bin/env bash
#SBATCH --job-name=comiset-stage
#SBATCH --partition=gpu_71gb
#SBATCH --gres=gpu:3g.71gb:1
#SBATCH --time=24:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --output=logs/slurm/%x-%j.out

set -euo pipefail

PROJECT_ROOT="${SLURM_SUBMIT_DIR:?Execute sbatch a partir do projeto}"
MODE="${1:?Use: filter RUN_ROOT | classify BIG_INDEX THINKING_MODE RUN_ROOT}"
THINKING_MODE=auto

case "$MODE" in
    filter)
        RUN_ROOT="${2:-runs/hpc-final}"
        ;;
    classify)
        BIG_INDEX="${2:?Informe o índice do modelo grande, de 0 a 5}"
        THINKING_MODE="${3:-auto}"
        RUN_ROOT="${4:-runs/hpc-final}"
        if [[ ! "$BIG_INDEX" =~ ^[0-5]$ ]]; then
            echo "BIG_INDEX deve estar entre 0 e 5." >&2
            exit 1
        fi
        if [[ ! "$THINKING_MODE" =~ ^(auto|think|no_think)$ ]]; then
            echo "THINKING_MODE deve ser auto, think ou no_think." >&2
            exit 1
        fi
        ;;
    *)
        echo "Modo inválido: $MODE. Use filter ou classify." >&2
        exit 1
        ;;
esac

cd "$PROJECT_ROOT"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Python do ambiente não encontrado: $VENV_PYTHON" >&2
    exit 1
fi

SITE_PACKAGES="$("$VENV_PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
CUDA_RUNTIME_LIB="$SITE_PACKAGES/nvidia/cuda_runtime/lib"
CUBLAS_LIB="$SITE_PACKAGES/nvidia/cublas/lib"

if [[ ! -d "$CUDA_RUNTIME_LIB" || ! -d "$CUBLAS_LIB" ]]; then
    echo "Bibliotecas CUDA Python não encontradas na .venv." >&2
    exit 1
fi

export LD_LIBRARY_PATH="$CUDA_RUNTIME_LIB:$CUBLAS_LIB:${LD_LIBRARY_PATH:-}"
export HF_HOME="$PROJECT_ROOT/.hf-stage"
export HF_HUB_OFFLINE=1
export HF_HUB_DISABLE_XET=1

if [[ ! -d "$HF_HOME" ]]; then
    echo "Cache compartilhado não encontrado: $HF_HOME" >&2
    exit 1
fi

mkdir -p logs/slurm
echo "=== COMISET: $MODE ==="
date
hostname
echo "HF_HOME=$HF_HOME"
echo "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-}"
nvidia-smi || true

if [[ -z "${CUDA_VISIBLE_DEVICES:-}" ]]; then
    echo "Slurm não disponibilizou CUDA_VISIBLE_DEVICES." >&2
    exit 1
fi

SYSTEM_INFO="$("$VENV_PYTHON" -c \
  'import llama_cpp; print(llama_cpp.llama_print_system_info().decode())')"
echo "$SYSTEM_INFO"

if ! grep -Eqi 'CUDA|GGML_CUDA' <<<"$SYSTEM_INFO"; then
    echo "llama-cpp-python não foi compilado com CUDA." >&2
    exit 1
fi

mapfile -t SMALL_MODELS < models-small.lock
mapfile -t BIG_MODELS < models-big.lock
RUN_NAMES=(llama-3.2-3b phi-4-mini-3.8b tinyllama-1.1b gemma-3-4b)

if [[ "${#SMALL_MODELS[@]}" -ne 4 || "${#BIG_MODELS[@]}" -ne 6 ]]; then
    echo "Esperados quatro modelos pequenos e seis modelos grandes." >&2
    exit 1
fi

mkdir -p "$RUN_ROOT"
cp models-small.lock models-big.lock "$RUN_ROOT/"

COMMON_ARGS=(
    --n-gpu-layers -1
    --n-batch 512
    --seed 2026
    --warmup-runs 1
    --inference-runs 1
    --prompt-format csv
    --resume
)

FILTER_ARGS=(
    --n-ctx 4096
    --max-output-tokens 512
)

CLASSIFY_ARGS=(
    --n-ctx 8192
    --max-output-tokens 256
    --max-tokens 5000
    --thinking-mode "$THINKING_MODE"
)

if [[ "$MODE" == "filter" ]]; then
    for index in "${!SMALL_MODELS[@]}"; do
        "$VENV_PYTHON" scripts/comiset_llm_pipeline.py filter-dataset \
            --input-dir dataset/processed \
            --run-dir "$RUN_ROOT/${RUN_NAMES[$index]}" \
            --model "${SMALL_MODELS[$index]}" \
            "${FILTER_ARGS[@]}" \
            "${COMMON_ARGS[@]}"
    done
else
    BIG_MODEL="${BIG_MODELS[$BIG_INDEX]}"
    "$VENV_PYTHON" scripts/comiset_llm_pipeline.py classify-model \
        --input-dir dataset/processed \
        --run-dir "$RUN_ROOT" \
        --big-model "$BIG_MODEL" \
        "${CLASSIFY_ARGS[@]}" \
        "${COMMON_ARGS[@]}"
fi
