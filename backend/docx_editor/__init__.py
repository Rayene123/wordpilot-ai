"""Safe inspection and text editing helpers for Word documents."""

from .inspection import DocumentSummary, ParagraphInfo, RunInfo, TableInfo, inspect_document
from .paragraph_operations import OperationResult, insert_paragraph, remove_paragraph
from .replacement import ReplacementResult, replace_text

__all__ = [
    "DocumentSummary", "ParagraphInfo", "RunInfo", "TableInfo",
    "OperationResult", "ReplacementResult", "inspect_document", "replace_text",
    "insert_paragraph", "remove_paragraph",
]
