"""Cliente de LinkedIn: OAuth + publicación. Solo habla HTTP; NO toca la base de datos.

Dónde se guardan los tokens lo decide el módulo que lo usa (content), a través de su service.
Este archivo no importa nada de `modules/`.

API de publicación: Posts API (`POST /rest/posts`), el reemplazo oficial de `/v2/ugcPosts`.
Exige los headers `Linkedin-Version` (AAAAMM) y `X-Restli-Protocol-Version: 2.0.0`.

Seguridad: este cliente NUNCA escribe tokens en los logs, y los mensajes de error nunca los incluyen.
"""
import logging
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings
from app.integrations.linkedin.exceptions import (
    LinkedInError,
    LinkedInOutcomeUnknownError,
    LinkedInRejectedError,
)

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"
SCOPES = "openid profile w_member_social"

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 30.0

# Códigos con los que LinkedIn avisa que la versión del header no sirve.
VERSION_ERROR_CODES = {"VERSION_MISSING", "NONEXISTENT_VERSION"}

# Caracteres reservados del formato "little" que usa el campo `commentary`. Hay que escaparlos con \ aunque se
# quiera escribir texto común: https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/little-text-format
LITTLE_RESERVED = re.compile(r"([\\|{}@\[\]()<>#*_~])")


def escape_commentary(text: str) -> str:
    r"""Escapa los caracteres reservados para que LinkedIn muestre el texto tal cual lo escribió el usuario.

    Efecto: los hashtags y menciones salen como texto, sin link ("#devops" se ve como #devops, pero no es clickeable).
    """
    return LITTLE_RESERVED.sub(r"\\\1", text)


def api_version_age_months(version: str, today: date) -> int:
    """Antigüedad en meses de una versión AAAAMM. 202610 consultada en agosto de 2027 -> 10."""
    year, month = int(version[:4]), int(version[4:6])
    return (today.year - year) * 12 + (today.month - month)


@dataclass(frozen=True)
class TokenGrant:
    access_token: str
    expires_in: int  # segundos


@dataclass(frozen=True)
class Profile:
    sub: str  # id de la persona en LinkedIn
    name: str


@dataclass(frozen=True)
class PublishResult:
    post_urn: str | None
    url: str | None


class LinkedInClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        api_version: str,
        http: httpx.Client | None = None,
    ):
        """`http` se puede inyectar: los tests pasan un cliente con MockTransport y no se toca la red."""
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.redirect_uri = redirect_uri
        self.api_version = api_version
        self._http = http or httpx.Client(timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT))
        self._states: set[str] = set()  # "state" de OAuth: anti-CSRF, vive en memoria mientras corre la app
        self._states_lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # ------------------------------------------------------------------ OAuth
    def new_state(self) -> str:
        state = secrets.token_urlsafe(16)
        with self._states_lock:
            self._states.add(state)
        return state

    def consume_state(self, state: str) -> bool:
        """True si el state lo emitimos nosotros y todavía no se usó (y lo da de baja)."""
        with self._states_lock:
            if state in self._states:
                self._states.discard(state)
                return True
            return False

    def authorization_url(self, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "scope": SCOPES,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_code(self, code: str) -> TokenGrant:
        try:
            response = self._http.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
        except httpx.HTTPError:
            raise LinkedInError("No pude conectarme con LinkedIn para obtener el permiso. Probá de nuevo.") from None
        if not response.is_success:
            # No se incluye el cuerpo: podría repetir el código o el secreto que mandamos.
            raise LinkedInError(f"LinkedIn no me dio el permiso (respondió {response.status_code}). Volvé a intentar.")
        try:
            data = response.json()
            return TokenGrant(access_token=data["access_token"], expires_in=int(data.get("expires_in", 0)))
        except (ValueError, KeyError, TypeError):
            raise LinkedInError("LinkedIn devolvió una respuesta que no pude entender al pedir el permiso.") from None

    def fetch_profile(self, access_token: str) -> Profile:
        try:
            response = self._http.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError:
            raise LinkedInError("No pude conectarme con LinkedIn para leer tu perfil. Probá de nuevo.") from None
        if not response.is_success:
            raise LinkedInError(f"No pude leer tu perfil de LinkedIn (respondió {response.status_code}).")
        try:
            info = response.json()
            return Profile(sub=info["sub"], name=info.get("name", ""))
        except (ValueError, KeyError, TypeError):
            raise LinkedInError("LinkedIn devolvió un perfil que no pude entender.") from None

    # ------------------------------------------------------------------ publicación
    def publish_text(self, access_token: str, person_id: str, text: str) -> PublishResult:
        """Publica un post de solo texto. Qué se lanza si algo sale mal importa mucho:

        - LinkedInRejectedError:       respuesta 4xx -> NO se publicó.
        - LinkedInOutcomeUnknownError: timeout, error de conexión, 5xx o respuesta rara -> NO SABEMOS.
        - Si LinkedIn contesta 2xx, el post ya existe: este método NUNCA lanza después de eso
          (si faltara el id del post, devuelve url=None en lugar de un error).
        """
        try:
            response = self._http.post(
                POSTS_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-Restli-Protocol-Version": "2.0.0",
                    "Linkedin-Version": self.api_version,
                },
                json={
                    "author": f"urn:li:person:{person_id}",
                    "commentary": escape_commentary(text),
                    "visibility": "PUBLIC",
                    "distribution": {
                        "feedDistribution": "MAIN_FEED",
                        "targetEntities": [],
                        "thirdPartyDistributionChannels": [],
                    },
                    "lifecycleState": "PUBLISHED",
                    "isReshareDisabledByAuthor": False,
                },
            )
        except httpx.TimeoutException:
            raise LinkedInOutcomeUnknownError(
                "LinkedIn no respondió a tiempo. No sé si el post se publicó: "
                "verificá en LinkedIn antes de volver a intentarlo."
            ) from None
        except httpx.HTTPError:
            raise LinkedInOutcomeUnknownError(
                "Se cortó la conexión con LinkedIn. No sé si el post se publicó: "
                "verificá en LinkedIn antes de volver a intentarlo."
            ) from None

        status = response.status_code
        if 200 <= status < 300:
            urn = response.headers.get("x-restli-id")
            logger.info("LinkedIn aceptó el post (HTTP %s)", status)
            return PublishResult(post_urn=urn, url=f"https://www.linkedin.com/feed/update/{urn}/" if urn else None)
        if 400 <= status < 500:
            raise LinkedInRejectedError(self._rejection_message(response))
        # 5xx, y cualquier otra cosa inesperada (1xx, 3xx): no sabemos si llegó a publicarse.
        raise LinkedInOutcomeUnknownError(
            f"LinkedIn respondió {status}. No sé si el post se publicó: "
            "verificá en LinkedIn antes de volver a intentarlo."
        )

    def _rejection_message(self, response: httpx.Response) -> str:
        status = response.status_code
        try:
            code = response.json().get("code")
        except (ValueError, AttributeError):
            code = None
        if status == 426 or code in VERSION_ERROR_CODES:
            return (
                f"LinkedIn no acepta la versión de API '{self.api_version}' (LINKEDIN_API_VERSION). "
                "Revisá ese valor en tu .env y mirá el README para elegir una versión vigente."
            )
        if status == 401:
            return "LinkedIn rechazó el permiso (venció o fue revocado). Reconectá LinkedIn en Ajustes."
        if status == 403:
            return (
                "LinkedIn no te dejó publicar (permiso insuficiente). Revisá que tu app tenga el producto "
                "«Share on LinkedIn» (permiso w_member_social) y reconectá LinkedIn en Ajustes."
            )
        if status == 429:
            return "LinkedIn dice que hiciste demasiados pedidos (hay un límite diario). Probá más tarde."
        return f"LinkedIn rechazó el post (respondió {status}): {response.text[:300]}"


@lru_cache
def get_linkedin_client() -> LinkedInClient:
    """Un solo cliente para toda la app. Los tests lo reemplazan con dependency_overrides."""
    s = get_settings()
    return LinkedInClient(
        s.linkedin_client_id, s.linkedin_client_secret, s.linkedin_redirect_uri, s.linkedin_api_version
    )
