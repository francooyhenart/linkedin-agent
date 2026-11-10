"""Permite arrancar con `python -m app` usando HOST y PORT del .env (por defecto 127.0.0.1:8000)."""
import uvicorn

from app.core.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)
