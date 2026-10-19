"""Expose the application and its factory to Uvicorn and legacy imports."""

from app.core.factory import create_app, lifespan


app = create_app()
