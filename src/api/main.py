"""ASGI entrypoint — exposes the `app` instance for uvicorn.

Usage:
    uvicorn src.api.main:app --reload --port 8000
"""

from src.api.routes import create_app

app = create_app()
