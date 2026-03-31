#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Uvicorn background thread runner for FastAPI.
"""

import threading

import uvicorn

from server.app import create_app


def start_server(state, mode_manager, inference_thread, training_manager,
                 evaluation_manager=None, host="0.0.0.0", port=8000):
    """Start uvicorn in a daemon thread."""
    app = create_app(state, mode_manager, inference_thread, training_manager,
                     evaluation_manager=evaluation_manager)

    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return thread
