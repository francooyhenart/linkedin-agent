"""Fakes simples de `PostWriter` para los tests de services (no hacen red).

Los tests de la capa HTTP usan los clientes reales con transporte simulado (ver tests/helpers.py);
estos fakes son para probar la lógica del service sin pasar por el cliente.
"""
from collections.abc import Callable

from app.shared.llm.ollama import LLMStatus


class NoWriter:
    """Para tests donde no se debería llamar nunca al modelo."""

    def write(self, system: str, user: str, model: str) -> str:
        raise AssertionError("no se esperaba llamar al modelo")

    def status(self) -> LLMStatus:
        return LLMStatus(ok=True, models=[])


class ScriptedWriter:
    """Devuelve (o lanza) lo que diga `script(call_number, user_prompt)`. Guarda cada llamada para inspeccionarla."""

    def __init__(self, script: Callable[[int, str], str] | None = None):
        self.script = script or (lambda n, user: f"Draft number {n}. Plain text.")
        self.calls: list[dict] = []

    def write(self, system: str, user: str, model: str) -> str:
        self.calls.append({"system": system, "user": user, "model": model})
        return self.script(len(self.calls), user)

    def status(self) -> LLMStatus:
        return LLMStatus(ok=True, models=["fake:1"])
