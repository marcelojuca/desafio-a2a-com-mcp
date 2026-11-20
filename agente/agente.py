"""Agente Central de Salas: servidor A2A v1.0 por fora, host MCP por dentro.

- GET  /.well-known/agent-card.json -> Agent Card v1.0
- POST /a2a                         -> JSON-RPC 2.0: SendMessage e GetTask

Sem LLM: o pedido chega em formato fixo e e traduzido por regra. Sem streaming:
SendMessage responde quando a Task chega a um estado terminal ou interrompido.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_cliente import McpCliente, trace_id_de
from ponte import Ponte
from tarefas import INPUT_REQUIRED, Tarefas, novo_id

HOST = os.environ.get("AGENTE_HOST", "127.0.0.1")
PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://{HOST}:{PORTA}").rstrip("/")
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

# Codigos JSON-RPC: os do JSON-RPC 2.0 e os especificos do A2A v1.0.
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
TASK_NOT_FOUND, UNSUPPORTED_OPERATION = -32001, -32004


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


AGENT_CARD: dict[str, Any] = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [{"url": f"{URL_PUBLICA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty",
                "escolha=sala-mirante",
                "escolha=recusar",
            ],
        }
    ],
}


class ErroRpc(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


tarefas = Tarefas()
mcp = McpCliente(MCP_URL)
ponte = Ponte(mcp)


def texto_da_mensagem(mensagem: dict[str, Any]) -> str:
    return " ".join(p.get("text", "") for p in mensagem.get("parts") or [] if isinstance(p, dict)).strip()


async def send_message(params: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
    mensagem = params.get("message")
    if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list):
        raise ErroRpc(INVALID_PARAMS, "params.message com parts e obrigatorio")
    texto = texto_da_mensagem(mensagem)
    task_id = mensagem.get("taskId")

    if not task_id:
        # Task nova: o trace-id do cliente A2A (ou um novo) acompanha todos os requests MCP dela.
        tarefa = tarefas.criar(mensagem.get("contextId"), trace_id_de(traceparent) or secrets.token_hex(16))
        tarefa.historico.append({**mensagem, "taskId": tarefa.id, "contextId": tarefa.context_id})
        log(f"[a2a] task={tarefa.id} criada ({tarefa.estado}) trace-id={tarefa.trace_id}")
        async with tarefa.trava:
            await ponte.iniciar(tarefa, texto)
        log(f"[a2a] task={tarefa.id} -> {tarefa.estado}")
        return {"task": tarefa.para_json()}

    tarefa = tarefas.obter(task_id)
    if tarefa is None:
        raise ErroRpc(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
    async with tarefa.trava:
        if tarefa.terminal:
            raise ErroRpc(UNSUPPORTED_OPERATION, f"Task {task_id} esta em estado terminal ({tarefa.estado}) e nao aceita mensagens")
        if tarefa.estado != INPUT_REQUIRED:
            raise ErroRpc(UNSUPPORTED_OPERATION, f"Task {task_id} nao esta aguardando input ({tarefa.estado})")
        tarefa.historico.append({**mensagem, "contextId": tarefa.context_id})
        await ponte.continuar(tarefa, texto)
    log(f"[a2a] task={tarefa.id} -> {tarefa.estado}")
    return {"task": tarefa.para_json()}


async def get_task(params: dict[str, Any], _traceparent: str | None) -> dict[str, Any]:
    task_id = params.get("id")
    if not isinstance(task_id, str):
        raise ErroRpc(INVALID_PARAMS, "params.id e obrigatorio")
    tarefa = tarefas.obter(task_id)
    if tarefa is None:
        raise ErroRpc(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
    history_length = params.get("historyLength")
    return {"task": tarefa.para_json(history_length if isinstance(history_length, int) else None)}


METODOS = {"SendMessage": send_message, "GetTask": get_task}


async def endpoint_a2a(request: Request) -> JSONResponse:
    traceparent = request.headers.get("traceparent")
    try:
        corpo = json.loads(await request.body())
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "JSON invalido"}})
    id_ = corpo.get("id") if isinstance(corpo, dict) else None
    try:
        if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
            raise ErroRpc(INVALID_REQUEST, "Request JSON-RPC 2.0 invalido")
        metodo = corpo["method"]
        log(f"[a2a] method={metodo} id={id_} traceparent={traceparent or '-'}")
        handler = METODOS.get(metodo)
        if handler is None:
            raise ErroRpc(METHOD_NOT_FOUND, f"Metodo nao suportado: {metodo}")
        params = corpo.get("params") or {}
        if not isinstance(params, dict):
            raise ErroRpc(INVALID_PARAMS, "params deve ser um objeto")
        resultado = await handler(params, traceparent)
        return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": resultado})
    except ErroRpc as e:
        erro: dict[str, Any] = {"code": e.code, "message": e.message}
        if e.data is not None:
            erro["data"] = e.data
        log(f"[a2a] erro {e.code}: {e.message}")
        return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": erro})


async def agent_card(_request: Request) -> JSONResponse:
    return JSONResponse(AGENT_CARD)


@asynccontextmanager
async def ciclo_de_vida(_app: Starlette):
    log(f"agente Central de Salas em {URL_PUBLICA} (A2A em /a2a) usando MCP em {MCP_URL}")
    yield
    await mcp.fechar()


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/a2a", endpoint_a2a, methods=["POST"]),
    ],
    lifespan=ciclo_de_vida,
)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORTA, log_level="warning")
