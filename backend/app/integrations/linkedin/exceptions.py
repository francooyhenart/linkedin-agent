"""Errores del cliente de LinkedIn.

La distinción importante está en las dos subclases de abajo, porque de ella depende no duplicar posts:

- LinkedInRejectedError:       LinkedIn contestó que NO (respuesta 4xx, salvo 408 y 409). El post definitivamente no se
                               publicó -> es seguro marcarlo como "failed" y dejar que el usuario lo reintente.
- LinkedInOutcomeUnknownError: no sabemos qué pasó (se cortó la conexión, venció el tiempo, error 5xx, 408 o 409).
                               Puede que LinkedIn SÍ haya publicado. Reintentar solo podría duplicar el post, así que
                               el post queda en "publishing" y hay que verificar a mano en LinkedIn.
"""
from app.core.exceptions import ExternalServiceError


class LinkedInError(ExternalServiceError):
    """Fallo general al hablar con LinkedIn (por ejemplo durante el login). Sin información sobre publicación."""


class LinkedInRejectedError(LinkedInError):
    """LinkedIn rechazó el pedido (respuesta 4xx): el post NO se publicó."""


class LinkedInOutcomeUnknownError(LinkedInError):
    """No se sabe si LinkedIn publicó el post (timeout, error de conexión o 5xx)."""
