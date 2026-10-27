"""Compatibility import for the framework-independent control-plane routes."""

from services.control_api.routes import dispatch_preview_api

__all__ = ["dispatch_preview_api"]
