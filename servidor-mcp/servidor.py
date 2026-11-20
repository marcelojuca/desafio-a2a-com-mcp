"""Servidor MCP da Central de Salas (Streamable HTTP, revisao 2026-07-28).

Tres tools (listar_salas, consultar_disponibilidade, reservar_sala), um resource
(politica://uso) e o ciclo completo de MRTR na reserva:

- conflito -> resultType "input_required" com uma elicitation em form mode e um
  requestState que carrega o pedido original e as alternativas calculadas;
- o requestState e selado (AES-256-GCM, com expiracao e vinculo ao request)
  pelo RequestStateBoundary do SDK, com a chave de REQUEST_STATE_SECRET;
- o retry chega com id novo, inputResponses e o requestState ecoado; o
  servidor nao guarda nada entre as duas rodadas, entao funciona mesmo depois
  de um restart.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.mcpserver import Context, MCPServer, RequestStateSecurity
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp_types import (
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ElicitRequest,
    ElicitRequestFormParams,
    InputRequiredResult,
)
from pydantic import BaseModel

from log_requests import RequestLogMiddleware

DADOS = Path(os.environ.get("DADOS_DIR", Path(__file__).resolve().parent.parent / "dados"))
FUSO_SP = timezone(timedelta(hours=-3))
JANELA_INICIO = 8
JANELA_FIM = 20
DURACAO_MAXIMA = timedelta(hours=2)
CHAVE_ELICITATION = "escolha_de_sala"
MENSAGEM_ELICITATION = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."
TTL_REQUEST_STATE = 10 * 60  # segundos; a politica do desafio pede entre 5 e 30 minutos


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- dados

SALAS: list[dict[str, Any]] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
SALAS_POR_ID = {s["id"]: s for s in SALAS}
RESERVAS: list[dict[str, Any]] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()


def proximo_id_reserva() -> str:
    maior = max((int(r["id"].split("-")[1]) for r in RESERVAS), default=0)
    return f"res-{maior + 1:04d}"


# --------------------------------------------------------------------------- modelos de saida


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# --------------------------------------------------------------------------- regras


def _parse(valor: str) -> datetime:
    try:
        dt = datetime.fromisoformat(valor)
    except (TypeError, ValueError):
        raise ToolError(f"Data invalida: {valor}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FUSO_SP)
    return dt


def validar_pedido(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Aplica as validacoes de sala e de politica, na ordem do enunciado."""
    if sala not in SALAS_POR_ID:
        raise ToolError(f"Sala inexistente: {sala}")
    ini, end = _parse(inicio), _parse(fim)
    if end <= ini:
        raise ToolError("Intervalo invalido: fim deve ser posterior a inicio")
    ini_sp, end_sp = ini.astimezone(FUSO_SP), end.astimezone(FUSO_SP)
    abre = ini_sp.replace(hour=JANELA_INICIO, minute=0, second=0, microsecond=0)
    fecha = ini_sp.replace(hour=JANELA_FIM, minute=0, second=0, microsecond=0)
    if ini_sp < abre or end_sp > fecha:
        raise ToolError("Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00")
    if end - ini > DURACAO_MAXIMA:
        raise ToolError("Duracao acima do limite: a politica permite no maximo 2 horas")
    return ini, end


def conflitos(sala: str, ini: datetime, end: datetime) -> list[dict[str, Any]]:
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala and _parse(r["inicio"]) < end and ini < _parse(r["fim"])
    ]


def alternativas(sala: str, ini: datetime, end: datetime) -> list[str]:
    """Salas livres com capacidade >= a pedida, ate 3, por capacidade e depois id."""
    capacidade = SALAS_POR_ID[sala]["capacidade"]
    livres = [
        s
        for s in SALAS
        if s["id"] != sala and s["capacidade"] >= capacidade and not conflitos(s["id"], ini, end)
    ]
    livres.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in livres[:3]]


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    nova = {"id": proximo_id_reserva(), "sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    RESERVAS.append(nova)
    log(f"[reserva] criada {nova['id']} sala={sala} inicio={inicio} fim={fim} responsavel={responsavel}")
    return ReservaOut(
        reserva=nova["id"],
        reservado=True,
        sala=sala,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=POLITICA_VERSAO,
    )


def exigir_elicitation_form(ctx: Context) -> None:
    """-32021 quando o cliente nao declarou elicitation em form mode neste request."""
    caps = ctx.client_capabilities
    elicitation = caps.elicitation if caps is not None else None
    # Um `elicitation: {}` sem modos conta como form (forma anterior aos modos); url-only nao conta.
    if elicitation is not None and (elicitation.form is not None or elicitation.url is None):
        return
    raise MCPError(
        code=MISSING_REQUIRED_CLIENT_CAPABILITY,
        message="Client did not declare the form elicitation capability required by reservar_sala",
        data={"requiredCapabilities": {"elicitation": {"form": {}}}},
    )


# --------------------------------------------------------------------------- servidor


def carregar_segredo() -> str:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        log(
            "REQUEST_STATE_SECRET ausente ou curto (minimo 32 bytes). Gere com:\n"
            '  export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")'
        )
        sys.exit(1)
    return segredo


mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[carregar_segredo()], ttl=TTL_REQUEST_STATE),
)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    ini, end = validar_pedido(sala, inicio, fim)
    em_conflito = conflitos(sala, ini, end)
    return Disponibilidade(
        sala=sala,
        livre=not em_conflito,
        conflitos=[ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in em_conflito],
    )


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
async def reservar_sala(
    sala: str, inicio: str, fim: str, responsavel: str, ctx: Context
) -> ReservaOut | InputRequiredResult:
    if ctx.request_state:
        return retomar_reserva(ctx)

    ini, end = validar_pedido(sala, inicio, fim)
    if not conflitos(sala, ini, end):
        return criar_reserva(sala, inicio, fim, responsavel)

    opcoes = alternativas(sala, ini, end)
    if not opcoes:
        raise ToolError("Sem alternativas disponiveis no intervalo")
    exigir_elicitation_form(ctx)

    # O estado carrega tudo que o retry precisa: o pedido original e as opcoes oferecidas.
    # Sai daqui em claro; o RequestStateBoundary do SDK o sela antes de ir para o fio.
    estado = {
        "pedido": {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel},
        "alternativas": opcoes,
    }
    return InputRequiredResult(
        input_requests={CHAVE_ELICITATION: pergunta(opcoes)},
        request_state=json.dumps(estado, separators=(",", ":")),
    )


def pergunta(opcoes: list[str]) -> ElicitRequest:
    return ElicitRequest(
        params=ElicitRequestFormParams(
            message=MENSAGEM_ELICITATION,
            requested_schema={
                "type": "object",
                "properties": {
                    "sala": {
                        "type": "string",
                        "title": "Sala",
                        "description": "Sala alternativa escolhida",
                        "enum": opcoes,
                    }
                },
                "required": ["sala"],
            },
        )
    )


def retomar_reserva(ctx: Context) -> ReservaOut | InputRequiredResult:
    """Segunda rodada do MRTR.

    Quando chegamos aqui o boundary ja verificou integridade, expiracao e o vinculo
    do requestState ao request (nome da tool e digest dos argumentos), e substituiu o
    token pelo texto que nos mesmos produzimos. Mesmo assim o pedido e reconstruido
    a partir do estado selado, nunca dos argumentos reenviados pelo cliente.
    """
    estado = json.loads(ctx.request_state or "{}")
    pedido, opcoes = estado["pedido"], estado["alternativas"]
    exigir_elicitation_form(ctx)

    resposta = (ctx.input_responses or {}).get(CHAVE_ELICITATION)
    if resposta is None:
        # Retry sem resposta: pergunta de novo, com o mesmo estado.
        return InputRequiredResult(
            input_requests={CHAVE_ELICITATION: pergunta(opcoes)},
            request_state=ctx.request_state,
        )
    acao = getattr(resposta, "action", None)
    if acao in ("decline", "cancel"):
        return ReservaOut(reservado=False, motivo="recusado" if acao == "decline" else "cancelado")
    if acao != "accept":
        raise ToolError("Resposta de elicitation invalida")

    escolhida = (getattr(resposta, "content", None) or {}).get("sala")
    if escolhida not in opcoes:
        raise ToolError(f"Escolha fora das alternativas oferecidas: {escolhida}")
    ini, end = validar_pedido(escolhida, pedido["inicio"], pedido["fim"])
    if conflitos(escolhida, ini, end):
        raise ToolError(f"A sala escolhida nao esta mais livre no intervalo: {escolhida}")
    return criar_reserva(escolhida, pedido["inicio"], pedido["fim"], pedido["responsavel"])


@mcp.resource("politica://uso", name="politica-de-uso", description="Politica de uso das salas.", mime_type="text/markdown")
def politica_de_uso() -> str:
    return POLITICA_TEXTO


def main() -> None:
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "7301"))
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host=host)
    log(f"servidor MCP central-de-salas em http://{host}:{port}/mcp (politica {POLITICA_VERSAO})")
    uvicorn.run(RequestLogMiddleware(app, log), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
