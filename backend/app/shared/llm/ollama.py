"""Cliente de Ollama (modelo de lenguaje local). Lo van a usar varios módulos (content ahora, jobs más adelante).

Es lo ÚNICO que habla HTTP con Ollama. Los services nunca hacen requests directos: le piden cosas a este cliente.
Todos los fallos salen como `ExternalServiceError` con un mensaje en castellano, apto para mostrar en el panel.

Este archivo no importa nada de `modules/`: shared/ no puede depender de los módulos.
"""
from dataclasses import dataclass
from functools import lru_cache

import httpx

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError

CONNECT_TIMEOUT = 5.0  # si Ollama no está abierto, se sabe enseguida
READ_TIMEOUT = 300.0  # generar texto con un modelo local puede tardar mucho
STATUS_TIMEOUT = 3.0


@dataclass(frozen=True)
class LLMStatus:
    ok: bool
    models: list[str]


class OllamaClient:
    def __init__(self, base_url: str, http: httpx.Client | None = None):
        """`http` se puede inyectar: los tests pasan un cliente con MockTransport y así no hace falta Ollama."""
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT))

    def chat(self, model: str, system: str, user: str) -> str:
        """Una pregunta, una respuesta (sin streaming). Devuelve el texto crudo del modelo."""
        try:
            response = self._http.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": model,
                    "stream": False,
                    "options": {"temperature": 0.7},
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                },
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise ExternalServiceError(f"No pude hablar con Ollama en {self.base_url}. ¿Está abierto?") from None
        except httpx.TimeoutException:
            raise ExternalServiceError(
                f"Ollama tardó más de {int(READ_TIMEOUT)} segundos en responder. "
                "Probá con un modelo más chico o con un texto más corto."
            ) from None
        except httpx.HTTPError:
            raise ExternalServiceError("Se cortó la conexión con Ollama a mitad de la respuesta.") from None

        if response.status_code == 404:
            raise ExternalServiceError(f"Ollama no tiene el modelo '{model}'. Corré: ollama pull {model}")
        if not response.is_success:
            raise ExternalServiceError(f"Ollama respondió {response.status_code}: {response.text[:200]}")

        try:
            content = response.json()["message"]["content"]
        except (ValueError, KeyError, TypeError):
            raise ExternalServiceError("Ollama devolvió una respuesta que no pude entender.") from None
        if not isinstance(content, str):
            raise ExternalServiceError("Ollama devolvió una respuesta que no pude entender.")
        return content

    def status(self) -> LLMStatus:
        """¿Está Ollama abierto y qué modelos tiene? Nunca lanza: si algo falla, ok=False."""
        try:
            response = self._http.get(f"{self.base_url}/api/tags", timeout=httpx.Timeout(STATUS_TIMEOUT))
            response.raise_for_status()
            return LLMStatus(ok=True, models=[m["name"] for m in response.json().get("models", [])])
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return LLMStatus(ok=False, models=[])


@lru_cache
def get_ollama_client() -> OllamaClient:
    """Un solo cliente para toda la app (reutiliza conexiones). Los tests lo reemplazan con dependency_overrides."""
    return OllamaClient(get_settings().ollama_url)
