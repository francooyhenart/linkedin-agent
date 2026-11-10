"""Escritor real de posts: adapta el cliente de Ollama a la interfaz `PostWriter` que pide el service."""
from app.shared.llm.ollama import LLMStatus, OllamaClient


class OllamaPostWriter:
    def __init__(self, client: OllamaClient):
        self.client = client

    def write(self, system: str, user: str, model: str) -> str:
        return self.client.chat(model=model, system=system, user=user)

    def status(self) -> LLMStatus:
        return self.client.status()
