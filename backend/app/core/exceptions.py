"""Excepciones de dominio base, compartidas por todos los módulos.

Los services lanzan estas excepciones cuando algo no se puede hacer (no existe, dato inválido,
falló un servicio externo). NO saben nada de HTTP: quien las traduce a códigos de estado es
el exception handler de main.py. Así el mismo service podría usarse desde un script o un test
sin FastAPI.
"""


class AppError(Exception):
    """Base de todos los errores esperables de la app. Lleva un mensaje apto para mostrar al usuario."""

    default_message = "Ocurrió un error."

    def __init__(self, message: str | None = None):
        self.message = message or self.default_message
        super().__init__(self.message)


class NotFoundError(AppError):
    """Lo que se pidió no existe."""


class InvalidInputError(AppError):
    """El pedido tiene formato válido pero no tiene sentido (fecha inválida, nota vacía...)."""


class ExternalServiceError(AppError):
    """Falló algo externo (Ollama, LinkedIn)."""
