"""A ponte: onde o estado do MCP (requestState) encontra o estado do A2A (Task).

- `iniciar`: Task nova -> descobre as tools (tools/list), le a politica
  (resources/read), chama reservar_sala (tools/call) e aplica o resultado.
- `_aplicar`: e aqui que um `input_required` do MCP vira TASK_STATE_INPUT_REQUIRED
  no A2A; o requestState fica guardado na Task (`Tarefa.pausa`), fora do fio.
- `continuar`: a resposta do cliente A2A vira `inputResponses`, e o mesmo
  tools/call e repetido com id novo levando o requestState ecoado sem alteracao.

O agente nao decide nada de dominio: conflito, politica e alternativas vem do
servidor MCP. Ele so traduz o pedido em texto para argumentos, e o resultado do
MCP para estados e artifacts do A2A.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

import httpx

from mcp_cliente import McpCliente, McpErro
from tarefas import CANCELED, COMPLETED, FAILED, INPUT_REQUIRED, WORKING, Pausa, Tarefa

TOOL_RESERVA = "reservar_sala"
URI_POLITICA = "politica://uso"
CAMPOS_DO_ARTIFACT = ("reserva", "sala", "inicio", "fim", "responsavel")
USO = "Pedido invalido. Use: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"

_PEDIDO = re.compile(r"^\s*reservar\s+(?P<resto>.+?)\s*$", re.DOTALL)
_CHAVE = re.compile(r"(?:^|\s)([A-Za-z_][\w-]*)=")
_ESCOLHA = re.compile(r"^\s*escolha\s*=\s*(?P<valor>\S+)\s*$")
_VERSAO = re.compile(r"^\s*vers(?:ao|ão)\s*:\s*(?P<versao>\S+)", re.IGNORECASE)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def interpretar_pedido(texto: str) -> dict[str, str] | None:
    """`reservar sala=... inicio=... fim=... responsavel=...` -> dict. O valor vai ate a proxima chave."""
    m = _PEDIDO.match(texto)
    if not m:
        return None
    resto = m.group("resto")
    chaves = list(_CHAVE.finditer(resto))
    if not chaves or resto[: chaves[0].start()].strip():
        return None
    pedido: dict[str, str] = {}
    for i, chave in enumerate(chaves):
        fim = chaves[i + 1].start() if i + 1 < len(chaves) else len(resto)
        pedido[chave.group(1)] = resto[chave.end() : fim].strip()
    return pedido


def interpretar_escolha(texto: str) -> str | None:
    m = _ESCOLHA.match(texto)
    return m.group("valor") if m else None


def versao_da_politica(texto: str) -> str | None:
    for linha in texto.splitlines():
        if linha.strip():
            m = _VERSAO.match(linha)
            return m.group("versao") if m else None
    return None


def linha_de_alternativas(opcoes: list[str]) -> str:
    return "alternativas: " + ", ".join(opcoes)


def texto_do_resultado(resultado: dict[str, Any]) -> str:
    return " ".join(c.get("text", "") for c in resultado.get("content", []) if c.get("type", "text") == "text").strip()


class Ponte:
    def __init__(self, mcp: McpCliente) -> None:
        self.mcp = mcp

    # ------------------------------------------------------------------ entrada

    async def iniciar(self, tarefa: Tarefa, texto: str) -> None:
        tarefa.transitar(WORKING)
        pedido = interpretar_pedido(texto)
        if pedido is None:
            tarefa.transitar(FAILED, USO)
            return
        try:
            # Descoberta em runtime: o schema da tool vem do servidor, nao do codigo.
            tools = {t["name"]: t for t in await self.mcp.listar_tools(trace_id=tarefa.trace_id)}
            tool = tools.get(TOOL_RESERVA)
            if tool is None:
                tarefa.transitar(FAILED, f"O servidor MCP nao oferece a tool {TOOL_RESERVA}")
                return
            argumentos, faltando = montar_argumentos(tool.get("inputSchema") or {}, pedido)
            if faltando:
                tarefa.transitar(FAILED, f"{USO} (faltou: {', '.join(faltando)})")
                return

            # Resource e escolha da aplicacao: o agente le a politica para carimbar o artifact.
            politica = await self.mcp.ler_resource(URI_POLITICA, trace_id=tarefa.trace_id)
            tarefa.politica = versao_da_politica(politica)

            resultado = await self.mcp.chamar_tool(TOOL_RESERVA, argumentos, trace_id=tarefa.trace_id)
        except McpErro as e:
            tarefa.transitar(FAILED, f"Erro do servidor MCP ({e.code}): {e.message}")
            return
        except httpx.HTTPError as e:
            tarefa.transitar(FAILED, f"Servidor MCP indisponivel: {e.__class__.__name__}")
            return
        self._aplicar(tarefa, TOOL_RESERVA, argumentos, resultado)

    async def continuar(self, tarefa: Tarefa, texto: str) -> None:
        pausa = tarefa.pausa
        assert pausa is not None and tarefa.estado == INPUT_REQUIRED
        escolha = interpretar_escolha(texto)

        if escolha == "recusar":
            resposta: dict[str, Any] = {"action": "decline"}
        elif escolha is not None and escolha in pausa.opcoes:
            resposta = {"action": "accept", "content": {pausa.campo: escolha}}
        else:
            # Fora do enum da elicitation: continua pausada e repete as alternativas.
            tarefa.transitar(INPUT_REQUIRED, linha_de_alternativas(pausa.opcoes))
            tarefa.pausa = pausa
            return

        tarefa.transitar(WORKING)
        try:
            # O retry: mesma tool e mesmos argumentos, id JSON-RPC novo (o cliente MCP
            # numera cada request), inputResponses com a chave que o servidor escolheu
            # e o requestState devolvido exatamente como chegou.
            resultado = await self.mcp.chamar_tool(
                pausa.tool,
                pausa.argumentos,
                trace_id=tarefa.trace_id,
                input_responses={pausa.chave: resposta},
                request_state=pausa.request_state,
            )
        except McpErro as e:
            tarefa.transitar(FAILED, f"Erro do servidor MCP ({e.code}): {e.message}")
            return
        except httpx.HTTPError as e:
            tarefa.transitar(FAILED, f"Servidor MCP indisponivel: {e.__class__.__name__}")
            return
        self._aplicar(tarefa, pausa.tool, pausa.argumentos, resultado)

    # ------------------------------------------------------------------ saida

    def _aplicar(self, tarefa: Tarefa, tool: str, argumentos: dict[str, Any], resultado: dict[str, Any]) -> None:
        """Traduz um resultado MCP em estado A2A. Aqui o input_required vira pausa da Task."""
        tipo = resultado.get("resultType", "complete")

        if tipo == "input_required":
            pausa = extrair_pausa(tool, argumentos, resultado)
            if pausa is None:
                tarefa.transitar(FAILED, "O servidor MCP pediu um tipo de input que este agente nao sabe repassar")
                return
            tarefa.transitar(INPUT_REQUIRED, linha_de_alternativas(pausa.opcoes))
            tarefa.pausa = pausa  # o requestState fica aqui, ligado a esta Task, e nao sai no fio
            log(f"[ponte] task={tarefa.id} pausada: input_required -> {INPUT_REQUIRED} chave={pausa.chave}")
            return

        if tipo != "complete":
            tarefa.transitar(FAILED, f"Resultado MCP desconhecido: resultType={tipo}")
            return

        if resultado.get("isError"):
            # Erro de execucao da tool: a mensagem exata dela vai para o historico da Task.
            tarefa.transitar(FAILED, texto_do_resultado(resultado) or "A tool devolveu erro sem mensagem")
            return

        dados = resultado.get("structuredContent")
        if not isinstance(dados, dict):
            try:
                dados = json.loads(texto_do_resultado(resultado))
            except ValueError:
                dados = {}
        if dados.get("reservado") is False:
            tarefa.transitar(CANCELED, f"Nenhuma sala reservada: {dados.get('motivo') or 'recusado'}")
            return

        reserva = {campo: dados.get(campo) for campo in CAMPOS_DO_ARTIFACT}
        reserva["politica"] = tarefa.politica
        tarefa.artefatos.append(
            {
                "artifactId": f"art-{tarefa.id.split('-', 1)[-1]}",
                "name": "reserva",
                "parts": [{"text": json.dumps(reserva, ensure_ascii=False)}],
            }
        )
        tarefa.transitar(COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")


def montar_argumentos(input_schema: dict[str, Any], pedido: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    """Preenche os argumentos a partir do inputSchema descoberto, nao de uma lista fixa."""
    propriedades = input_schema.get("properties") or {}
    argumentos = {nome: pedido[nome] for nome in propriedades if nome in pedido}
    faltando = [nome for nome in input_schema.get("required") or [] if nome not in argumentos]
    return argumentos, faltando


def extrair_pausa(tool: str, argumentos: dict[str, Any], resultado: dict[str, Any]) -> Pausa | None:
    pedidos = resultado.get("inputRequests") or {}
    request_state = resultado.get("requestState")
    if len(pedidos) != 1 or not isinstance(request_state, str):
        return None
    chave, pedido = next(iter(pedidos.items()))
    params = pedido.get("params") or {}
    if pedido.get("method") != "elicitation/create" or params.get("mode", "form") != "form":
        return None
    propriedades = (params.get("requestedSchema") or {}).get("properties") or {}
    if len(propriedades) != 1:
        return None
    campo, schema = next(iter(propriedades.items()))
    opcoes = schema.get("enum") or ([schema["const"]] if "const" in schema else [])
    if not opcoes:
        return None
    return Pausa(
        tool=tool,
        argumentos=dict(argumentos),
        chave=chave,
        campo=campo,
        opcoes=[str(o) for o in opcoes],
        request_state=request_state,
    )
