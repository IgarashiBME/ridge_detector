#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FastAPI application factory.
"""

from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from server.routes_api import router as api_router
from server.routes_ws import router as ws_router


class NoCacheHTMLMiddleware(BaseHTTPMiddleware):
    """Set Cache-Control: no-cache for HTML responses."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(state, mode_manager, inference_thread, training_manager,
               evaluation_manager=None) -> FastAPI:
    app = FastAPI(title="Ridge Detector v2", version="2.0.0")

    # Middleware
    app.add_middleware(NoCacheHTMLMiddleware)

    # Store references in app state
    app.state.shared_state = state
    app.state.mode_manager = mode_manager
    app.state.inference_thread = inference_thread
    app.state.training_manager = training_manager
    app.state.evaluation_manager = evaluation_manager

    # API routes
    app.include_router(api_router, prefix="/api")
    app.include_router(ws_router)

    # Serve PWA static files
    web_dir = Path(__file__).parent.parent / "web"
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    return app
