# Execução do pipeline COMISET no HPC Slurm

Este roteiro adapta o fluxo documentado em `~/Projects/luigi-research` para este projeto. Não use o `slurm_train.sh` daquele repositório diretamente: ele executa `uv run start`, que pertence ao pipeline Luigi.

O procedimento abaixo executa o dataset completo já materializado:

- 49.800 eventos;
- 249 segmentos, sendo 49 `lab` e 200 `real`;
- quatro modelos pequenos para filtragem;
- seis modelos grandes para classificação;
- 24 combinações entre filtro e classificador.

A matriz de famílias de modelos segue `docs/artigo_antigo.md`, mas é executada sobre o benchmark COMISET atual. Isso não reproduz o experimento histórico de 60 alertas nem suas estratégias de ensemble. Não é necessário enviar os ZIPs originais nem executar a preparação do dataset.

## 1. Conectar ao cluster

Conecte-se à VPN do Inatel e teste o acesso usando o mesmo usuário e senha da VPN:

```bash
ssh -p 22022 lucas.ruan@slurm.inatel.br
```

Depois de validar o acesso, use `exit` para voltar ao terminal local.

## 2. Enviar o projeto e o dataset processado

O `dataset/processed` ocupa aproximadamente 96 MB. Não envie os ZIPs, caches, execuções anteriores, ambiente virtual ou `dataset/lab_anchors.jsonl`.

Na máquina local:

```bash
cd ~/Projects/artigo-inatel

ssh -p 22022 lucas.ruan@slurm.inatel.br \
  "mkdir -p ~/artigo-inatel"

COPYFILE_DISABLE=1 tar -czf - \
  pyproject.toml \
  uv.lock \
  .python-version \
  README.md \
  AGENTS.md \
  scripts \
  docs \
  dataset/processed |
ssh -p 22022 lucas.ruan@slurm.inatel.br \
  "tar -xzf - -C ~/artigo-inatel && find ~/artigo-inatel -type f -name '._*' -delete"
```

Conecte-se ao servidor e confira a cópia:

```bash
ssh -p 22022 lucas.ruan@slurm.inatel.br

cd ~/artigo-inatel

du -sh dataset/processed
find dataset/processed/lab -name '*.jsonl' | wc -l
find dataset/processed/real -name 'real_*.jsonl' | wc -l
```

Resultado esperado:

```text
aproximadamente 96M
49
200
```

## 3. Preparar Python e llama.cpp com CUDA

O worker possui driver NVIDIA 575.57.08, compatível com CUDA até 12.9, mas não possui o compilador `nvcc`. O `nvcc` não é necessário para executar uma biblioteca já compilada. Portanto, use o wheel oficial CUDA 12.5 do `llama-cpp-python` 0.3.34, que é compatível com esse driver e com a H200.

Ainda no servidor:

```bash
export PATH="$HOME/.local/bin:$PATH"
cd ~/artigo-inatel

uv --version
uv python install 3.13
uv sync --frozen --no-dev

ldd --version | head -n 1

uv pip install \
  --python .venv/bin/python \
  --reinstall \
  --no-deps \
  'https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.34-cu125/llama_cpp_python-0.3.34-py3-none-manylinux_2_35_x86_64.whl'

uv pip install \
  --python .venv/bin/python \
  --no-deps \
  'nvidia-cuda-runtime-cu12==12.5.82' \
  'nvidia-cublas-cu12==12.5.3.2'
```

O wheel possui aproximadamente 1,9 GB, portanto o download e a instalação podem levar alguns minutos. Os pacotes NVIDIA fornecem `libcudart.so.12`, `libcublas.so.12` e `libcublasLt.so.12`, ausentes no worker. `--no-deps` é seguro aqui porque o `uv sync` anterior já instalou as demais dependências. O wheel exige glibc 2.35 ou superior. Não copie a `.venv` do macOS e não execute outro `uv sync` depois dessas instalações, pois isso pode restaurar o pacote vindo do PyPI. Os workers usam diretamente `.venv/bin/python`, sem depender de `uv` no nó de computação.

Confirme o backend dentro de um worker GPU:

Evite um `--wrap` longo, pois uma quebra de linha depois de `export` altera o comando. Crie um script curto:

```bash
mkdir -p logs/slurm

cat > scripts/check_llama_cuda.sh <<'EOF'
#!/usr/bin/env bash
#SBATCH --job-name=check-llama-cuda
#SBATCH --partition=gpu_35gb
#SBATCH --gres=gpu:1g.35gb:1
#SBATCH --time=00:05:00
#SBATCH --output=logs/slurm/%x-%j.out

set -euo pipefail
cd "$HOME/artigo-inatel"

VENV_PYTHON="$HOME/artigo-inatel/.venv/bin/python"
SITE_PACKAGES="$("$VENV_PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
export LD_LIBRARY_PATH="$SITE_PACKAGES/nvidia/cuda_runtime/lib:$SITE_PACKAGES/nvidia/cublas/lib:${LD_LIBRARY_PATH:-}"

exec "$VENV_PYTHON" -c 'import llama_cpp; print(llama_cpp.llama_print_system_info().decode())'
EOF

chmod +x scripts/check_llama_cuda.sh
sbatch scripts/check_llama_cuda.sh
```

Depois que o job terminar, consulte:

```bash
cat logs/slurm/check-llama-cuda-<job-id>.out
```

A saída deve mencionar `CUDA` ou `GGML_CUDA`. Não execute o benchmark completo se aparecer somente CPU.

## 4. Fixar as revisões dos modelos

Resultados finais devem utilizar commits específicos do Hugging Face, em vez de referências móveis como `main`.

O artigo histórico relaciona quatro modelos leves e seis robustos. Gere dois arquivos com referências GGUF Q4_K_M e commits específicos:

```bash
cd ~/artigo-inatel

uv run --no-sync python - <<'PY'
from pathlib import Path
from huggingface_hub import model_info

groups = {
    "models-small.lock": [
        ("bartowski/Llama-3.2-3B-Instruct-GGUF", "Llama-3.2-3B-Instruct-Q4_K_M.gguf"),
        ("bartowski/microsoft_Phi-4-mini-instruct-GGUF", "microsoft_Phi-4-mini-instruct-Q4_K_M.gguf"),
        ("TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF", "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"),
        ("bartowski/google_gemma-3-4b-it-GGUF", "google_gemma-3-4b-it-Q4_K_M.gguf"),
    ],
    "models-big.lock": [
        ("bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF", "DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf"),
        ("bartowski/phi-4-GGUF", "phi-4-Q4_K_M.gguf"),
        ("bartowski/Mistral-Nemo-Instruct-2407-GGUF", "Mistral-Nemo-Instruct-2407-Q4_K_M.gguf"),
        ("bartowski/Meta-Llama-3.1-8B-Instruct-GGUF", "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"),
        ("bartowski/google_gemma-3-12b-it-GGUF", "google_gemma-3-12b-it-Q4_K_M.gguf"),
        ("Qwen/Qwen3-14B-GGUF", "Qwen3-14B-Q4_K_M.gguf"),
    ],
}

for output, models in groups.items():
    references = []
    for repository, filename in models:
        info = model_info(repository)
        available = {item.rfilename for item in info.siblings}
        if filename not in available:
            raise SystemExit(f"Arquivo não encontrado: {repository}:{filename}")
        references.append(f"{repository}@{info.sha}:{filename}")
    Path(output).write_text("\n".join(references) + "\n", encoding="utf-8")
    print(f"{output}: {len(references)} modelos")
PY

cat models-small.lock
cat models-big.lock
```

Preserve os dois arquivos junto aos resultados. Eles registram os commits utilizados. As famílias e arquivos acima foram verificados no Hugging Face; o script resolve novamente o commit vigente no momento em que o benchmark é congelado.

Os modos `think` e `no_think` de DeepSeek-R1 e Qwen3 mencionados no artigo histórico são configurações de inferência, não modelos GGUF adicionais. A CLI usa `--thinking-mode auto|think|no_think` e grava o modo na saída e no manifesto. `/think` e `/no_think` são switches oficiais do Qwen3; no DeepSeek-R1-Distill-Qwen são diretivas experimentais de prompt, não um hard switch garantido pelo modelo.

## 5. Preparar a execução sem rede nos workers

Os workers Slurm deste cluster não acessam a internet. Portanto, os GGUFs devem ser baixados no login node para um diretório compartilhado antes de cada job. Como o `$HOME` possui limite de 50 GB, use o diretório dedicado `~/artigo-inatel/.hf-stage` somente como staging:

1. baixe os quatro modelos pequenos e execute todas as filtragens;
2. confirme os quatro checkpoints de filtragem;
3. apague apenas `.hf-stage`;
4. baixe um modelo grande, classifique as quatro saídas e aguarde o job terminar;
5. apague `.hf-stage` e repita para o próximo modelo grande.

Nunca use `$TMPDIR` para esse cache: ele pertence ao worker, começa vazio e não pode ser preenchido sem rede. Não apague `.hf-stage` enquanto um job estiver ativo.

### 5.1 Criar o script Slurm por fase

No servidor, crie um único script com os modos `filter` e `classify`:

```bash
cat > scripts/slurm_comiset.sh <<'EOF'
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
MODE="${1:?Use: filter RUN_ROOT | classify BIG_INDEX THINKING_MODE RUN_ROOT | raw BIG_INDEX THINKING_MODE RUN_ROOT}"
THINKING_MODE=auto

case "$MODE" in
    filter)
        RUN_ROOT="${2:-runs/hpc-final}"
        ;;
    classify|raw)
        BIG_INDEX="${2:?Informe o índice do modelo grande, de 0 a 5}"
        THINKING_MODE="${3:-auto}"
        if [[ "$MODE" == "raw" ]]; then
            RUN_ROOT="${4:-runs/hpc-raw}"
        else
            RUN_ROOT="${4:-runs/hpc-final}"
        fi
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
        echo "Modo inválido: $MODE. Use filter, classify ou raw." >&2
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
    --classify-n-ctx 8192
    --classify-max-output-tokens 256
    --max-tokens 5000
    --thinking-mode "$THINKING_MODE"
)

RAW_CLASSIFY_ARGS=(
    --n-ctx 8192
    --max-output-tokens 256
    --include-irrelevant
    --max-events-per-segment 1000
    --max-tokens 5000
    --thinking-mode "$THINKING_MODE"
    --run-manifest "$RUN_ROOT/run_manifest.json"
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
elif [[ "$MODE" == "classify" ]]; then
    BIG_MODEL="${BIG_MODELS[$BIG_INDEX]}"
    for small_index in "${!SMALL_MODELS[@]}"; do
        "$VENV_PYTHON" scripts/comiset_llm_pipeline.py run-dataset \
            --input-dir dataset/processed \
            --run-dir "$RUN_ROOT/${RUN_NAMES[$small_index]}" \
            --small-model "${SMALL_MODELS[$small_index]}" \
            --big-model "$BIG_MODEL" \
            "${CLASSIFY_ARGS[@]}" \
            "${COMMON_ARGS[@]}"
    done
else
    BIG_MODEL="${BIG_MODELS[$BIG_INDEX]}"
    RAW_INPUT="$RUN_ROOT/dataset_input/all_events.jsonl"
    RAW_NAME="big-${BIG_INDEX}__thinking-${THINKING_MODE}"
    RAW_OUTPUT_DIR="$RUN_ROOT/classify/$RAW_NAME"

    if [[ ! -s "$RAW_INPUT" ]]; then
        echo "Entrada raw não encontrada ou vazia: $RAW_INPUT" >&2
        echo "Prepare $RUN_ROOT/dataset_input/all_events.jsonl antes de usar o modo raw." >&2
        exit 1
    fi

    "$VENV_PYTHON" scripts/comiset_llm_pipeline.py classify \
        --input "$RAW_INPUT" \
        --output "$RAW_OUTPUT_DIR/classifications.jsonl" \
        --checkpoint "$RAW_OUTPUT_DIR/checkpoint.json" \
        --model "$BIG_MODEL" \
        "${RAW_CLASSIFY_ARGS[@]}" \
        "${COMMON_ARGS[@]}"
fi
EOF

chmod +x scripts/slurm_comiset.sh
mkdir -p logs/slurm logs/model-downloads
```

O script usa a MIG `3g.71gb` para reduzir a latência dos modelos grandes. O modo `classify` carrega somente um modelo grande e o aplica sequencialmente às quatro saídas de filtragem. A barra mostra `segment=X/249 chunk=Y/Z`; atualizações usam retorno de carro e podem ser acompanhadas com o comando `watch` da seção 8.

## 6. Executar a filtragem

### 6.1 Baixar os quatro modelos pequenos no login node

Para automatizar o download e a submissão da filtragem, use o script a partir do
login node. Ele baixa os quatro GGUFs antes de chamar o Slurm:

```bash
cd ~/artigo-inatel
chmod +x scripts/hpc_filter.sh
scripts/hpc_filter.sh runs/hpc-final
```

O script interrompe se algum download falhar. Se o job for interrompido depois do
download, mantenha `.hf-stage` e repita somente a submissão manualmente com
`sbatch scripts/slurm_comiset.sh filter runs/hpc-final`.

Ainda fora do Slurm:

```bash
cd ~/artigo-inatel
export HF_HOME="$HOME/artigo-inatel/.hf-stage"
export HF_HUB_DISABLE_XET=1

rm -rf "$HF_HOME"
mkdir -p "$HF_HOME" logs/model-downloads

{
  date
  time .venv/bin/python - <<'PY'
from pathlib import Path
from huggingface_hub import hf_hub_download

for reference in Path("models-small.lock").read_text(encoding="utf-8").splitlines():
    if not reference.strip():
        continue
    repository, filename = reference.rsplit(":", 1)
    repo_id, revision = repository.rsplit("@", 1)
    print(f"Baixando {repo_id}:{filename}", flush=True)
    path = hf_hub_download(repo_id=repo_id, filename=filename, revision=revision)
    print(f"Pronto: {path}", flush=True)
PY
  date
} 2>&1 | tee logs/model-downloads/small.log

du -sh "$HF_HOME"
```

Modelos públicos funcionam sem autenticação, mas `HF_TOKEN` pode ser exportado no login node para evitar limites reduzidos. Não grave o token no script nem nos logs.

### 6.2 Submeter a filtragem

```bash
sbatch scripts/slurm_comiset.sh filter runs/hpc-final
```

O `--resume` já está habilitado. Se o job for interrompido, mantenha os quatro modelos pequenos em `.hf-stage` e repita o mesmo comando.

### 6.3 Confirmar a conclusão antes de liberar espaço

Após o job terminar, valide os quatro checkpoints:

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path

paths = sorted(Path("runs/hpc-final").glob("*/filter/*/checkpoint.json"))
for path in paths:
    phase = json.loads(path.read_text(encoding="utf-8")).get("phase")
    print(f"{phase}: {path}")
if len(paths) != 4 or any(
    json.loads(path.read_text(encoding="utf-8")).get("phase") != "done"
    for path in paths
):
    raise SystemExit("As quatro filtragens ainda não terminaram.")
PY
```

Somente depois dessa validação, remova os GGUFs pequenos:

```bash
rm -rf "$HOME/artigo-inatel/.hf-stage"
mkdir -p "$HOME/artigo-inatel/.hf-stage"
```

Os resultados, checkpoints e métricas ficam em `runs/hpc-final` e não são removidos.

## 7. Classificar com um modelo grande por vez

Para executar os seis modelos grandes automaticamente, use o script no login node:

```bash
cd ~/artigo-inatel
chmod +x scripts/hpc_classify_all.sh
scripts/hpc_classify_all.sh 0 no_think runs/hpc-final
```

Ele baixa um modelo, submete o job com `sbatch --wait`, aguarda a conclusão,
remove apenas `~/artigo-inatel/.hf-stage` e passa ao próximo modelo. Se um job
falhar, o script para e preserva o cache do modelo que falhou; depois, retome a
partir do índice correspondente:

```bash
scripts/hpc_classify_all.sh 2 no_think runs/hpc-final
```

Como o script precisa continuar executando no login node, use `tmux`, `screen` ou
uma sessão persistente equivalente. Não remova `.hf-stage` enquanto houver um job
Slurm ativo.

Classificações produzidas antes da compactação de contexto não são comparáveis. Preserve as filtragens e arquive somente classificações antigas antes da primeira execução do código novo:

```bash
BACKUP="runs/precompact-classify-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
for run in runs/hpc-final/*; do
    if [[ -d "$run/classify" ]]; then
        mkdir -p "$BACKUP/$(basename "$run")"
        mv "$run/classify" "$BACKUP/$(basename "$run")/"
        if [[ -f "$run/run_manifest.json" ]]; then
            cp "$run/run_manifest.json" "$BACKUP/$(basename "$run")/run_manifest.json"
        fi
    fi
done

.venv/bin/python - <<'PY'
import json
from pathlib import Path

big_models = set(Path("models-big.lock").read_text(encoding="utf-8").splitlines())
for path in Path("runs/hpc-final").glob("*/run_manifest.json"):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["phases"] = {
        key: value
        for key, value in manifest.get("phases", {}).items()
        if not key.startswith("classify:")
    }
    for model in big_models:
        manifest.get("models", {}).pop(model, None)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
PY
```

Os diretórios `filter/`, `dataset_input/`, checkpoints e métricas de filtragem permanecem em `runs/hpc-final`. O backup preserva classificações e manifestos antigos; os novos diretórios e fases incluem `thinking=<modo>`.

Use índices de `0` a `5`, na ordem de `models-big.lock`. Para preparar o primeiro modelo no login node:

```bash
cd ~/artigo-inatel
export HF_HOME="$HOME/artigo-inatel/.hf-stage"
export HF_HUB_DISABLE_XET=1
export BIG_INDEX=0

rm -rf "$HF_HOME"
mkdir -p "$HF_HOME" logs/model-downloads

{
  date
  time .venv/bin/python - <<'PY'
import os
from pathlib import Path
from huggingface_hub import hf_hub_download

index = int(os.environ["BIG_INDEX"])
references = [
    line for line in Path("models-big.lock").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
reference = references[index]
repository, filename = reference.rsplit(":", 1)
repo_id, revision = repository.rsplit("@", 1)
print(f"Baixando [{index}] {repo_id}:{filename}", flush=True)
path = hf_hub_download(repo_id=repo_id, filename=filename, revision=revision)
print(f"Pronto: {path}", flush=True)
PY
  date
} 2>&1 | tee "logs/model-downloads/big-$BIG_INDEX.log"

du -sh "$HF_HOME"

# Escolha auto, think ou no_think.
export THINKING_MODE=no_think
sbatch scripts/slurm_comiset.sh classify "$BIG_INDEX" "$THINKING_MODE" runs/hpc-final
```

O modo `classify --resume` chama `classify-model` uma vez por modelo grande e modo
de raciocínio. Ele valida as quatro filtragens já concluídas, classifica o raw uma
vez e produz uma saída para cada filtro, reutilizando o mesmo modelo grande e a
mesma configuração. A classificação recebe o payload compacto, `n_ctx=8192`,
pré-divisão de 5.000 tokens com verificação de contexto e até 256 tokens de saída.

Aguarde o job chegar ao estado `COMPLETED`. Se ele for interrompido, não apague `.hf-stage`: submeta novamente o mesmo `BIG_INDEX` e o mesmo `THINKING_MODE`. Depois da conclusão, libere o cache e repita o bloco com `BIG_INDEX=1`, depois `2`, `3`, `4` e `5`. Para comparar modos, repita o mesmo índice com outro `THINKING_MODE`; os diretórios incluem `thinking-auto`, `thinking-think` ou `thinking-no_think`, evitando colisão de checkpoints.

Nunca prepare o próximo índice enquanto o job atual estiver ativo. O log `logs/model-downloads/big-N.log` registra separadamente o download feito fora do Slurm; no `run_manifest.json`, `resolve_download_seconds` medirá apenas a resolução do arquivo já presente no cache offline.

## 8. Acompanhar e retomar a execução

Jobs ativos:

```bash
squeue -u "$USER"
```

Log do Slurm e logs dos pipelines:

```bash
JOB_ID=123456
LOG="logs/slurm/comiset-stage-$JOB_ID.out"

# Polling funciona melhor que tail -f para barras com retorno de carro em filesystem compartilhado.
watch -n 1 "tail -c 65536 '$LOG' | tr '\\r' '\\n' | tail -n 10"

tail -f runs/hpc-final/*/pipeline.log
```

Histórico e recursos utilizados:

```bash
sacct -j 123456 \
  --format=JobID,JobName,Partition,State,Elapsed,AllocTRES,ExitCode
```

Para cancelar e localizar checkpoints:

```bash
scancel 123456
find runs/hpc-final -name checkpoint.json -print
```

Regras de retomada:

- filtragem interrompida: mantenha os quatro modelos pequenos no staging e execute novamente `filter`;
- classificação interrompida: mantenha o modelo grande atual e execute novamente `classify` com o mesmo índice e modo de raciocínio; cada fonte possui checkpoint independente;
- cache removido antes da conclusão: baixe novamente o mesmo conjunto ou modelo no login node;
- checkpoint `done`: o pipeline só pula a fonte quando a saída durável e sua sequência de segmentos estão válidas.

Não misture resultados quando mudar dataset, modelos, prompts, formato, contexto, chunking ou parâmetros. A compactação atual permite preservar os diretórios `filter/` já concluídos, mas classificações anteriores à compactação devem ser arquivadas. Modos de raciocínio não colidem porque fazem parte do nome do diretório e do checkpoint.

## 9. Validar a conclusão

Conte as linhas geradas:

```bash
find runs/hpc-final -name filtered_events.jsonl -exec wc -l {} +
find runs/hpc-final -name classifications.jsonl -exec wc -l {} +
```

Resultado esperado:

- quatro arquivos `filtered_events.jsonl`, cada um com 49.800 linhas;
- cinco arquivos `classifications.jsonl` para cada modelo grande/modo executado: um raw e um por filtro;
- 30 arquivos se os seis modelos forem executados em um único modo cada;
- dez arquivos adicionais para cada modo extra executado somente em DeepSeek e Qwen;
- cada `classifications.jsonl` com 249 linhas.

Localize métricas e erros operacionais:

```bash
find runs/hpc-final -name classification_metrics.json -print
find runs/hpc-final -name classification_errors.jsonl -print
du -sh runs/hpc-final .venv
```

Existem quatro manifestos, um por modelo de filtragem:

```bash
find runs/hpc-final -name run_manifest.json -print
```

Confira neles:

- referências e commits dos modelos;
- SHA-256 e quantização dos GGUFs;
- backend CUDA e hardware;
- `n_ctx`, limites de entrada/saída, modo de raciocínio, compactação, `n_gpu_layers`, `n_batch` e seed;
- tempos de resolução do cache, carga, warm-up e inferência;
- tempos de download externo em `logs/model-downloads/`;
- checksums dos artefatos de entrada e saída.

## 10. Baixar os resultados

Na máquina local:

```bash
cd ~/Projects/artigo-inatel
mkdir -p runs/from-hpc

scp -P 22022 -r \
  lucas.ruan@slurm.inatel.br:~/artigo-inatel/runs/hpc-final \
  runs/from-hpc/
```

## Limites e solução de problemas

### Espaço em disco

O `$HOME` do cluster possui limite de 50 GB e não comporta com segurança os dez GGUFs simultaneamente. O fluxo usa `~/artigo-inatel/.hf-stage`, visível no login node e nos workers, com os quatro modelos pequenos durante a filtragem ou somente um modelo grande durante a classificação.

Acompanhe o uso antes de cada download:

```bash
du -sh ~/.cache/uv ~/artigo-inatel/.venv ~/artigo-inatel/.hf-stage \
  ~/artigo-inatel/runs 2>/dev/null
```

Apague somente o staging e apenas quando nenhum job estiver usando-o:

```bash
rm -rf "$HOME/artigo-inatel/.hf-stage"
mkdir -p "$HOME/artigo-inatel/.hf-stage"
```

Não use `$TMPDIR` para os GGUFs e não envie os ZIPs originais para o cluster.

### GPU não aparece

Confira se o script contém o par correto de partição e GRES:

```text
gpu_18gb  + gpu:1g.18gb:1
gpu_35gb  + gpu:1g.35gb:1
gpu_71gb  + gpu:3g.71gb:1
```

Também confira no log se `CUDA_VISIBLE_DEVICES` não está vazio.

### Falta de memória de GPU

Se ocorrer OOM com o perfil de 35 GB, altere o script para:

```bash
#SBATCH --partition=gpu_71gb
#SBATCH --gres=gpu:3g.71gb:1
```

### Erros TLS do Hugging Face

TLS é necessário somente durante o download no login node; os workers executam com `HF_HUB_OFFLINE=1`. Se o login node apresentar erro de certificado, configure antes dos comandos de download:

```bash
CA_BUNDLE="$(.venv/bin/python -c 'import certifi; print(certifi.where())')"
export SSL_CERT_FILE="$CA_BUNDLE"
export REQUESTS_CA_BUNDLE="$CA_BUNDLE"
export CURL_CA_BUNDLE="$CA_BUNDLE"
export HF_HUB_DISABLE_XET=1
```

### Componentes desnecessários

Este fluxo não precisa de:

- DVC;
- Docker;
- Kubeflow;
- NumPy/Numba CUDA do exemplo `hello_world`;
- ZIPs originais do COMISET;
- preparação ou extração do dataset.

## 11. Baseline sem filtragem: `classify-model`

Para comparar a classificação com e sem filtragem, não é mais necessário submeter
um job raw separado. O modo `classify` chama `classify-model` sobre o diretório
HPC: ele usa `all_events.jsonl` diretamente para a baseline, passa
`--include-irrelevant` internamente e processa também todas as saídas dos filtros.

O subcomando `classify` continua disponível para depuração de uma única entrada.
O antigo fluxo manual raw permanece apenas como procedimento de recuperação quando
se deseja classificar um arquivo isolado fora da execução combinada.

O baseline ainda usa a mesma sanitização e compactação do payload de classificação,
o mesmo formato CSV, o mesmo chunking, `n_ctx=8192`, `max-tokens=5000`, seed e
limite de saída. Portanto, “sem filtragem” significa sem remover eventos na etapa
leve, e não enviar o JSON bruto completo com campos de avaliação ao LLM.

### 11.1 Preparar a entrada raw

Depois de concluir a filtragem normal, copie o `all_events.jsonl` já materializado
para a nova execução. O arquivo é igual para as quatro pastas de modelo pequeno;
use uma delas apenas como fonte:

```bash
cd ~/artigo-inatel

RAW_ROOT=runs/hpc-raw
SOURCE_ROOT=runs/hpc-final/llama-3.2-3b

mkdir -p "$RAW_ROOT/dataset_input"
cp "$SOURCE_ROOT/dataset_input/all_events.jsonl" "$RAW_ROOT/dataset_input/"
cp "$SOURCE_ROOT/dataset_input/manifest.csv" "$RAW_ROOT/dataset_input/"

wc -l "$RAW_ROOT/dataset_input/all_events.jsonl"
```

O resultado esperado é `49.800` linhas. Não copie `filtered_events.jsonl`: a
execução raw precisa receber `dataset_input/all_events.jsonl` diretamente.

### 11.2 Baixar o modelo grande no login node

Escolha o mesmo `BIG_INDEX` e `THINKING_MODE` usados na condição filtrada. O índice
segue a ordem de `models-big.lock`:

```bash
cd ~/artigo-inatel
export HF_HOME="$HOME/artigo-inatel/.hf-stage"
export HF_HUB_DISABLE_XET=1
export BIG_INDEX=0
export THINKING_MODE=no_think

rm -rf "$HF_HOME"
mkdir -p "$HF_HOME" logs/model-downloads

{
  date
  time .venv/bin/python - <<'PY'
import os
from pathlib import Path
from huggingface_hub import hf_hub_download

index = int(os.environ["BIG_INDEX"])
references = [
    line for line in Path("models-big.lock").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
reference = references[index]
repository, filename = reference.rsplit(":", 1)
repo_id, revision = repository.rsplit("@", 1)
print(f"Baixando [{index}] {repo_id}:{filename}", flush=True)
path = hf_hub_download(repo_id=repo_id, filename=filename, revision=revision)
print(f"Pronto: {path}", flush=True)
PY
  date
} 2>&1 | tee "logs/model-downloads/raw-big-$BIG_INDEX.log"

du -sh "$HF_HOME"
```

### 11.3 Saída integrada raw + filtrado

O comando abaixo é suficiente para executar a baseline e as quatro condições
filtradas no mesmo job:

```bash
sbatch scripts/slurm_comiset.sh classify "$BIG_INDEX" "$THINKING_MODE" runs/hpc-final
```

Saída esperada:

```text
runs/hpc-final/
  classify/raw/big-0__thinking-no_think/
    classifications.jsonl
  classify/<modelo-pequeno>/big-0__thinking-no_think/
    classifications.jsonl
  classify/big-0__thinking-no_think/sources.json
```

Cada `classifications.jsonl` deve ter 249 linhas. Para executar outro modelo grande,
aguarde o job terminar, remova somente `.hf-stage`, baixe o novo `BIG_INDEX` e
submeta novamente. O nome que inclui o índice e o modo de raciocínio mantém
checkpoints separados.

O raw e cada filtragem possuem métricas próprias. Compare somente entradas do mesmo
modelo grande, modo, prompt, formato, chunking e parâmetros; o custo da filtragem
fica separado do custo da classificação.

Compare os arquivos em `runs/hpc-raw` com o diretório equivalente em
`runs/hpc-final`, mantendo exatamente o mesmo modelo grande, modo, prompt, formato,
chunking e parâmetros. O tempo do baseline raw mede somente a classificação; o fluxo
filtrado possui adicionalmente a etapa de filtragem pequena.
