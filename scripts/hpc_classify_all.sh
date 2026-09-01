#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
START_INDEX="${1:-0}"
THINKING_MODE="${2:-no_think}"
RUN_ROOT="${3:-runs/hpc-final}"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
HF_HOME="$PROJECT_ROOT/.hf-stage"

cd "$PROJECT_ROOT"

[[ -x "$VENV_PYTHON" ]] || {
    echo "Ambiente Python não encontrado: $VENV_PYTHON" >&2
    exit 1
}
[[ -f models-big.lock ]] || {
    echo "Arquivo models-big.lock não encontrado." >&2
    exit 1
}
[[ "$START_INDEX" =~ ^[0-5]$ ]] || {
    echo "O índice inicial deve estar entre 0 e 5." >&2
    exit 1
}
[[ "$THINKING_MODE" =~ ^(auto|think|no_think)$ ]] || {
    echo "O modo deve ser auto, think ou no_think." >&2
    exit 1
}

mapfile -t BIG_MODELS < <(sed '/^[[:space:]]*$/d' models-big.lock)
[[ "${#BIG_MODELS[@]}" -eq 6 ]] || {
    echo "Esperados 6 modelos grandes, encontrados ${#BIG_MODELS[@]}" >&2
    exit 1
}

export HF_HOME
export HF_HUB_DISABLE_XET=1
mkdir -p logs/model-downloads

clear_model_cache() {
    [[ "$HF_HOME" == "$PROJECT_ROOT/.hf-stage" ]] || {
        echo "Recusa limpar cache fora de $PROJECT_ROOT/.hf-stage" >&2
        exit 1
    }
    rm -rf -- "$HF_HOME"
    mkdir -p "$HF_HOME"
}

download_model() {
    local index="$1"
    "$VENV_PYTHON" - "$index" <<'PY'
import sys
from pathlib import Path
from huggingface_hub import hf_hub_download

index = int(sys.argv[1])
references = [
    line.strip()
    for line in Path("models-big.lock").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
reference = references[index]
repository, filename = reference.rsplit(":", 1)
repo_id, revision = repository.rsplit("@", 1)
print(f"Baixando [{index}] {repo_id}:{filename}", flush=True)
path = hf_hub_download(repo_id=repo_id, filename=filename, revision=revision)
print(f"Pronto: {path}", flush=True)
PY
}

for ((index = START_INDEX; index < ${#BIG_MODELS[@]}; index++)); do
    clear_model_cache
    echo "=== Modelo grande $index/${#BIG_MODELS[@]} ==="
    echo "${BIG_MODELS[$index]}"
    {
        date
        time download_model "$index"
        date
    } 2>&1 | tee "logs/model-downloads/big-$index.log"

    echo "Submetendo classificação do modelo $index e aguardando o Slurm..."
    if ! sbatch --wait "$PROJECT_ROOT/scripts/slurm_comiset.sh" \
        classify "$index" "$THINKING_MODE" "$RUN_ROOT"; then
        echo "A classificação do modelo $index falhou; o cache foi preservado para retomada." >&2
        exit 1
    fi

    echo "Modelo $index concluído; liberando o cache antes do próximo."
    clear_model_cache
done

echo "Classificação concluída para os modelos grandes $START_INDEX a 5."
