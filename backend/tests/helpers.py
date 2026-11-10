"""Piezas para armar tests: clientes de Ollama y LinkedIn con transporte HTTP SIMULADO (httpx.MockTransport).

Con MockTransport no hay red: cada request que haría el cliente llega a una función `handler(request)` del test,
que devuelve la respuesta que queramos (o lanza un error de conexión/timeout). Se prueba el cliente REAL
(armado de requests, headers, interpretación de respuestas) y solo se reemplaza el otro lado del cable.
"""
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from app.integrations.linkedin.client import LinkedInClient
from app.shared.llm.ollama import OllamaClient

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
Handler = Callable[[httpx.Request], httpx.Response]


def ollama_client(handler: Handler, base_url: str = "http://ollama.test") -> OllamaClient:
    return OllamaClient(base_url, httpx.Client(transport=httpx.MockTransport(handler)))


def ollama_says(text: str) -> Handler:
    """Handler que responde como Ollama a /api/chat con ese texto."""
    return lambda request: httpx.Response(200, json={"message": {"role": "assistant", "content": text}})


def linkedin_client(
    handler: Handler,
    api_version: str = "202610",
    client_id: str = "client-id",
    client_secret: str = "client-secret",
) -> LinkedInClient:
    return LinkedInClient(
        client_id,
        client_secret,
        "http://localhost:8000/auth/linkedin/callback",
        api_version,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )


class LinkedInCalls:
    """Handler de LinkedIn que cuenta cuántas veces se llamó a /rest/posts y qué se mandó.

    `respond` decide la respuesta: una función request -> Response, o que lance un error de httpx.
    Por defecto contesta 201 con el id del post en el header x-restli-id, como la API real.
    """

    def __init__(self, respond: Handler | None = None):
        self.respond = respond or (lambda request: httpx.Response(201, headers={"x-restli-id": "urn:li:share:123"}))
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)

    @property
    def count(self) -> int:
        return len(self.requests)


def future_expiry(days: int = 30) -> str:
    """Vencimiento del permiso en el formato que guarda la app (ISO con offset)."""
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="minutes")
