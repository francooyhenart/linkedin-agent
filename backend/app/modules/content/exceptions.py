"""Errores propios del módulo content.

Cada uno hereda de una base genérica (core/exceptions.py), que es la que main.py sabe traducir a HTTP.
"""
from app.core.exceptions import ExternalServiceError, InvalidInputError, NotFoundError


class PostNotFoundError(NotFoundError):
    default_message = "Ese post no existe."


class IdeaNotFoundError(NotFoundError):
    default_message = "Esa idea no existe."


class InvalidDateError(InvalidInputError):
    default_message = "Fecha inválida. Usá el formato AAAA-MM-DDTHH:MM."


class EmptyIdeaError(InvalidInputError):
    default_message = "La idea está vacía."


class NothingToGenerateFromError(InvalidInputError):
    default_message = "Escribí una nota con la idea primero."


class NoUnusedIdeasError(InvalidInputError):
    default_message = "No hay ideas sin usar. Cargá algunas en Ideas."


class NoIdeasToSuggestFromError(InvalidInputError):
    default_message = "Primero anotá al menos una idea: las sugerencias salen de tu banco de ideas."


class NoPublishDaysError(InvalidInputError):
    default_message = "Elegí al menos un día de publicación en Ajustes."


class InvalidSettingError(InvalidInputError):
    default_message = "Ajuste inválido."


class NoUsefulSuggestionsError(ExternalServiceError):
    default_message = "El modelo no devolvió sugerencias que sirvan. Probá de nuevo."


class ReservedStatusError(InvalidInputError):
    default_message = "El estado «publishing» lo maneja la app y no se puede elegir a mano."


# ---- publicación y conexión con LinkedIn
class EmptyPostError(InvalidInputError):
    default_message = "El post está vacío."


class PostAlreadyPublishedError(InvalidInputError):
    default_message = "Ese post ya está publicado."


class PostBeingPublishedError(InvalidInputError):
    default_message = (
        "Este post ya se está publicando o quedó sin confirmar. "
        "Verificá en LinkedIn si salió antes de volver a intentarlo."
    )


class LinkedInNotConnectedError(InvalidInputError):
    default_message = "Conectá LinkedIn en Ajustes para publicar desde acá."


class LinkedInNotConfiguredError(InvalidInputError):
    default_message = "Falta LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET en el archivo .env"


class InvalidOAuthStateError(InvalidInputError):
    default_message = "Estado OAuth inválido. Volvé a intentar desde Ajustes."


class LinkedInAuthorizationDeniedError(InvalidInputError):
    default_message = "LinkedIn rechazó la conexión."
