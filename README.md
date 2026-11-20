# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio **A Ponte**. São dois processos separados, em Python:

| Processo | Pasta | Porta | Endpoint | Papel |
|---|---|---|---|---|
| Servidor MCP `central-de-salas` | `servidor-mcp/` | 7301 | `POST /mcp` (Streamable HTTP, stateless) | expõe `listar_salas`, `consultar_disponibilidade`, `reservar_sala` e o resource `politica://uso`, com MRTR na reserva |
| Agente `Central de Salas` | `agente/` | 7300 | `POST /a2a` (A2A v1.0, JSON-RPC) e `GET /.well-known/agent-card.json` | servidor A2A por fora, host MCP por dentro |

```
cliente A2A ──SendMessage/GetTask──▶ agente (:7300) ──tools/list, resources/read, tools/call──▶ servidor MCP (:7301)
              ◀── Task (estado, artifact) ──          ◀── complete | input_required + requestState ──
```

Stack: `mcp==2.3.0` (SDK oficial Python, v2, revisão 2026-07-28 da spec) no servidor; `starlette`, `uvicorn` e `httpx` no agente. Versões travadas em `pyproject.toml` e `uv.lock` de cada pasta. Não há LLM nem SDK de provedor de LLM em lugar nenhum: o agente interpreta o pedido em formato fixo por regra.

## Como rodar

Pré-requisitos: [uv](https://docs.astral.sh/uv/) e Python 3.11 ou superior. O uv baixa o Python 3.12 indicado em `.python-version` se ele não estiver instalado. O validador roda com o `python3` do sistema (3.10 ou superior).

```bash
git clone https://github.com/marcelojuca/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
```

**Terminal 1: servidor MCP.** A chave do `requestState` vem de `REQUEST_STATE_SECRET`, com no mínimo 32 bytes aleatórios. Gere uma chave sua (nunca a commite) e use a mesma em todo restart, senão os `requestState` emitidos antes deixam de ser válidos:

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
cd servidor-mcp
uv run servidor.py
```

Para reiniciar o servidor mantendo a chave, basta repetir `uv run servidor.py` no mesmo terminal. Sem a variável, ou com menos de 32 bytes, o processo se recusa a subir.

**Terminal 2: agente.**

```bash
cd agente
uv run agente.py
```

**Terminal 3: validador.** Rode com os dois processos recém-iniciados:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Variáveis opcionais, todas com os valores padrão do enunciado: `MCP_HOST`/`MCP_PORT` (127.0.0.1/7301), `AGENTE_HOST`/`AGENTE_PORT` (127.0.0.1/7300), `AGENTE_URL` (URL pública anunciada no card, padrão `http://127.0.0.1:7300`) e `MCP_URL` (padrão `http://127.0.0.1:7301/mcp`).

<details>
<summary>Sem uv (pip + venv)</summary>

```bash
# servidor MCP
cd servidor-mcp && python3 -m venv .venv && .venv/bin/pip install mcp==2.3.0 cryptography==48.0.1
.venv/bin/python servidor.py
# agente
cd agente && python3 -m venv .venv && .venv/bin/pip install httpx==0.28.1 starlette==1.10.0 uvicorn==0.54.0
.venv/bin/python agente.py
```
</details>

### Roteiro manual (fluxo do avaliador)

```bash
# Agent Card
curl -s http://localhost:7300/.well-known/agent-card.json

# Sala ocupada: a Task para em TASK_STATE_INPUT_REQUIRED com "alternativas: sala-fusca, sala-mirante"
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' \
  -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' \
  -d "$(python3 -c 'import json;print(json.dumps(json.load(open("exemplos/wire/08-a2a-send-message.json"))["request"]["body"]))')"

# Continuação (troque TASK_ID pelo id devolvido acima); use escolha=recusar para ver TASK_STATE_CANCELED
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":2,"method":"SendMessage","params":{"message":{"messageId":"m2","role":"ROLE_USER","parts":[{"text":"escolha=sala-mirante"}],"taskId":"TASK_ID"}}}'

# GetTask
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":3,"method":"GetTask","params":{"id":"TASK_ID"}}'
```

## Onde a ponte acontece

A ponte inteira está em [`agente/ponte.py`](agente/ponte.py). Quando o `tools/call` de `reservar_sala` volta com `resultType: "input_required"`, o método `Ponte._aplicar` ([`agente/ponte.py:153`](agente/ponte.py#L153)) chama `extrair_pausa` ([`:206`](agente/ponte.py#L206)). Essa função lê a única entrada de `inputRequests` (a chave que o servidor atribuiu, a propriedade do `requestedSchema` e o `enum`, na ordem recebida) e guarda tudo, junto com o `requestState` opaco, em `Tarefa.pausa`. Em seguida a Task vai para `TASK_STATE_INPUT_REQUIRED`, com a mensagem `alternativas: <ids>` ([`:157-163`](agente/ponte.py#L157-L163)). O `requestState` fica só nesse objeto em memória. `Tarefa.para_json` ([`agente/tarefas.py`](agente/tarefas.py)) não serializa `pausa`, então ele não aparece no card, no artifact nem nas mensagens. A volta é em `Ponte.continuar` ([`:116`](agente/ponte.py#L116)). O `escolha=<id>` do cliente A2A vira `{"action":"accept","content":{"sala":<id>}}`, e `escolha=recusar` vira `{"action":"decline"}`. Uma escolha fora do enum mantém a Task pausada e repete a lista. O mesmo `tools/call` sai de novo ([`:136-142`](agente/ponte.py#L136-L142)), com a mesma tool e os mesmos argumentos, `inputResponses` sob a mesma chave e o `requestState` ecoado byte a byte. O id JSON-RPC é novo porque [`McpCliente.request`](agente/mcp_cliente.py#L78) numera cada request com um contador do processo. Como a pausa vive na `Tarefa` e cada SendMessage é serializado pela trava da própria Task, duas Tasks pausadas ao mesmo tempo nunca trocam de `requestState`.

Do lado do servidor, `reservar_sala` ([`servidor-mcp/servidor.py:221`](servidor-mcp/servidor.py#L221)) devolve um `InputRequiredResult` cujo `request_state` traz o pedido original e as alternativas oferecidas. Na segunda rodada, `retomar_reserva` ([`:268`](servidor-mcp/servidor.py#L268)) reconstrói o pedido só a partir desse estado e confere que a escolha pertence às alternativas seladas.

## Decisões técnicas

**Proteção do `requestState`.** Usei o utilitário pronto do SDK: `MCPServer(request_state_security=RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600))` ([`servidor.py:200`](servidor-mcp/servidor.py#L200)). Com isso, o `RequestStateBoundary` do SDK fica na borda do servidor.
- **Na saída**, embrulha o estado que a tool produziu em um envelope com `iat`, `exp`, método, nome da tool, digest SHA-256 dos argumentos e audiência (nome do servidor), e o sela com **AES-256-GCM**, com chave derivada por HKDF-SHA256 da variável de ambiente. O token tem o formato `v1.<base64url(kid‖nonce‖ciphertext‖tag)>`. Ele é cifrado, além de assinado, então também é ilegível para o cliente.
- **Na entrada**, verifica tag, expiração, método, tool, digest dos argumentos e audiência antes de qualquer handler rodar. Qualquer falha vira `-32602 "Invalid or expired requestState"`, e o motivo real vai só para o stderr (por exemplo `requestState rejected on tools/call: seal`).
- **Argumentos adulterados no retry:** o digest dos argumentos não bate e o estado é rejeitado com `-32602`. Ainda assim, por defesa em profundidade, `retomar_reserva` usa os valores selados, nunca os reenviados.
- **Validade:** 10 minutos (`TTL_REQUEST_STATE = 600`, dentro da faixa de 5 a 30 minutos). O servidor não guarda nada entre `input_required` e o retry, então um retry continua válido depois de um restart se a chave for a mesma. Testei os dois casos: reiniciar com a mesma chave conclui a reserva, e reiniciar com outra chave dá `-32602`.
- **Sem segredo no código:** se `REQUEST_STATE_SECRET` faltar ou tiver menos de 32 bytes, o servidor encerra com a instrução de como gerar a chave.

**MRTR feito à mão, e não com `Resolve`/`Elicit`.** O SDK também oferece MRTR por resolvers, mas neles o estado guarda só a resposta, e o resolver recalcula a pergunta a cada rodada. Preferi que a tool devolvesse o `InputRequiredResult` diretamente. Assim o estado carrega o pedido inteiro e as alternativas, como o enunciado pede, e a chave (`escolha_de_sala`) é atribuída pelo servidor. A checagem de capability é explícita (`exigir_elicitation_form`) e acontece depois de calcular as alternativas. Sem alternativas, o resultado é `isError` com "Sem alternativas disponiveis no intervalo". Com alternativas e sem `elicitation.form` declarado no `_meta` do próprio request, a resposta é `-32021` com `data.requiredCapabilities` e HTTP 400.

**Transporte e `_meta`.** O servidor roda `streamable_http_app(stateless_http=True, json_response=True)`. A rejeição de `_meta` sem `protocolVersion` ou `clientCapabilities` (`-32602`, HTTP 400) e a checagem dos headers espelhados (`-32020`) são feitas pelo próprio SDK. Um middleware ASGI ([`servidor-mcp/log_requests.py`](servidor-mcp/log_requests.py)) registra no stderr cada request (método, id, traceparent do `_meta`, nome da tool ou URI, e `retry=sim` quando há `requestState`), inclusive os que o SDK recusa antes do handler.

**Cliente MCP do agente.** [`agente/mcp_cliente.py`](agente/mcp_cliente.py) é um cliente JSON-RPC mínimo sobre Streamable HTTP. Todo request leva no `_meta` `protocolVersion`, `clientInfo`, `clientCapabilities = {"elicitation": {"form": {}}}` e um `traceparent` com o trace-id da Task e um span-id novo. Nos headers vão `MCP-Protocol-Version`, `Mcp-Method` e, em `tools/call` e `resources/read`, `Mcp-Name`. Não usei o `mcp.Client` do SDK por dois motivos, ambos verificados na v2.3.0. Primeiro, o `ClientSession` só declara elicitation se receber um `elicitation_callback`, e então declara `{"form": {}, "url": {}}` (`mcp/client/session.py:635`), não a forma pedida. Segundo, o modelo de callback é justamente o que fecharia o ciclo sem devolver a pergunta ao cliente A2A. O SDK permite receber o `InputRequiredResult` cru com `session.call_tool(..., allow_input_required=True)`, e cheguei a validar esse caminho, mas o cliente próprio deixa o contrato de fio explícito e auditável. A cada Task nova o agente faz `tools/list` e monta os argumentos a partir do `inputSchema` descoberto, sem lista fixa no código. Depois faz `resources/read politica://uso` e extrai a versão da primeira linha, e só então faz o `tools/call`.

**Estado das Tasks.** Fica em memória, em um dicionário `task id -> Tarefa` ([`agente/tarefas.py`](agente/tarefas.py)), e cada Task tem uma trava própria (`asyncio.Lock`). Os estados seguem `SUBMITTED -> WORKING -> (INPUT_REQUIRED -> WORKING)* -> COMPLETED | CANCELED | FAILED`. Estado terminal é definitivo: `transitar` recusa sair dele, e um SendMessage para uma Task terminal recebe `-32004` (UnsupportedOperation). Task inexistente recebe `-32001`. O trace-id vem do header `traceparent` da chamada A2A que abre a Task, ou é gerado quando o header falta. Ele é guardado na Task e usado em todos os requests MCP dela, inclusive no retry. Um erro de execução da tool (`isError: true`) termina a Task em `TASK_STATE_FAILED`, com o texto da tool na mensagem de status e no histórico. As reservas do servidor também ficam em memória e não sobrevivem a um restart, como permitido.

**Agent Card.** Segue a forma da v1.0 (`supportedInterfaces[]` com `url`, `protocolBinding: "JSONRPC"` e `protocolVersion: "1.0"`), espelhando `exemplos/wire/07-a2a-agent-card.json`, com a skill `reservar-sala`.

**Plataforma.** `cryptography` é dependência transitiva do `mcp` (via `pyjwt[crypto]`). Travei a 48.0.1 porque as versões mais novas não publicam wheel para macOS Intel e, sem wheel, a instalação tenta compilar o pacote com Rust.

## Saída do validador

Última execução, a partir de um clone limpo do fork, com os dois processos recém-iniciados:

```
trace-id desta execucao: ffca45dbf5e4ab64ab9289588c4cd70a
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
