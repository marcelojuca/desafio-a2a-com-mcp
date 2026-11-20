"""Task do A2A v1.0: identidade, estado e produto, guardada em memoria no agente.

A parte publica (id, contextId, status, history, artifacts) e o que vai para o
fio. A parte privada (`pausa`) e o estado da ponte com o MCP enquanto a Task
espera o cliente A2A, incluindo o requestState opaco, e nunca e serializada.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Any

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
REJECTED = "TASK_STATE_REJECTED"
TERMINAIS = {COMPLETED, CANCELED, FAILED, REJECTED}


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


@dataclass
class Pausa:
    """O que o agente guarda entre o input_required do MCP e a resposta do cliente A2A.

    `request_state` e opaco: o agente guarda e ecoa byte a byte, nunca abre.
    """

    tool: str
    argumentos: dict[str, Any]
    chave: str  # chave atribuida pelo servidor em inputRequests, devolvida igual em inputResponses
    campo: str  # propriedade do requestedSchema que a escolha preenche
    opcoes: list[str]  # o enum da elicitation, na ordem em que veio
    request_state: str


@dataclass
class Tarefa:
    id: str
    context_id: str
    trace_id: str
    estado: str = SUBMITTED
    mensagem_status: dict[str, Any] | None = None
    historico: list[dict[str, Any]] = field(default_factory=list)
    artefatos: list[dict[str, Any]] = field(default_factory=list)
    pausa: Pausa | None = None
    politica: str | None = None  # versao lida do resource politica://uso
    trava: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def terminal(self) -> bool:
        return self.estado in TERMINAIS

    def mensagem_agente(self, texto: str) -> dict[str, Any]:
        return {
            "messageId": novo_id("msg"),
            "role": "ROLE_AGENT",
            "parts": [{"text": texto}],
            "taskId": self.id,
            "contextId": self.context_id,
        }

    def transitar(self, estado: str, texto: str | None = None) -> None:
        if self.terminal:
            raise RuntimeError(f"Task {self.id} ja esta em estado terminal ({self.estado})")
        self.estado = estado
        if texto is None:
            self.mensagem_status = None
        else:
            msg = self.mensagem_agente(texto)
            self.mensagem_status = msg
            self.historico.append(msg)
        if estado != INPUT_REQUIRED:
            self.pausa = None

    def para_json(self, history_length: int | None = None) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.estado}
        if self.mensagem_status is not None:
            status["message"] = self.mensagem_status
        historico = self.historico
        if history_length is not None:
            historico = historico[-history_length:] if history_length > 0 else []
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": list(historico),
            "artifacts": list(self.artefatos),
        }


class Tarefas:
    def __init__(self) -> None:
        self._por_id: dict[str, Tarefa] = {}

    def criar(self, context_id: str | None, trace_id: str) -> Tarefa:
        tarefa = Tarefa(id=novo_id("task"), context_id=context_id or novo_id("ctx"), trace_id=trace_id)
        self._por_id[tarefa.id] = tarefa
        return tarefa

    def obter(self, task_id: str) -> Tarefa | None:
        return self._por_id.get(task_id)
