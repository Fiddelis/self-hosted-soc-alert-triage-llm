# AGENTS.md

## Escopo

Este repositório materializa uma amostra do COMISET e executa um pipeline local de LLMs para triagem de eventos Windows. Leia `docs/aplicacao.md` antes de alterar extração, prompts, inferência ou métricas.

## Contexto da pesquisa

- `docs/artigo_antigo.md` é histórico: descreve outro dataset/experimento de 60 alertas e ensembles.
- `docs/dataset.md` é o artigo do COMISET.
- O benchmark atual usa 49 segmentos `lab` e 200 segmentos `real`, com 200 eventos cada.
- A mudança de dataset em relação ao artigo antigo é intencional; não a trate como bug.
- Não altere os artigos históricos para fazê-los coincidir com o software atual.

## Ambiente

```bash
git lfs install
git lfs pull
uv sync
```

Python: 3.13.

Build acelerado do `llama-cpp-python`:

```bash
# Apple Silicon
CMAKE_ARGS="-DGGML_METAL=on" uv sync --reinstall-package llama-cpp-python

# NVIDIA/Linux
CMAKE_ARGS="-DGGML_CUDA=on" uv sync --reinstall-package llama-cpp-python
```

Nunca suponha que o build Metal funciona em CUDA ou vice-versa. Confirme o backend com `llama_cpp.llama_print_system_info()`.

## Organização

- `scripts/comiset_extract.py`: preparação e extração do dataset.
- `scripts/comiset_llm_pipeline.py`: filtragem, classificação e relatórios.
- `scripts/comiset_segments.py`: resumo/separação de segmentos.
- `scripts/comiset/`: módulos compartilhados.
- `dataset/processed/`: amostra versionada com Git LFS.
- `runs/`: resultados gerados e ignorados.
- `docs/aplicacao.md`: documentação técnica e lista de riscos do benchmark.

## Convenções

- Código e identificadores em inglês; documentação e mensagens ao usuário podem ser em português.
- Prefira biblioteca padrão e funções existentes antes de criar abstrações.
- Preserve JSONL como uma entidade por linha e UTF-8.
- Use escrita atômica para checkpoints e métricas substituídas durante execuções longas.
- Não carregue os ZIPs completos ou JSONLs grandes em memória sem necessidade.
- Não modifique arquivos materializados/LFS incidentalmente.
- Não edite nem remova arquivos não rastreados que não foram criados pela tarefa atual.

## Invariantes do dataset

- Nunca envie `evaluation`, `segment_label`, campos MITRE ou equivalentes ao LLM.
- Ao adicionar campos de rótulo, atualize a ocultação e crie teste contra vazamento.
- Preserve `segment_id`, `event_id`, `anchor_line`, `anchor_time` e `event_line` em transformações.
- Seed, prefixo real, quantidade/tamanho das janelas e seleção de âncoras fazem parte da definição do benchmark.
- Uma alteração nesses valores exige regenerar manifestos e documentar que o dataset mudou.
- Todo segmento esperado deve continuar no denominador, mesmo se a filtragem remover todos os eventos.

## Invariantes do benchmark

- Compare modelos com o mesmo dataset, prompt, formato, contexto, chunking e regra de agregação.
- Fixe repositório, nome do GGUF, revisão e checksum antes de resultados finais.
- Registre quantização, versão de llama.cpp, backend, hardware e parâmetros `n_ctx`, `n_gpu_layers` e `n_batch`.
- Separe download, carga a frio, warm-up e inferência aquecida.
- Não converta silenciosamente erro de execução em decisão do modelo.
- Não chame a agregação atual de votação por maioria: no estado atual ela é OR (`any Interesting`).
- Não use `run-direct` somente com âncoras positivas para alegar especificidade ou acurácia global.

## Modelos

Formato aceito:

```text
repositorio-hf:arquivo.gguf
```

Exemplo:

```text
Qwen/Qwen3-14B-GGUF@<commit-hf>:Qwen3-14B-Q4_K_M.gguf
```

Verifique no Hugging Face se repositório e nome de arquivo existem antes de documentá-los. Não troque família ou quantização de modelo sem explicar o impacto metodológico.

## Validação mínima

Após mudanças Python:

```bash
uv run python -m unittest discover -s scripts -p 'test_*.py'
uv run python -m compileall -q scripts
uv run python scripts/comiset_extract.py --help
uv run python scripts/comiset_llm_pipeline.py --help
git diff --check
```

Mudança não trivial em ocultação, chunking, retomada, agregação ou métricas deve incluir um teste pequeno que falhe antes da correção.

Não baixe modelos grandes nem execute o dataset completo apenas para smoke test. Use fixture JSONL mínima ou mock do gateway.

## Revisão antes de concluir

1. Confira `git status` e não inclua mudanças alheias.
2. Verifique se nenhum rótulo aparece no payload efetivamente enviado ao modelo.
3. Verifique se checkpoint/resume não duplica nem omite registros.
4. Verifique se os denominadores das métricas incluem todas as amostras esperadas.
5. Atualize `docs/aplicacao.md` quando CLI, esquema, dataset ou metodologia mudar.
6. Destaque qualquer impacto sobre comparabilidade/reprodutibilidade no resumo da alteração.

## Decisões metodológicas implementadas

- rótulos MITRE e equivalentes textuais são ocultados antes do prompt;
- o real contém 200 janelas sorteadas entre 1.000 limpas, com seed `2026`;
- segmentos totalmente filtrados permanecem como `empty_after_filter`;
- chunks usam maioria, erros se abstêm e empate é `Not Interesting`;
- falhas operacionais ficam fora da matriz como itens não pontuados;
- carga, warm-up e inferência têm tempos separados;
- `run_manifest.json` registra artefatos, ambiente e parâmetros.

Consulte a seção final de `docs/aplicacao.md` para riscos ainda abertos, especialmente timeout local, janela por linhas, representatividade do prefixo e chunking aproximado.
