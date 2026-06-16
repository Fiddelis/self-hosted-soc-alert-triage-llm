# COMISET Dataset Materializado + LLM Pipeline

Este projeto prepara um dataset pequeno e fixo a partir dos ZIPs do COMISET e roda os
testes com Ollama sobre esses arquivos materializados. O fluxo recomendado é:

```text
dataset/lab.zip + dataset/lab_anchors.jsonl -> dataset/anchors.best.jsonl -> dataset/processed/lab/*.jsonl
dataset/real.zip -> prefixo de 2 GiB -> 200 janelas sem repetição -> dataset/processed/real/*.jsonl
dataset/processed -> filtragem linha a linha com modelo pequeno -> classificação por segmento com modelos grandes
```

## Estrutura

Coloque os arquivos brutos em `dataset/`:

```text
dataset/
  lab.zip
  real.zip
  lab_anchors.jsonl
```

## 1. Preparar Dataset Fixo

Este comando usa as 49 técnicas de `lab_anchors.jsonl`, resolve no `lab.zip` um evento
real de cada técnica e extrai cerca de 200 logs em volta da linha do ataque. Para o
real, cria/reutiliza um prefixo descompactado de 2 GiB e amostra 200 janelas de 200
logs sem repetir linhas entre arquivos.

```bash
uv run python scripts/comiset_extract.py prepare-dataset \
  --lab-zip dataset/lab.zip \
  --real-zip dataset/real.zip \
  --lab-anchors dataset/lab_anchors.jsonl \
  --best-anchors dataset/anchors.best.jsonl \
  --output-dir dataset/processed \
  --real-count 200 \
  --events-per-segment 200 \
  --seed 2026
```

Saídas principais:

```text
dataset/anchors.best.jsonl
dataset/processed/lab/T1574.jsonl
dataset/processed/lab/manifest.csv
dataset/processed/real/real_001.jsonl
dataset/processed/real/anchors.sampled.jsonl
dataset/processed/real/manifest.csv
dataset/cache/real_prefix.jsonl
```

`dataset/anchors.best.jsonl` guarda as âncoras lab resolvidas, incluindo a linha real
usada como centro (`resolved_anchor_line`) e os limites de linha (`line_start`/`line_end`).
`anchors.sampled.jsonl` guarda as âncoras sintéticas do real, também com
`line_start`/`line_end`, para auditoria. Se quiser recriar exatamente a mesma amostra,
mantenha a mesma seed e o mesmo prefixo.

## 2. Rodar Testes Ollama Sobre O Dataset Pronto

```bash
uv run python scripts/comiset_llm_pipeline.py run-dataset \
  --input-dir dataset/processed \
  --run-dir runs/final \
  --small-model llama3.2:3b \
  --big-model deepseek-r1:14b \
  --big-model qwen3:14b \
  --big-model phi4:14b \
  --big-model mistral-nemo:12b \
  --max-tokens 1500 \
  --prompt-format csv
```

Se parar, rode o mesmo comando com:

```bash
--resume
```

O checkpoint continua nas etapas LLM:

- filtro: última linha processada de `runs/final/dataset_input/all_events.jsonl`;
- classificação: último segmento classificado por modelo grande.

Use `--rebuild-input` no `run-dataset` apenas quando alterar arquivos em
`dataset/processed`.

## Estrutura Legada/Opcional

Os comandos abaixo continuam disponíveis para auditoria, extração materializada antiga
ou execução direta a partir dos ZIPs.

Os resultados ficam em `runs/`:

```text
runs/lab/extract/
runs/lab/anchors/
runs/lab/filter/
runs/lab/classify/
runs/real/extract/
runs/real/anchors/
runs/real/filter/
runs/real/classify/
```

O que fica em cada etapa:

```text
runs/<dataset>/extract/
  anchors.jsonl
  anchors.raw.jsonl
  checkpoint.json
  segment_events.jsonl
  segment_summary.csv
  by_segment/

runs/<dataset>/anchors/
  anchors.jsonl
  anchors.raw.jsonl
  checkpoint.json

runs/<dataset>/filter/<modelo_pequeno>/
  filtered_events.jsonl
  checkpoint.json
  metrics.json
  metrics_by_segment.csv

runs/<dataset>/classify/<modelo_pequeno>/<modelo_grande>/
  classifications.jsonl
  checkpoint.json
```

- `extract`: dados extraídos do ZIP e segmentados por janela temporal. É opcional no
  fluxo direto.
- `anchors`: âncoras compartilhadas do dataset no fluxo direto. Não depende do modelo
  pequeno.
- `filter`: saída do modelo pequeno, com decisão linha a linha e métricas da filtragem.
- `classify`: saída dos modelos grandes, com decisão por segmento/ataque.

Descrição dos arquivos:

- `anchors.raw.jsonl`: todos os eventos que tinham `rule_technique_id`, antes do
  agrupamento.
- `anchors.jsonl`: âncoras agrupadas usadas pelo pipeline. Cada linha representa uma
  janela de contexto. No `run-direct`, fica em `runs/<dataset>/anchors/` e é reutilizada
  por qualquer modelo pequeno.
- `segment_events.jsonl`: eventos extraídos ao redor de cada âncora. Use apenas se quiser
  materializar tudo antes da LLM para auditoria/debug.
- `segment_summary.csv`: resumo com quantidade de linhas/eventos por segmento/ataque.
- `by_segment/`: pasta opcional com um `.jsonl` separado para cada segmento/ataque.
- `filtered_events.jsonl`: saída do modelo pequeno. No fluxo direto, por padrão salva só
  as linhas mantidas pelo filtro. Use `--keep-dropped` se quiser salvar também as
  removidas.
- `metrics.json`: métricas globais da filtragem, como total de linhas, quantas ficaram,
  quantas foram removidas e quantas linhas com rule foram removidas.
- `metrics_by_segment.csv`: mesmas métricas de filtragem, mas separadas por
  `segment_id`/ataque.
- `classifications.jsonl`: saída do modelo grande. Cada linha representa a classificação
  final de um segmento/ataque.
- `checkpoint.json`: progresso da etapa. Permite continuar com `--resume` se o processo
  parar no meio.

## 1. Subir Ollama

```bash
docker compose up -d ollama
```

O pipeline usa a biblioteca Python `ollama`. Se um modelo não existir localmente, o
script tenta baixá-lo automaticamente. Para impedir isso, use `--no-pull-missing`.
Para economizar disco, use `--delete-model-after-use`: o script remove do Ollama cada
modelo efetivamente usado ao final da etapa, inclusive se a etapa falhar.
Os comandos `run`, `run-dataset` e `run-direct` gravam logs legíveis em
`<run-dir>/pipeline.log` por padrão. Para escolher outro arquivo, use `--log-file`.

## Métricas Estatísticas

Ao final da filtragem, o pipeline gera relatórios na pasta do modelo pequeno:

```text
filter_metrics.json
filter_confusion.csv
filter_metrics_by_segment.csv
filter_timing.csv
filter_false_negatives.jsonl
filter_false_positives.jsonl
```

Na filtragem, o ground truth positivo é a linha que tinha `rule_technique_id` oculto em
`evaluation`. `filter_false_negatives.jsonl` contém os logs maliciosos removidos pelo
filtro, incluindo linha, MITRE, `llm_event` completo, avaliação e resposta do modelo.

Ao final da classificação, cada pasta de modelo grande recebe:

```text
classification_metrics.json
classification_confusion.csv
classification_timing.csv
classification_chunk_timing.csv
classification_false_negatives.jsonl
classification_false_positives.jsonl
classification_true_positives.jsonl
classification_true_negatives.jsonl
```

As métricas incluem TP, FP, FN, TN, precisão, recall, especificidade, acurácia, F1,
balanced accuracy, erros de parse/inferência e tempos por inferência. Para recalcular:

```bash
uv run python scripts/comiset_llm_pipeline.py filter-report \
  --input runs/final/filter/llama3.2_3b/filtered_events.jsonl

uv run python scripts/comiset_llm_pipeline.py classify-report \
  --input runs/final/classify/llama3.2_3b/deepseek-r1_14b/classifications.jsonl
```

No `run-direct`, use `--keep-dropped` se quiser auditoria detalhada completa dos logs
removidos; sem isso, o JSONL da filtragem guarda só os logs mantidos.

## 2. Gerar Âncoras

Gere as âncoras uma vez por dataset. Esta etapa lê o ZIP em streaming, encontra os
eventos com `rule_technique_id`, agrupa eventos próximos do mesmo ataque e salva o
resultado em `runs/<dataset>/anchors/`.

Lab:

```bash
uv run python scripts/comiset_extract.py anchors \
  --zip dataset/lab.zip \
  --anchors runs/lab/anchors/anchors.jsonl \
  --checkpoint runs/lab/anchors/checkpoint.json \
  --before-seconds 60 \
  --after-seconds 60
```

Real:

```bash
uv run python scripts/comiset_extract.py anchors \
  --zip dataset/real.zip \
  --anchors runs/real/anchors/anchors.jsonl \
  --checkpoint runs/real/anchors/checkpoint.json \
  --before-seconds 60 \
  --after-seconds 60
```

Se parar, rode o mesmo comando com:

```bash
--resume
```

Saídas:

```text
runs/<dataset>/anchors/anchors.raw.jsonl
runs/<dataset>/anchors/anchors.jsonl
runs/<dataset>/anchors/checkpoint.json
```

`anchors.raw.jsonl` guarda todas as âncoras brutas. `anchors.jsonl` guarda as âncoras
agrupadas que serão usadas pelo pipeline. Se você mudar a janela temporal, os campos de
âncora ou a regra de agrupamento, gere as âncoras novamente. O agrupamento usa
`--merge-anchor-gap-seconds 0` por padrão, ou seja, junta âncoras apenas quando são do
mesmo host, têm a mesma identidade de técnica/regra e as janelas se encostam ou se
sobrepõem.

Se quiser reduzir para as melhores âncoras por técnica MITRE, escolha as âncoras sem
reler o ZIP:

```bash
uv run python scripts/comiset_extract.py select-best-anchors \
  --anchors runs/lab/anchors/anchors.jsonl \
  --output runs/lab/anchors/anchors.best.jsonl \
  --top-per-technique 1 \
  --min-time-gap-seconds 60
```

Essa etapa roda apenas em cima do `anchors.jsonl` e, para cada `technique_id`, mantém
as `N` melhores âncoras com maior `anchor_lines`. Use `--top-per-technique 2` ou
`--top-per-technique 3` para manter mais ocorrências repetidas do mesmo ataque. Por
padrão, ocorrências repetidas da mesma técnica precisam ter janelas temporais separadas
por pelo menos 60 segundos; use `--min-time-gap-seconds 0` para desativar esse filtro.

## 3. Rodar Pipeline Direto Recomendado

Este é o modo recomendado para o dataset real. Ele usa as âncoras já geradas e não cria
`segment_events.jsonl`. Se `runs/<dataset>/anchors/anchors.jsonl` não existir, o comando
para e pede para executar a etapa de âncoras primeiro.

Fluxo:

```text
anchors prontas + ZIP -> modelo pequeno filtra linha a linha -> modelos grandes classificam
```

Lab:

```bash
uv run python scripts/comiset_llm_pipeline.py run-direct \
  --zip dataset/lab.zip \
  --run-dir runs/lab \
  --small-model llama3.2:3b \
  --big-model deepseek-r1:14b \
  --big-model qwen3:14b \
  --big-model phi4:14b \
  --big-model mistral-nemo:12b \
  --max-tokens 1500 \
  --prompt-format csv
```

Real:

```bash
uv run python scripts/comiset_llm_pipeline.py run-direct \
  --zip dataset/real.zip \
  --run-dir runs/real \
  --small-model llama3.2:3b \
  --big-model deepseek-r1:14b \
  --big-model qwen3:14b \
  --big-model phi4:14b \
  --big-model mistral-nemo:12b \
  --max-tokens 1500 \
  --prompt-format csv
```

Se parar, rode o mesmo comando com:

```bash
--resume
```

Para testar outro modelo pequeno, troque apenas `--small-model`. O resultado vai para
outra pasta e não sobrescreve a execução anterior. As âncoras continuam em
`runs/<dataset>/anchors/`, então não precisam ser coletadas novamente.

Saídas principais:

```text
runs/lab/anchors/anchors.jsonl
runs/lab/anchors/anchors.raw.jsonl
runs/lab/anchors/checkpoint.json

runs/lab/filter/llama3.2_3b/filtered_events.jsonl
runs/lab/filter/llama3.2_3b/checkpoint.json
runs/lab/filter/llama3.2_3b/metrics.json
runs/lab/filter/llama3.2_3b/metrics_by_segment.csv

runs/lab/classify/llama3.2_3b/deepseek-r1_14b/classifications.jsonl
runs/lab/classify/llama3.2_3b/qwen3_14b/classifications.jsonl
```

O prompt usa CSV por padrão para economizar tokens. A classificação divide segmentos
grandes em chunks de aproximadamente `--max-tokens`.

## 4. Extração Materializada Opcional

Use esta etapa se quiser criar `segment_events.jsonl` antes da LLM para auditoria,
debug ou reprocessar vários filtros sem reler o ZIP.

Lab:

```bash
uv run python scripts/comiset_extract.py extract \
  --zip dataset/lab.zip \
  --output runs/lab/extract/segment_events.jsonl \
  --anchors runs/lab/extract/anchors.jsonl \
  --checkpoint runs/lab/extract/checkpoint.json \
  --before-seconds 60 \
  --after-seconds 60
```

Real:

```bash
uv run python scripts/comiset_extract.py extract \
  --zip dataset/real.zip \
  --output runs/real/extract/segment_events.jsonl \
  --anchors runs/real/extract/anchors.jsonl \
  --checkpoint runs/real/extract/checkpoint.json \
  --before-seconds 60 \
  --after-seconds 60
```

Se parar, rode o mesmo comando com:

```bash
--resume
```

A extração faz duas passagens no ZIP:

1. encontra eventos âncora com `rule_technique_id`;
2. busca os eventos no intervalo `-60s/+60s` no mesmo host.

O campo `rule_technique_id` não é enviado para LLM. Ele fica apenas em `evaluation`,
para métricas posteriores.

## 5. Ver Quantas Linhas Há Por Ataque

Isso depende de `segment_events.jsonl`, então use depois da extração materializada.

Gerar resumo:

```bash
uv run python scripts/comiset_segments.py summary \
  --input runs/lab/extract/segment_events.jsonl \
  --output runs/lab/extract/segment_summary.csv
```

Separar cada ataque/segmento em um `.jsonl`:

```bash
uv run python scripts/comiset_segments.py split \
  --input runs/lab/extract/segment_events.jsonl \
  --output-dir runs/lab/extract/by_segment
```

Isso cria também:

```text
runs/lab/extract/by_segment/manifest.csv
```

## 6. Rodar Pipeline A Partir De `segment_events.jsonl`

Este modo usa o arquivo materializado da etapa opcional:

```text
segment_events.jsonl -> modelo pequeno -> modelos grandes
```

```bash
uv run python scripts/comiset_llm_pipeline.py run \
  --input runs/lab/extract/segment_events.jsonl \
  --run-dir runs/lab \
  --small-model llama3.2:3b \
  --big-model deepseek-r1:14b \
  --big-model qwen3:14b \
  --big-model phi4:14b \
  --big-model mistral-nemo:12b \
  --max-tokens 1500 \
  --prompt-format csv
```

## 7. Métricas Da Filtragem

Durante a filtragem, o pipeline gera automaticamente:

```text
metrics.json
metrics_by_segment.csv
```

`metrics.json` mostra o total geral:

```json
{
  "events": 1000,
  "kept": 420,
  "dropped": 580,
  "rule_events": 37,
  "rule_kept": 35,
  "rule_dropped": 2,
  "keep_rate": 0.42,
  "drop_rate": 0.58,
  "rule_recall_after_filter": 0.9459,
  "rule_drop_rate": 0.0541
}
```

Campos importantes:

- `events`: quantas linhas foram analisadas.
- `kept`: quantas linhas o modelo pequeno manteve.
- `dropped`: quantas linhas o modelo pequeno removeu.
- `rule_events`: quantas linhas tinham `rule_technique_id` ocultado em `evaluation`.
- `rule_kept`: quantas linhas com rule foram mantidas.
- `rule_dropped`: quantas linhas com rule foram removidas.

Para recalcular depois:

```bash
uv run python scripts/comiset_llm_pipeline.py filter-metrics \
  --input runs/lab/filter/llama3.2_3b/filtered_events.jsonl \
  --output runs/lab/filter/llama3.2_3b/filter_metrics.csv
```

Observação: no modo `run-direct`, `filtered_events.jsonl` salva só linhas mantidas por
padrão. Portanto, para esse modo, use o `metrics.json` e `metrics_by_segment.csv`
gerados durante a execução como fonte correta dos descartes. Recalcular por
`filter-metrics` só preserva contagem de removidos se você tiver rodado com
`--keep-dropped`.

Para uma técnica específica:

```bash
uv run python scripts/comiset_llm_pipeline.py filter-metrics \
  --input runs/lab/filter/llama3.2_3b/filtered_events.jsonl \
  --output runs/lab/filter/llama3.2_3b/filter_metrics_T1553.004.csv \
  --technique-id T1553.004
```

## 8. Rodar Etapas Separadas

Filtragem direta do ZIP, sem criar `segment_events.jsonl`. Esta etapa também usa
âncoras já geradas; ela não cria `anchors.jsonl`.

```bash
uv run python scripts/comiset_llm_pipeline.py extract-filter \
  --zip dataset/lab.zip \
  --output runs/lab/filter/llama3.2_3b/filtered_events.jsonl \
  --anchors runs/lab/anchors/anchors.jsonl \
  --checkpoint runs/lab/filter/llama3.2_3b/checkpoint.json \
  --anchor-checkpoint runs/lab/anchors/checkpoint.json \
  --model llama3.2:3b \
  --prompt-format csv
```

Filtragem a partir de `segment_events.jsonl` materializado:

```bash
uv run python scripts/comiset_llm_pipeline.py filter \
  --input runs/lab/extract/segment_events.jsonl \
  --output runs/lab/filter/llama3.2_3b/filtered_events.jsonl \
  --checkpoint runs/lab/filter/llama3.2_3b/checkpoint.json \
  --model llama3.2:3b \
  --prompt-format csv
```

Classificação:

```bash
uv run python scripts/comiset_llm_pipeline.py classify \
  --input runs/lab/filter/llama3.2_3b/filtered_events.jsonl \
  --output runs/lab/classify/llama3.2_3b/deepseek-r1_14b/classifications.jsonl \
  --checkpoint runs/lab/classify/llama3.2_3b/deepseek-r1_14b/checkpoint.json \
  --model deepseek-r1:14b \
  --max-tokens 1500 \
  --prompt-format csv
```

## Checkpoints

Todos os scripts usam checkpoints em JSON. Se uma execução parar, rode novamente com:

```bash
--resume
```

As etapas longas mostram uma barra de progresso no terminal. Para desativar:

```bash
--no-progress
```

Na extração, o checkpoint guarda a fase e a linha processada do JSONL interno. Em ZIP
comprimido, retomar ainda exige descomprimir o fluxo até o ponto salvo, mas sem extrair
o arquivo inteiro para disco.

Na LLM, o checkpoint guarda a última linha filtrada ou o último segmento classificado.
