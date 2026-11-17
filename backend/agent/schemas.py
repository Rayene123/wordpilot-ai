"""Compatibility exports for the shared document operation schemas."""

from backend.operation_schemas import (
    DocumentOperation,
    InsertOperation,
    RemoveOperation,
    ReplaceOperation,
)

__all__ = ["DocumentOperation", "InsertOperation", "RemoveOperation", "ReplaceOperation"]
