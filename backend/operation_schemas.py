"""Shared Pydantic schemas for supported document operations."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _OperationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReplaceOperation(_OperationSchema):
    operation_type: Literal["replace"]
    target_text: str = Field(min_length=1)
    replacement_text: str


class InsertOperation(_OperationSchema):
    operation_type: Literal["insert"]
    anchor_paragraph_text: str = Field(min_length=1)
    inserted_text: str
    position: Literal["before", "after"]
    paragraph_style: str | None = None
    location: tuple[int, ...] | None = None

    @field_validator("location")
    @classmethod
    def validate_location(cls, value: tuple[int, ...] | None) -> tuple[int, ...] | None:
        """Allow only body indexes or four-part table-cell paragraph indexes."""
        if value is not None and (len(value) not in (1, 4) or any(index < 0 for index in value)):
            raise ValueError("location must contain one or four non-negative indexes")
        return value


class RemoveOperation(_OperationSchema):
    operation_type: Literal["remove"]
    target_text: str = Field(min_length=1)
    location: tuple[int, ...] | None = None

    @field_validator("location")
    @classmethod
    def validate_location(cls, value: tuple[int, ...] | None) -> tuple[int, ...] | None:
        """Allow only body indexes or four-part table-cell paragraph indexes."""
        if value is not None and (len(value) not in (1, 4) or any(index < 0 for index in value)):
            raise ValueError("location must contain one or four non-negative indexes")
        return value


DocumentOperation: TypeAlias = Annotated[
    ReplaceOperation | InsertOperation | RemoveOperation,
    Field(discriminator="operation_type"),
]

__all__ = ["DocumentOperation", "InsertOperation", "RemoveOperation", "ReplaceOperation"]
