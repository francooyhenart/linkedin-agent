"""Escritor falso, TEMPORAL (fase 3): devuelve siempre el mismo texto, sin llamar a ningún modelo.

Sirve para probar generate y plan antes de tener Ollama. En la fase 4 se reemplaza en
dependencies.py por el escritor real y este archivo se elimina (los tests tendrán sus propios fakes).
"""


class FakePostWriter:
    def write(self, system: str, user: str, model: str) -> str:
        return "Draft from the fake writer. Same text every time, no model involved."
