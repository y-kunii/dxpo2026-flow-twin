"""FastAPI control-plane service.

The factory import is lazy so compatibility shims can import route/feature
contracts without constructing the ASGI application or creating a cycle.
"""

from __future__ import annotations

from typing import Any


def create_app(*args: Any, **kwargs: Any) -> Any:
    from .application import create_app as factory

    return factory(*args, **kwargs)


__all__ = ["create_app"]
