"""Cliente MCP do agente (o lado "host" da ponte).

Fala Streamable HTTP com o servidor MCP como um cliente de verdade, sem sessao:
cada request carrega no `_meta` a versao do protocolo, o clientInfo, as
capabilities (elicitation em form mode) e o traceparent da Task, e carrega nos
headers o espelho que o transporte exige (MCP-Protocol-Version, Mcp-Method e,
em tools/call e resources/read, Mcp-Name).

O cliente nao responde elicitation por conta propria: um `input_required` volta
cru para quem chamou, que decide o que fazer com ele (no agente, pausar a Task).
Cada request recebe um id JSON-RPC novo, de um contador do processo, entao o
retry de um MRTR nunca reaproveita o id do request inicial.
"""

from __future__ import annotations

import itertools
import json
import re
import secrets
import sys
from typing import Any

import httpx

PROTOCOLO = "2026-07-28"
CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}
CAPABILITIES = {"elicitation": {"form": {}}}
_TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def trace_id_de(traceparent: str | None) -> str | None:
    """Extrai o trace-id de um header traceparent W3C valido, ou None."""
    m = _TRACEPARENT.match((traceparent or "").strip().lower())
    if not m or m.group(1) == "ff" or set(m.group(2)) == {"0"} or set(m.group(3)) == {"0"}:
        return None
    return m.group(2)


def novo_traceparent(trace_id: str) -> str:
    """Mesmo trace-id, span-id novo: cada request MCP e um span filho da chamada A2A."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


class McpErro(Exception):
    """Erro de protocolo (campo `error` do JSON-RPC) devolvido pelo servidor MCP."""

    def __init__(self, code: int, message: str, data: Any = None, http_status: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data
        self.http_status = http_status


class McpCliente:
    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self.url = url
        self._ids = itertools.count(1)
        self._http = httpx.AsyncClient(timeout=timeout)

    async def fechar(self) -> None:
        await self._http.aclose()

    async def request(self, metodo: str, params: dict[str, Any], *, trace_id: str | None) -> dict[str, Any]:
        """Envia um request MCP e devolve o `result` cru (inclusive `input_required`)."""
        meta: dict[str, Any] = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
            "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
            "io.modelcontextprotocol/clientCapabilities": CAPABILITIES,
        }
        if trace_id:
            meta["traceparent"] = novo_traceparent(trace_id)
        id_ = next(self._ids)
        corpo = {"jsonrpc": "2.0", "id": id_, "method": metodo, "params": {**params, "_meta": meta}}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOLO,
            "Mcp-Method": metodo,
        }
        nome = params.get("name") if metodo == "tools/call" else params.get("uri") if metodo == "resources/read" else None
        if nome:
            headers["Mcp-Name"] = nome

        log(f"[agente->mcp] method={metodo} id={id_} traceparent={meta.get('traceparent', '-')}" + (f" name={nome}" if nome else ""))
        resp = await self._http.post(self.url, json=corpo, headers=headers)
        mensagem = _ler_resposta(resp)
        if "error" in mensagem:
            e = mensagem["error"] or {}
            raise McpErro(e.get("code", 0), e.get("message", ""), e.get("data"), resp.status_code)
        return mensagem.get("result") or {}

    # Atalhos para os tres metodos que o agente usa.

    async def listar_tools(self, *, trace_id: str | None) -> list[dict[str, Any]]:
        return (await self.request("tools/list", {}, trace_id=trace_id)).get("tools", [])

    async def ler_resource(self, uri: str, *, trace_id: str | None) -> str:
        resultado = await self.request("resources/read", {"uri": uri}, trace_id=trace_id)
        return "".join(c.get("text", "") for c in resultado.get("contents", []))

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        *,
        trace_id: str | None,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": nome, "arguments": argumentos}
        if input_responses is not None:
            params["inputResponses"] = input_responses
        if request_state is not None:
            params["requestState"] = request_state
        return await self.request("tools/call", params, trace_id=trace_id)


def _ler_resposta(resp: httpx.Response) -> dict[str, Any]:
    """Aceita resposta JSON direta ou um stream SSE com a mensagem JSON-RPC."""
    tipo = resp.headers.get("content-type", "")
    if tipo.startswith("text/event-stream"):
        for linha in resp.text.splitlines():
            if linha.startswith("data:"):
                dado = json.loads(linha[5:].strip())
                if isinstance(dado, dict) and ("result" in dado or "error" in dado):
                    return dado
        raise McpErro(-32603, "stream SSE sem resposta JSON-RPC", http_status=resp.status_code)
    try:
        return resp.json()
    except ValueError:
        raise McpErro(-32603, f"resposta nao-JSON do servidor MCP (HTTP {resp.status_code})", http_status=resp.status_code) from None
