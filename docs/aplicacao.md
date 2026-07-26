# Documentação da aplicação COMISET + pipeline local de LLMs

## 1. Finalidade e estado atual

A aplicação prepara uma amostra reproduzível do dataset COMISET e executa um pipeline local de triagem de eventos de segurança em duas etapas:

1. um LLM pequeno decide, linha a linha, quais eventos possuem contexto útil;
2. um ou mais LLMs maiores classificam cada segmento como `Interesting` ou `Not Interesting`.

A inferência ocorre no mesmo processo Python por meio de `llama-cpp-python`. Os modelos GGUF são baixados do Hugging Face Hub e armazenados no cache local. Não existe servidor de inferência ou dependência de Ollama no estado atual da árvore de trabalho.

Esta implementação está em evolução. O dataset e o benchmark atuais **não são uma reprodução direta** do experimento de 60 alertas descrito em `docs/artigo_antigo.md`. O artigo antigo é contexto histórico da pesquisa; o experimento atual usa uma amostra materializada do COMISET.

## 2. Fontes científicas

### 2.1 Artigo anterior da pesquisa

`docs/artigo_antigo.md` descreve a proposta original de triagem auto-hospedada para SOCs:

- coleta em ambiente Windows com Sysmon, Winlogbeat e Elasticsearch;
- 60 alertas, sendo 41 ataques e 19 casos benignos;
- filtragem por modelos leves;
- classificação por modelos robustos;
- ensembles por maioria, votação ponderada e seleção dinâmica;
- comparação com classificadores tradicionais.

Esses números, estratégias e resultados não devem ser atribuídos automaticamente ao software atual.

### 2.2 Artigo do COMISET

`docs/dataset.md` descreve o COMISET, dataset de eventos Windows coletados em dois ambientes:

- **malicious test environment/lab:** ambiente controlado com ataques reais e rótulos MITRE ATT&CK;
- **real working environment/real:** atividade cotidiana de uma rede universitária, também contendo eventos que as regras do dataset marcaram com técnicas MITRE.

O artigo relata aproximadamente 250 milhões de eventos no total. Os arquivos completos são grandes demais para uso direto frequente, por isso esta aplicação materializa subconjuntos menores.

## 3. Arquitetura

```text
COMISET original
  dataset/lab.zip
  dataset/real.zip
          |
          v
scripts/comiset_extract.py
  âncoras lab + janelas fixas
  amostra real reproduzível
          |
          v
dataset/processed/{lab,real}/*.jsonl
          |
          v
scripts/comiset_llm_pipeline.py
  1. filtro linha a linha (LLM pequeno)
  2. agrupamento por segmento
  3. chunks de contexto
  4. classificação (LLMs grandes)
          |
          v
runs/<execução>/
  checkpoints, resultados, métricas e relatórios
```

### 3.1 Componentes

| Caminho | Responsabilidade |
|---|---|
| `scripts/comiset_extract.py` | Inspeção dos ZIPs, coleta/mesclagem/seleção de âncoras e materialização dos segmentos. |
| `scripts/comiset_llm_pipeline.py` | Filtragem, classificação, composição dos fluxos e geração de relatórios. |
| `scripts/comiset_segments.py` | Resumo e separação de um JSONL materializado por segmento. |
| `scripts/comiset/llama_cpp_client.py` | Parse da referência Hugging Face, download do GGUF e inferência com llama.cpp. |
| `scripts/comiset/prompts.py` | Prompts de sistema das duas etapas. |
| `scripts/comiset/records.py` | Conversão para CSV/JSON, chunking e interpretação das decisões. |
| `scripts/comiset/metrics.py` | Matrizes de confusão, métricas, tempos e relatórios de erros. |
| `scripts/comiset/checkpoint.py` | Escrita atômica e leitura de checkpoints. |
| `scripts/comiset/progress.py` | Barra de progresso sem dependência externa. |
| `dataset/processed/` | Amostra materializada e versionada por Git LFS. |
| `runs/` | Resultados locais; ignorados pelo Git. |

## 4. Ambiente e instalação

### 4.1 Requisitos

- Python 3.13, conforme `.python-version` e `pyproject.toml`;
- `uv` para ambiente e lockfile;
- Git LFS para obter os JSONL materializados;
- espaço para os GGUFs no cache do Hugging Face;
- Metal em Apple Silicon ou CUDA em NVIDIA para aceleração.

Dependências diretas:

- `llama-cpp-python`;
- `huggingface-hub`;
- `numpy`;
- `pandas`.

### 4.2 Dataset versionado

```bash
git lfs install
git lfs pull
```

Sem o Git LFS, os arquivos podem existir apenas como ponteiros de texto e não como JSONL utilizáveis.

### 4.3 Apple Silicon/Metal

```bash
CMAKE_ARGS="-DGGML_METAL=on" uv sync --reinstall-package llama-cpp-python
```

Confirme o backend instalado:

```bash
uv run python - <<'PY'
import llama_cpp
print(llama_cpp.llama_print_system_info().decode())
PY
```

A saída deve mencionar `METAL`.

### 4.4 Linux/NVIDIA CUDA

No servidor Linux, instale toolkit/driver CUDA compatíveis e reconstrua o binding:

```bash
CMAKE_ARGS="-DGGML_CUDA=on" uv sync --reinstall-package llama-cpp-python
```

A imagem ou ambiente CUDA deve ser construído no próprio sistema Linux; o binário Metal do macOS não é portátil.

### 4.5 Cache e autenticação do Hugging Face

Por padrão, `hf_hub_download` utiliza o cache do Hugging Face. Para escolher outro local:

```bash
export HF_HOME=/caminho/com/espaco/huggingface
```

Modelos públicos não exigem token. Modelos privados ou gated exigem autenticação compatível com `huggingface-hub`.

## 5. Identificação e carregamento dos modelos

As opções `--model`, `--small-model` e `--big-model` usam:

```text
repositorio/slug[@revisão]:arquivo.gguf
```

Exemplo para benchmark, preferencialmente com o commit do Hugging Face:

```text
bartowski/Llama-3.2-3B-Instruct-GGUF@<commit-hf>:Llama-3.2-3B-Instruct-Q4_K_M.gguf
```

Ao receber a primeira solicitação, o gateway:

1. separa `repo_id`, revisão opcional e `filename`;
2. baixa ou reutiliza o arquivo por `hf_hub_download`;
3. calcula o SHA-256 do GGUF e registra o commit resolvido do Hugging Face;
4. instancia `llama_cpp.Llama` com contexto, GPU, batch e seed fixos;
5. registra metadados GGUF, hash do chat template, backend e hardware;
6. executa os warm-ups fora do cronômetro de inferência;
7. chama `create_chat_completion` com temperatura zero, seed e limite de saída.

Opções comuns:

| Opção | Padrão | Efeito |
|---|---:|---|
| `--n-ctx` | `4096` | Janela de contexto do modelo. |
| `--n-gpu-layers` | `-1` | Tenta descarregar todas as camadas na GPU; `0` força CPU. |
| `--n-batch` | `512` | Batch lógico usado no processamento do prompt. |
| `--seed` | `2026` | Seed da inicialização e geração. |
| `--max-output-tokens` | `512` | Limite da resposta do modelo. |
| `--warmup-runs` | `1` | Inferências de aquecimento fora das métricas. |
| `--inference-runs` | `1` | Repetições medidas de cada prompt. |
| `--run-manifest` | automático | Caminho opcional do manifesto reproduzível. |
| `--prompt-format` | `csv` | Formato enviado ao modelo: `csv` ou `json`. |

O parâmetro de timeout ainda existe na CLI por compatibilidade com o fluxo anterior, mas não interrompe inferência dentro do processo.

## 6. Dataset materializado atual

### 6.1 Composição observada

| Subconjunto | Segmentos | Eventos por segmento | Eventos totais | Rótulo de segmento atual |
|---|---:|---:|---:|---|
| `lab` | 49 | 200 | 9.800 | uma técnica MITRE por segmento |
| `real` | 200 | 200 | 40.000 | `benign_or_unknown`, sem técnica |
| **Total** | **249** | **200** | **49.800** | 49 positivos e 200 negativos no relatório de classificação |

A seleção atual produz aproximadamente 19,7% de segmentos positivos e 80,3% negativos.

### 6.2 Lab

O arquivo `dataset/lab_anchors.jsonl` possui 49 âncoras, uma por técnica selecionada. Durante `prepare-dataset`, o código:

1. localiza no ZIP o primeiro evento compatível com a técnica, host e limites originais da âncora;
2. atualiza a âncora para essa linha real;
3. escolhe uma janela de 200 linhas, normalmente 100 antes e 99 depois;
4. grava um JSONL por técnica em `dataset/processed/lab/`;
5. registra limites e estatísticas em `manifest.csv`.

A janela materializada é baseada em **quantidade de linhas**, embora a âncora mantenha também metadados temporais de ±60 segundos.

### 6.3 Real

O fluxo atual:

1. descompacta/reutiliza os primeiros 2 GiB do JSONL em `dataset/cache/real_prefix.jsonl`;
2. coleta eventos que possuem timestamp e host;
3. coleta os primeiros 1.000 blocos limpos e não sobrepostos;
4. rejeita qualquer bloco que contenha campos MITRE ou `RuleName` revelando técnica;
5. sorteia 200 dos 1.000 candidatos com seed `2026`;
6. usa a mesma geometria do lab: 100 linhas anteriores, evento central e 99 posteriores;
7. grava `real_001.jsonl` até `real_200.jsonl`;
8. valida 200 eventos e ausência de rótulos em cada segmento;
9. marca os segmentos como `benign_or_unknown`.

A seed, o prefixo e o algoritmo precisam permanecer iguais para reproduzir a mesma amostra.

### 6.4 Manifestos

Cada subconjunto possui `manifest.csv` com:

- caminho e `segment_id`;
- origem (`lab` ou `real`);
- linha central e limites da janela;
- horário e host da âncora;
- rótulo de segmento;
- quantidade e intervalo dos eventos extraídos.

### 6.5 Estrutura de cada evento

Forma simplificada:

```json
{
  "segment_id": "...",
  "anchor_line": 123,
  "anchor_time": "2022-01-01T00:00:00+00:00",
  "event_line": 120,
  "event_id": "...",
  "llm_event": {
    "@timestamp": "...",
    "host_name": "...",
    "process_name": "..."
  },
  "evaluation": {
    "segment_label": {
      "technique_ids": ["T1059.001"]
    },
    "event_has_rule_technique": true,
    "hidden_label_fields": {
      "rule_technique_id": "T1059.001"
    }
  }
}
```

`llm_event` é a parte destinada ao prompt. `evaluation` deve permanecer exclusivamente como verdade de terreno e nunca ser enviado ao modelo.

## 7. Preparação do dataset

Os ZIPs completos não são versionados. Coloque-os em:

```text
dataset/lab.zip
dataset/real.zip
```

Comando principal:

```bash
uv run python scripts/comiset_extract.py prepare-dataset \
  --lab-zip dataset/lab.zip \
  --real-zip dataset/real.zip \
  --lab-anchors dataset/lab_anchors.jsonl \
  --best-anchors dataset/anchors.best.jsonl \
  --output-dir dataset/processed \
  --real-count 200 \
  --real-candidate-pool 1000 \
  --events-per-segment 200 \
  --seed 2026
```

Utilitários disponíveis em `comiset_extract.py`:

| Comando | Uso |
|---|---|
| `inspect` | Exibe membros e tamanhos do ZIP. |
| `scan-labels` | Procura campos de regra/técnica. |
| `anchors` | Coleta e mescla âncoras MITRE. |
| `merge-anchors` | Refaz a mesclagem de um arquivo de âncoras. |
| `select-best-anchors` | Seleciona as melhores ocorrências por técnica. |
| `prepare-dataset` | Gera a amostra final lab/real. |
| `extract` | Extrai janelas temporais em fluxo legado/opcional. |

Consulte opções completas com:

```bash
uv run python scripts/comiset_extract.py <comando> --help
```

## 8. Execução recomendada

```bash
uv run python scripts/comiset_llm_pipeline.py run-dataset \
  --input-dir dataset/processed \
  --run-dir runs/final \
  --small-model bartowski/Llama-3.2-3B-Instruct-GGUF:Llama-3.2-3B-Instruct-Q4_K_M.gguf \
  --big-model bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF:DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf \
  --big-model Qwen/Qwen3-14B-GGUF:Qwen3-14B-Q4_K_M.gguf \
  --max-tokens 1500 \
  --n-ctx 4096 \
  --n-gpu-layers -1 \
  --prompt-format csv
```

Para continuar:

```bash
# Utilize exatamente os mesmos modelos e parâmetros.
uv run python scripts/comiset_llm_pipeline.py run-dataset ... --resume
```

Use `--rebuild-input` somente se os arquivos de `dataset/processed/` tiverem mudado.

### 8.1 Comandos do pipeline

| Comando | Entrada | Resultado |
|---|---|---|
| `filter` | um JSONL materializado | decisão do LLM pequeno por evento |
| `filter-dataset` | `dataset/processed/` | junta lab/real e executa somente a filtragem |
| `extract-filter` | ZIP + âncoras | extração e filtragem sem materializar tudo |
| `classify` | saída da filtragem | decisão do modelo grande por segmento/chunk |
| `run` | `segment_events.jsonl` | filtro + N classificadores |
| `run-dataset` | `dataset/processed/` | fluxo recomendado sobre lab + real |
| `run-direct` | ZIP + âncoras | fluxo direto legado, sem `segment_events.jsonl` |
| `filter-metrics` | saída filtrada | resumo da retenção de eventos com regra |
| `filter-report` | saída filtrada completa | métricas detalhadas e erros do filtro |
| `classify-report` | classificações | métricas e erros da classificação |

## 9. Funcionamento da inferência

### 9.1 Filtragem

Para cada evento, o prompt pede:

```json
{"relevant": true, "confidence": 0.9, "reason": "..."}
```

O resultado é adicionado em `filter_result`. Todos os eventos são preservados, inclusive descartes e erros, para manter o universo de segmentos e permitir auditoria. `--keep-dropped` permanece apenas por compatibilidade de CLI.

### 9.2 Serialização CSV e JSON

CSV é o padrão por consumir menos caracteres. Ele possui colunas normalizadas para horário, host, usuário, processo, pai, evento, regra, command line e mensagem. JSON preserva o conteúdo completo de `llm_event`.

### 9.3 Chunking

Os eventos mantidos são agrupados por `segment_id`. O tamanho é estimado por `max_tokens * 4` caracteres. Cada chunk repete cabeçalho e metadados do segmento e é classificado separadamente.

### 9.4 Classificação

Resposta esperada:

```json
{"classification": "Interesting", "confidence": 0.9, "reason": "..."}
```

A decisão consolidada usa **votação por maioria** entre chunks válidos. Chunks com erro se abstêm; empate resulta em `Not Interesting`. Um segmento sem nenhum evento após o filtro recebe `empty_after_filter` e `Not Interesting`, portanto continua no denominador como FN ou TN. Se todos os chunks falharem, o segmento recebe estado operacional `error` e não é convertido em classe negativa.

### 9.5 Parse de respostas

O parser tenta, nesta ordem:

1. interpretar toda a resposta como JSON;
2. localizar o primeiro bloco entre chaves e interpretá-lo;
3. registrar `parse_error` e a resposta bruta.

Erros de inferência e parse são preservados como estado operacional. As métricas informam cobertura, itens pontuados e não pontuados, sem transformar falha em decisão negativa.

## 10. Checkpoints e retomada

Checkpoints são JSON gravados por substituição atômica. Eles armazenam a fase, posição de entrada, contadores, modelos e caminhos.

Regras práticas:

- retome sempre na mesma pasta e com os mesmos argumentos;
- não misture resultados de modelos/quantizações diferentes;
- não altere o JSONL de entrada durante uma execução;
- em ZIP comprimido, a retomada ainda percorre o fluxo até a linha salva;
- checkpoint `done` faz o comando retornar sem reprocessar.

## 11. Saídas

Estrutura principal:

```text
runs/final/
  pipeline.log
  run_manifest.json
  dataset_input/
    all_events.jsonl
    manifest.csv
  filter/<modelo-pequeno>/
    filtered_events.jsonl
    checkpoint.json
    metrics.json
    metrics_by_segment.csv
    filter_metrics.json
    filter_confusion.csv
    filter_metrics_by_segment.csv
    filter_timing.csv
    filter_false_negatives.jsonl
    filter_false_positives.jsonl
    filter_errors.jsonl
  classify/<modelo-pequeno>/<modelo-grande>/
    classifications.jsonl
    checkpoint.json
    classification_metrics.json
    classification_confusion.csv
    classification_timing.csv
    classification_chunk_timing.csv
    classification_false_negatives.jsonl
    classification_false_positives.jsonl
    classification_true_positives.jsonl
    classification_true_negatives.jsonl
    classification_errors.jsonl
```

Os nomes das pastas são derivados da referência completa do modelo por `safe_name`; caracteres como `/` e `:` tornam-se `_`.

## 12. Métricas

### 12.1 Filtragem

A verdade positiva em nível de evento é a presença de campos MITRE escondidos em `evaluation.hidden_label_fields`. Portanto, as métricas medem principalmente a retenção de **linhas marcadas por regra**, não a relevância semântica de todo o contexto.

São calculados TP, FP, FN, TN, precisão, recall, especificidade, acurácia, F1 e balanced accuracy, além de erros de parse/inferência e tempos.

`rule_recall_after_filter` é:

```text
linhas com regra mantidas / linhas com regra existentes
```

### 12.2 Classificação

A verdade positiva do segmento é a existência de `evaluation.segment_label.technique_ids`. A previsão consolidada segue a maioria dos chunks válidos. O relatório mantém `processed_segments`, `scored_segments`, `unscored_segments` e cobertura para que erros não desapareçam do total esperado.

### 12.3 Tempos

São reportados média, mediana, p95, mínimo, máximo e total para inferências/chunks/segmentos. Download/resolução, SHA-256, carga e cada warm-up ficam separados em `run_manifest.json`; `elapsed_seconds` representa a média das repetições aquecidas de cada prompt.

## 13. Protocolo recomendado para benchmark

Antes de publicar resultados:

1. congele os JSONL e registre checksums;
2. fixe repositório, arquivo GGUF, revisão/commit e SHA-256 de cada modelo;
3. fixe versão de `llama-cpp-python`, commit/backend do llama.cpp e parâmetros;
4. registre GPU, VRAM, driver, CUDA/Metal, CPU, RAM e sistema operacional;
5. confirme aceleração pelo `llama_print_system_info` e pelos logs de carga;
6. separe download, carga a frio, warm-up e inferência aquecida;
7. execute a mesma quantidade de repetições para todos os modelos;
8. preserve todos os 249 segmentos no denominador, inclusive os que perderem todos os eventos na filtragem;
9. registre erros de parse e inferência como categoria própria;
10. defina previamente a regra entre chunks e, se houver, a estratégia de ensemble;
11. use exatamente o mesmo formato de prompt e contexto em todas as comparações;
12. publique matriz de confusão e intervalos/variação, não apenas médias.

## 14. Desenvolvimento e validação

Testes atuais:

```bash
uv run python -m unittest discover -s scripts -p 'test_*.py'
uv run python -m compileall -q scripts
```

Validações rápidas:

```bash
uv run python scripts/comiset_extract.py --help
uv run python scripts/comiset_llm_pipeline.py --help
git diff --check
```

Arquivos em `dataset/processed/` usam Git LFS. Não os regrave sem intenção explícita de alterar a amostra do benchmark.

## 15. Inconsistências e riscos encontrados

Esta seção registra os riscos encontrados e o estado das correções. Itens marcados como resolvidos possuem implementação e testes na árvore atual. A diferença intencional em relação ao artigo antigo não é tratada como defeito.

### Críticos

1. **[RESOLVIDO] Vazamento de rótulo por campos textuais.** As 990 linhas afetadas no lab foram sanitizadas: `RuleName`, campos MITRE e linhas equivalentes de `event_original_message` são movidos para `evaluation`. CSV e JSON também sanitizam o payload em tempo de execução; testes percorreram os 49.800 eventos sem encontrar rótulos nos prompts.
2. **[RESOLVIDO] Contaminação do dataset real.** Os 200 segmentos foram regenerados a partir de um pool de 1.000 janelas limpas com seed `2026`. A validação atual confirma 40.000 eventos, zero campos MITRE ocultos/expostos em `RuleName`, geometria 100 + central + 99 e nenhuma sobreposição. A classe continua denominada `benign_or_unknown`, pois ausência de rótulo não comprova benignidade.
3. **[RESOLVIDO] Segmentos totalmente descartados.** Todos os eventos são preservados na saída do filtro e o segmento é criado antes da decisão de relevância. Segmentos vazios recebem `empty_after_filter` e entram como FN/TN; um teste confirma o denominador completo de 249 segmentos.
4. **[RESOLVIDO] Agregação entre chunks.** A saída grava uma decisão consolidada por maioria; chunks inválidos se abstêm e empate é `Not Interesting`. A estratégia, votos e estado ficam registrados.

### Altos

5. **[RESOLVIDO] Separação dos tempos.** Download/resolução, cálculo de hash, carga e warm-ups acontecem antes das medições e são registrados separadamente. `--warmup-runs` e `--inference-runs` controlam repetições.
6. **Timeout não funciona na inferência local.** `timeout_seconds` é descartado pelo gateway; um modelo travado não é interrompido.
7. **Janelas fixas por linha não equivalem a ±60 segundos.** No material atual, 200 linhas podem cobrir de 0 até 12.776 segundos no lab e até 3.861 segundos no real. Isso deve ser assumido explicitamente pelo protocolo.
8. **Amostra real limitada a uma região inicial do prefixo de 2 GiB.** O scanner para após encontrar os primeiros 1.000 blocos limpos; a seleção dos 200 não representa uniformemente o arquivo COMISET real completo de 914 GB.
9. **Chunking não usa o tokenizer real.** A regra de quatro caracteres por token pode exceder ou subutilizar `n_ctx`; cabeçalhos e tokens de saída não entram precisamente no orçamento.
10. **[RESOLVIDO] Identidade e ambiente da execução.** `run_manifest.json` registra revisão solicitada/resolvida, SHA-256 e tamanho do GGUF, metadados de quantização, hash do chat template, versões, backend, hardware, parâmetros, hashes de prompts e artefatos de entrada/saída.
11. **[RESOLVIDO] Erros separados de decisões.** Filtro e classificação registram falhas como itens não pontuados, com cobertura explícita e arquivos `*_errors.jsonl`; falha operacional não entra como classe negativa.

### Médios

12. **CSV descarta campos extraídos.** `file.path`, `winlog.task` e `Task` fazem parte de `DEFAULT_KEEP_FIELDS`, mas não possuem colunas no payload CSV. O formato JSON e o CSV analisam contextos diferentes.
13. **Ground truth da filtragem é estreito.** Uma linha contextual sem regra pode ser relevante para investigação, mas é considerada negativa nas métricas de evento. Assim, FP/FN do filtro não equivalem diretamente a relevância analítica humana.
14. **Sobreposição no lab.** Existem 3 pares de janelas de linha sobrepostas e 317 `event_id`s repetidos entre segmentos. Isso cria dependência entre amostras.
15. **`run-direct` não cria negativos por conta própria.** Quando executado apenas sobre âncoras MITRE do lab, não é adequado para medir especificidade/acurácia de classificação.
16. **Cobertura de testes ainda parcial.** Agora há testes de seleção limpa do real, ocultação de rótulos, geometria, segmentos vazios, denominador de 249 segmentos, maioria/empate/erros, revisão de modelo e separação de preparação/warm-up. Retomada e chunking com tokenizer real ainda não estão cobertos.
17. **Metadados do pacote estão incompletos.** `pyproject.toml` ainda usa a descrição placeholder `Add your description here`.
