"""Configuración de la app, leída desde variables de entorno / archivo .env.

Acá van solo los datos que cambian de una instalación a otra y los secretos
(URL de la base, credenciales de LinkedIn, zona horaria...).
Los ajustes que el usuario edita desde el panel (días, hora, modelo...) NO van acá:
viven en la tabla `settings` de la base.
"""
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Este archivo está en backend/app/core/config.py -> la raíz del proyecto está 3 carpetas arriba.
# Así el .env se encuentra aunque arranques la app desde otra carpeta.
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",  # el .env también tiene POSTGRES_USER, etc. para Docker; acá no los usamos
    )

    # --- Servidor: por defecto solo escucha en tu propia máquina ---
    host: str = "127.0.0.1"
    port: int = 8000

    # --- Base de datos ---
    database_url: str = "postgresql+psycopg://linkedin:linkedin@127.0.0.1:5432/linkedin_agent"

    # --- Fechas ---
    timezone: str = "America/Argentina/Buenos_Aires"

    # --- Ollama (modelo local) ---
    # 127.0.0.1 y no "localhost": en Windows localhost puede resolver primero a IPv6 y colgarse.
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.1:8b"

    # --- LinkedIn (opcional) ---
    linkedin_client_id: str = ""
    linkedin_client_secret: str = ""
    linkedin_redirect_uri: str = "http://localhost:8000/auth/linkedin/callback"
    # Versión de la API de LinkedIn (header Linkedin-Version, formato AAAAMM). LinkedIn da de baja cada versión
    # al año de salir: hay que subirla una vez por año. El panel avisa cuando tiene más de 10 meses.
    linkedin_api_version: str = "202610"

    # --- Scheduler ---
    # Si un post quedó atrasado más que esto, no se publica solo: pasa a "ready".
    max_late_hours: int = 12
    # False = no arranca el scheduler (no se publica nada solo). Lo usan los tests y sirve para depurar.
    scheduler_enabled: bool = True

    @field_validator("timezone")
    @classmethod
    def timezone_must_exist(cls, value: str) -> str:
        """Falla al arrancar (y no a las 9 AM de un martes) si la zona horaria está mal escrita."""
        try:
            ZoneInfo(value)
        except Exception as exc:  # ZoneInfoNotFoundError, ValueError...
            raise ValueError(f"TIMEZONE inválida: '{value}'. Ejemplo: America/Argentina/Buenos_Aires") from exc
        return value

    @field_validator("linkedin_api_version")
    @classmethod
    def api_version_must_be_yyyymm(cls, value: str) -> str:
        month = value[4:6]
        if not (len(value) == 6 and value.isdigit() and "01" <= month <= "12"):
            raise ValueError(f"LINKEDIN_API_VERSION inválida: '{value}'. Tiene que ser AAAAMM, por ejemplo 202610")
        return value

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    """Se crea una sola vez y se reutiliza (lru_cache). En tests se puede limpiar con get_settings.cache_clear()."""
    return Settings()
