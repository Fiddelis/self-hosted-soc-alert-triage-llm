#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ROOT="${1:-runs/hpc-final}"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
HF_HOME="$PROJECT_ROOT/.hf-stage"

cd "$PROJECT_ROOT"

[[ -x "$VENV_PYTHON" ]] || {
    echo "Ambiente Python não encontrado: $VENV_PYTHON" >&2
    exit 1
}
[[ -f models-small.lock ]] || {
    echo "Arquivo models-small.lock não encontrado." >&2
    exit 1
}

export HF_HOME
export HF_HUB_DISABLE_XET=1
mkdir -p "$HF_HOME" logs/model-downloads

echo "Baixando os modelos pequenos para $HF_HOME"
{
    date
    time "$VENV_PYTHON" - <<'PY'
from pathlib import Path
from huggingface_hub import hf_hub_download

references = [
    line.strip()
    for line in Path("models-small.lock").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
if len(references) != 4:
    raise SystemExit(f"Esperados 4 modelos pequenos, encontrados {len(references)}")

for reference in references:
    repository, filename = reference.rsplit(":", 1)
    repo_id, revision = repository.rsplit("@", 1)
    print(f"Baixando {repo_id}:{filename}", flush=True)
    path = hf_hub_download(repo_id=repo_id, filename=filename, revision=revision)
    print(f"Pronto: {path}", flush=True)
PY
    date
} 2>&1 | tee "logs/model-downloads/small.log"

echo "Submetendo a filtragem em $RUN_ROOT"
sbatch "$PROJECT_ROOT/scripts/slurm_comiset.sh" filter "$RUN_ROOT"
