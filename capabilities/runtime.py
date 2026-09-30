"""Capability discovery and execution kept separate from the MCP service."""
from __future__ import annotations

from importlib import import_module

_CAPABILITIES = {"office": "capabilities.office"}


def discover() -> list[str]:
    return sorted(_CAPABILITIES)


def execute(name: str, operation: str, **arguments):
    module_name = _CAPABILITIES.get(name)
    if module_name is None:
        raise ValueError(f"unknown capability: {name}")
    module = import_module(module_name)
    handler = getattr(module, "OPERATIONS", {}).get(operation)
    if not callable(handler):
        raise ValueError(f"unknown operation: {name}.{operation}")
    return handler(**arguments)
