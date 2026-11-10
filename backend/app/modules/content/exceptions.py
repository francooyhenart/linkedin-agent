"""Errores propios del módulo content.

Cada uno hereda de una base genérica (core/exceptions.py), que es la que main.py sabe traducir a HTTP.
"""
from app.core.exceptions import InvalidInputError, NotFoundError


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


class NoPublishDaysError(InvalidInputError):
    default_message = "Elegí al menos un día de publicación en Ajustes."


class InvalidSettingError(InvalidInputError):
    default_message = "Ajuste inválido."
