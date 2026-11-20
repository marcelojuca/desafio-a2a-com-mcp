"""Middleware ASGI que registra no stderr cada request JSON-RPC recebido.

Fica por fora do app do SDK para registrar tambem os requests que o SDK recusa
antes de chegar ao handler (por exemplo, _meta sem os campos obrigatorios).
Registra metodo, id, traceparent do _meta e, em tools/call, o nome da tool.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


class RequestLogMiddleware:
    def __init__(self, app: Any, log: Callable[[str], None]) -> None:
        self.app = app
        self.log = log

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        corpo = b""
        mensagens: list[dict] = []
        while True:
            msg = await receive()
            mensagens.append(msg)
            if msg["type"] != "http.request":
                break
            corpo += msg.get("body", b"")
            if not msg.get("more_body", False):
                break
        self._registrar(corpo)

        fila = iter(mensagens)

        async def replay() -> dict:
            try:
                return next(fila)
            except StopIteration:
                return await receive()

        await self.app(scope, replay, send)

    def _registrar(self, corpo: bytes) -> None:
        try:
            dados = json.loads(corpo)
        except ValueError:
            self.log("[mcp] request com corpo invalido")
            return
        for item in dados if isinstance(dados, list) else [dados]:
            if not isinstance(item, dict):
                continue
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
            alvo = params.get("name") or params.get("uri")
            partes = [
                f"method={item.get('method')}",
                f"id={item.get('id')}",
                f"traceparent={meta.get('traceparent', '-')}",
            ]
            if alvo:
                partes.append(f"name={alvo}")
            if "requestState" in params:
                partes.append("retry=sim")
            self.log("[mcp] " + " ".join(partes))
