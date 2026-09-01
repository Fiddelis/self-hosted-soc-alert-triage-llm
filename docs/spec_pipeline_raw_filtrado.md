# Spec: classificar raw e filtrado no mesmo pipeline e endurecer a inferência

## Problem Statement

O benchmark atual consegue classificar os dados filtrados e os dados raw, mas o raw precisa ser executado separadamente por meio do modo operacional `raw`. Isso aumenta a chance de esquecer a baseline, duplica preparação e carga do modelo grande e dificulta garantir que a comparação use exatamente o mesmo modelo, prompt, formato, contexto, chunking e regra de agregação.

A auditoria da execução HPC também encontrou problemas que podem invalidar ou distorcer os resultados:

- uma saída de filtragem ficou estruturalmente corrompida, com segmento ausente, eventos duplicados e apenas 248 segmentos;
- um modelo pequeno produziu quase somente erros de parsing, que acabaram funcionando como remoção efetiva de eventos para a classificação;
- alguns modelos grandes excederam a janela de contexto;
- respostas com raciocínio foram truncadas pelo limite de saída e registradas como erros de parsing;
- checkpoints e arquivos JSONL não são sempre validados de forma suficiente antes da retomada;
- a ausência da classificação raw em uma execução comum deixa a baseline dependente de uma etapa manual separada.

O benchmark deve continuar preservando os 249 segmentos e 49.800 eventos esperados, sem enviar rótulos de avaliação ao modelo e sem transformar falhas operacionais em decisões negativas.

## Solution

Criar uma orquestração única de classificação do modelo grande que, para cada modelo grande e modo de raciocínio, processe sequencialmente:

1. a entrada raw completa, com todos os eventos;
2. cada saída de filtragem válida dos modelos pequenos disponíveis.

O raw deve ser processado uma única vez por combinação de modelo grande e modo de raciocínio. Cada fonte terá sua própria saída JSONL, checkpoint, métricas, tempos e referência de entrada, permitindo comparar raw versus cada filtragem sem colisões ou duplicações.

A mesma execução deve reutilizar o modelo grande carregado e aplicar a mesma configuração de prompt, sanitização, formato, limite de contexto, chunking, agregação e política de erros às duas condições. A única diferença metodológica intencional será a entrada: raw contém todos os eventos; filtrado contém somente os eventos mantidos pelo filtro, preservando os registros e o estado operacional dos eventos removidos ou com erro.

A implementação também deve endurecer o pipeline para:

- exigir respostas JSON estruturadas e validar os campos esperados;
- detectar respostas truncadas pelo `finish_reason`;
- dimensionar chunks com tokens reais e reservar espaço para a saída;
- tratar erro de filtragem como erro operacional ou abstinência, nunca como `relevant=false`;
- validar a integridade dos JSONL antes da classificação e antes da retomada;
- fazer flush do resultado antes de gravar o checkpoint;
- impedir que um arquivo com segmentos duplicados, ausentes ou fora de ordem gere classificações aparentemente completas;
- gerar métricas e um índice comparativo por fonte.

## User Stories

1. As a pesquisador do benchmark, quero que uma execução do modelo grande classifique automaticamente o raw e todas as filtragens, so that eu não precise lembrar de uma etapa manual separada para obter a baseline.

2. As a pesquisador, quero que o raw seja classificado exatamente uma vez por modelo grande e modo de raciocínio, so that quatro filtros não produzam quatro cópias redundantes da mesma baseline.

3. As a pesquisador, quero reutilizar o mesmo modelo grande carregado durante a classificação raw e filtrada, so that as condições tenham menor custo operacional e configuração mais controlada.

4. As a pesquisador, quero que cada fonte tenha checkpoint próprio, so that a interrupção de uma classificação filtrada não obrigue a repetir o raw ou outras filtragens já concluídas.

5. As a pesquisador, quero retomar uma fonte usando somente registros JSONL duráveis, so that um checkpoint gravado antes do resultado não faça o pipeline pular segmentos.

6. As a pesquisador, quero que a retomada falhe de forma explícita quando o checkpoint e o arquivo de saída não correspondem, so that o pipeline não anexe resultados a uma sequência corrompida.

7. As a pesquisador, quero validar que a entrada raw contém os 249 segmentos e 49.800 eventos esperados, so that uma baseline incompleta não seja confundida com resultado do modelo.

8. As a pesquisador, quero validar cada saída de filtragem antes de classificá-la, so that arquivos com eventos duplicados, segmentos ausentes ou contagem incorreta sejam rejeitados antes de gerar métricas.

9. As a pesquisador, quero uma saída de classificação por segmento para cada fonte, so that raw, filtro do Llama 3.2, filtro do Phi-4 Mini, filtro do TinyLlama e filtro do Gemma possam ser comparados sem ambiguidade.

10. As a pesquisador, quero que a saída registre explicitamente se a fonte é raw ou filtrada e qual modelo pequeno produziu a filtragem, so that os artefatos não dependam apenas do nome de diretório.

11. As a pesquisador, quero que raw e filtrado usem o mesmo prompt e a mesma sanitização, so that a diferença observada seja atribuível à filtragem e não a uma mudança acidental de protocolo.

12. As a pesquisador, quero que dados de avaliação, rótulos MITRE, `segment_label` e campos equivalentes continuem ocultos do payload enviado ao modelo em ambas as fontes, so that não haja vazamento de ground truth.

13. As a pesquisador, quero preservar `segment_id`, `event_id`, `anchor_line`, `anchor_time` e `event_line` durante a classificação, so that cada resultado possa ser rastreado até o evento original.

14. As a pesquisador, quero que uma resposta do modelo seja limitada a JSON estruturado, so that problemas de texto livre, marcadores de raciocínio e blocos incompletos não sejam tratados como decisões válidas.

15. As a pesquisador, quero validar os campos e valores da resposta JSON, so that um JSON sintaticamente válido mas sem classificação ou sem decisão de filtragem seja registrado como erro.

16. As a pesquisador, quero registrar `finish_reason`, tokens do prompt e tokens da resposta, so that truncamento por limite de saída seja distinguido de uma decisão legítima.

17. As a pesquisador, quero que o pipeline reserve espaço para a resposta dentro de `n_ctx`, so that um chunk de entrada grande não falhe por exceder a janela de contexto do modelo.

18. As a pesquisador, quero que chunks inválidos ou respostas truncadas produzam abstinência operacional, so that uma falha não seja convertida silenciosamente em `Not Interesting`.

19. As a pesquisador, quero que erros de filtragem permaneçam identificáveis por evento e segmento, so that uma filtragem com baixa cobertura não produza acurácia artificial por classe negativa dominante.

20. As a pesquisador, quero que um segmento com erro de filtragem seja marcado como não pontuado ou erro operacional quando a falha impedir uma decisão segura, so that o relatório não o trate como segmento vazio após filtragem.

21. As a pesquisador, quero que todos os 249 segmentos esperados apareçam no relatório operacional, mesmo quando sem decisão válida, so that a cobertura do processo seja transparente.

22. As a pesquisador, quero métricas de classificação separadas por fonte, so that eu possa obter acurácia, precisão, recall, F1, especificidade, balanced accuracy, matriz de confusão, cobertura e taxa de erro para raw e cada filtragem.

23. As a pesquisador, quero um relatório comparativo que relacione a fonte, o filtro, o modelo grande, o modo de raciocínio e os hashes das entradas, so that os resultados sejam auditáveis e reproduzíveis.

24. As a pesquisador, quero que os tempos de carga, warm-up e inferência permaneçam separados por modelo e fonte, so that o custo da filtragem não seja confundido com o custo da classificação.

25. As a pesquisador, quero que os modos `auto`, `think` e `no_think` permaneçam identificados nos artefatos, so that resultados de condições metodologicamente diferentes não sejam misturados.

26. As a pesquisador, quero que o protocolo trate a saída estruturada e o modo de raciocínio como parte da configuração experimental, so that a comparação com execuções antigas não seja declarada equivalente quando o protocolo mudou.

27. As a pesquisador, quero executar um smoke test com uma fixture pequena e um gateway falso, so that o fluxo raw + filtrado seja validado sem baixar modelos grandes nem consumir o dataset completo.

28. As a pesquisador, quero que a execução HPC tenha uma única chamada por modelo grande e modo de raciocínio para processar todas as fontes, so that a operação seja simples e a baseline não seja esquecida.

29. As a pesquisador, quero conservar uma classificação de entrada única como operação de baixo nível, so that seja possível depurar ou reprocessar uma fonte específica sem duplicar a orquestração completa.

30. As a pesquisador, quero que uma execução antiga ou um arquivo corrompido não seja “consertado” silenciosamente, so that resultados potencialmente inválidos não sejam apresentados como comparáveis.

31. As a pesquisador, quero que a documentação explique a diferença entre raw e filtrado, so that a baseline seja interpretada corretamente e não seja confundida com uma filtragem sem eventos.

## Implementation Decisions

- A seam principal será uma orquestração de classificação por modelo grande. Ela recebe o diretório da execução, o modelo grande, o modo de raciocínio e a configuração de inferência, descobre a entrada raw e as saídas de filtragem, valida as entradas e executa cada fonte com o mesmo gateway.

- O pipeline de alto nível de classificação será responsável pela classificação raw e filtrada. A operação de classificação de uma única entrada continuará disponível como seam de baixo nível para depuração, recuperação e testes.

- O fluxo HPC deixará de depender de uma chamada raw separada para produzir a baseline. A chamada de classificação por modelo grande deverá classificar o raw uma vez e cada filtragem válida uma vez.

- A entrada raw será o artefato materializado completo do dataset. Ela será processada com inclusão explícita de eventos relevantes e irrelevantes, sem usar a saída de nenhum filtro.

- Cada fonte terá identidade explícita: `raw` ou `filtered` com o identificador do modelo pequeno. O identificador da fonte será incluído no manifesto, checkpoint, classificação e relatórios.

- As saídas de fontes diferentes não compartilharão arquivo, checkpoint ou métricas. O layout lógico deverá permitir localizar separadamente o resultado raw e o resultado de cada filtro sem depender de sobrescrita ou de nomes ambíguos.

- O modelo grande, seu commit, o arquivo GGUF, checksum, backend, hardware, versão do runtime e parâmetros de contexto serão registrados uma vez no manifesto compartilhado. Cada fonte registrará também seu hash de entrada e protocolo específico.

- A classificação raw e filtrada usará o mesmo prompt, formato de payload, sanitização de campos, limite de eventos por segmento, estratégia de chunking, limite de saída, temperatura, seed e regra de agregação. A diferença de entrada será registrada como parte da condição experimental.

- A entrada raw deverá ter 249 segmentos e 49.800 eventos para o dataset atual. Uma entrada filtrada deverá conservar a entidade completa do JSONL e ser validada por chave de evento, segmento esperado e ausência de duplicações. A implementação deve derivar o denominador do manifesto quando possível, sem remover segmentos totalmente filtrados.

- A saída de classificação deverá ter uma linha por segmento esperado. Um segmento sem decisão válida poderá possuir estado operacional `error`, `unscored` ou equivalente, mas não poderá ser convertido em classe negativa por causa de falha de inferência.

- O gateway deverá usar resposta JSON estruturada para filtragem e classificação. A forma JSON deverá ser validada semanticamente, incluindo campos obrigatórios, tipos e valores permitidos. O modo estruturado não autoriza o envio de rótulos de avaliação ao modelo.

- O gateway deverá preservar metadados de resposta, incluindo `finish_reason`, contagem de tokens do prompt e da conclusão. Resposta truncada, vazia, inválida ou incompatível com o schema será registrada como erro operacional.

- O modo `think` será tratado como condição experimental distinta. Para condições estruturadas comparáveis, o protocolo preferencial será `no_think` ou `auto` conforme o modelo; se `think` for mantido, seu limite de saída, cobertura e comportamento de JSON deverão ser registrados e validados separadamente.

- O chunking de classificação deverá usar contagem de tokens do tokenizer do modelo, ou uma verificação equivalente feita pelo gateway, reservando espaço para o limite de saída e uma margem de segurança. O algoritmo deverá dividir chunks antes da chamada quando a soma ultrapassar o contexto.

- Um evento com payload individualmente maior que o orçamento não deverá ser enviado em um chunk inválido nem descartado como irrelevante. O caso deverá gerar erro operacional ou uma política explícita de truncamento documentada e mensurada.

- Erros de filtragem não conterão uma decisão negativa sintética. A classificação deverá distinguir entre evento não mantido pelo filtro, segmento vazio legitimamente filtrado e segmento cujo filtro não conseguiu produzir decisão.

- O checkpoint só poderá avançar depois que o resultado JSONL tiver sido escrito e liberado. A retomada deverá conferir contagem, validade sintática e sequência de identificadores duráveis. Divergência deverá interromper a execução com instrução para novo diretório ou reprocessamento explícito.

- A integridade será verificada também quando o checkpoint indicar conclusão. Um estado `done` não será suficiente para pular a etapa se o arquivo de saída não satisfizer a validação esperada.

- O relatório deverá separar métricas pontuadas de cobertura operacional. Erros e abstinências permanecerão visíveis, mas fora da matriz de confusão quando não houver decisão válida. A métrica não poderá ser artificialmente beneficiada por segmentos que falharam.

- Cada fonte deverá produzir os artefatos de métricas existentes para classificação e uma identificação de fonte. Deverá haver também um índice comparativo que permita obter a linha raw e as linhas filtradas do mesmo modelo grande, modo e execução.

- A estratégia atual de agregação será preservada neste escopo; a alteração não deverá renomeá-la nem atribuir a ela uma interpretação metodológica diferente sem decisão separada.

- O comando de alto nível deverá poder retomar apenas fontes incompletas. Se o raw já estiver completo, ele não será executado novamente ao retomar uma filtragem, e vice-versa.

- A execução não fará reparo automático de arquivo corrompido. Ela falhará de forma segura e exigirá novo diretório ou reconstrução explícita, preservando o artefato inválido para auditoria.

## Testing Decisions

- O teste principal será no seam de orquestração de classificação por modelo grande, usando arquivos JSONL pequenos e um gateway falso. Ele deverá verificar que uma execução com dois filtros produz uma fonte raw e duas fontes filtradas, sem duplicar o raw.

- O teste de orquestração deverá verificar que todas as fontes recebem o mesmo modelo grande, prompt, formato, modo, seed, contexto e regra de agregação, diferenciando somente a identidade e o conteúdo da entrada.

- O teste deverá verificar que o raw inclui eventos que seriam removidos pelos filtros e que os rótulos de avaliação continuam ausentes do payload enviado ao gateway.

- O teste deverá verificar que cada fonte possui saída, checkpoint e métricas separados e que a retomada de uma fonte não altera as demais.

- O teste deverá simular checkpoint à frente do JSONL, JSONL com linha parcial, JSONL com chave duplicada e JSONL com segmento ausente. Em todos os casos, a execução deverá interromper ou retomar somente quando a política definida for segura.

- O teste deverá verificar que um filtro com erro operacional não produz `relevant=false` sintético nem `empty_after_filter` pontuável quando a ausência de eventos resulta de erro.

- O teste deverá verificar que um segmento com todos os chunks inválidos permanece no denominador operacional, mas não entra na matriz de confusão como classe negativa.

- O teste deverá verificar respostas JSON válidas, JSON com campo ausente, tipo incorreto, enum inválido, resposta vazia e resposta truncada. O comportamento esperado é decisão válida somente no primeiro caso.

- O teste do gateway deverá verificar que a chamada usa JSON estruturado, preserva `finish_reason` e registra uso de tokens. Como a versão usada no HPC é `llama-cpp-python 0.3.34`, a compatibilidade da API deverá ser coberta por um teste de contrato nessa versão.

- O teste de chunking deverá cobrir o limite exato do contexto, a reserva de tokens de saída, a divisão de um segmento em múltiplos chunks e um evento que não cabe individualmente.

- O teste deverá verificar que a classificação de uma entrada única continua funcionando, evitando regressão do seam de baixo nível usado para depuração.

- O teste de métricas deverá conferir precisão, recall, F1, acurácia, especificidade, balanced accuracy, matriz de confusão, cobertura, erros e não pontuados separadamente por fonte.

- O teste deverá verificar que os denominadores incluem todos os segmentos esperados e que uma fonte com erro não parece melhor apenas por ter menos itens pontuados.

- A suíte existente de testes unitários do pipeline e do cliente llama.cpp será estendida com fixtures pequenas. Não será executado o dataset completo nem baixado GGUF grande como parte da validação local.

- A validação final deverá incluir descoberta de testes, compilação Python, ajuda da CLI, verificação de diff e uma execução de smoke test com gateway falso.

## Out of Scope

- Alterar a composição dos 4 modelos pequenos ou 6 modelos grandes.

- Trocar família de modelo, quantização, arquivo GGUF ou dataset.

- Alterar os rótulos de avaliação, a definição dos 249 segmentos, as janelas ou a seleção das âncoras.

- Reescrever artigos históricos ou fazer o artigo antigo coincidir com o benchmark atual.

- Executar novamente o dataset completo no desenvolvimento local.

- Corrigir automaticamente os arquivos já corrompidos da execução HPC.

- Tornar os modos `think` de Qwen3 e DeepSeek metodologicamente equivalentes.

- Criar um serviço de inferência remoto ou substituir `llama-cpp-python`.

- Alterar a regra de agregação de chunks em uma iniciativa separada.

- Produzir ensembles, votação entre modelos grandes ou uma nova política de decisão além da existente.

## Further Notes

A execução inicial ainda pode fornecer indícios exploratórios, mas os resultados afetados por arquivo de filtragem incompleto, cobertura quase nula, truncamento ou erro de contexto não devem ser tratados como resultado final.

Depois desta alteração, será necessário rerodar as classificações filtradas afetadas e a baseline raw para cada modelo grande e modo escolhido. As filtragens pequenas que forem validadas estruturalmente poderão ser reutilizadas; filtragens corrompidas ou com cobertura insuficiente deverão ser refeitas antes da classificação final.

A baseline raw deverá permanecer comparável à condição filtrada somente quando o modelo grande, prompt, formato, sanitização, contexto, chunking, seed, versão do runtime, backend, hardware e regra de agregação forem iguais. O manifesto deverá deixar explícita qualquer diferença.

A definição de pronto inclui documentação operacional atualizada para que o comando padrão do HPC produza raw e filtrado, instruções de retomada por fonte e checklist de validação dos 249 segmentos e 49.800 eventos.

A publicação no tracker deve receber o rótulo `ready-for-agent`.
