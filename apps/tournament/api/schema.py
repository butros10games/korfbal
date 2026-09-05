"""Schema support for revision-checked DELETE commands with JSON bodies."""

from typing import Any

from drf_spectacular.openapi import AutoSchema
from drf_spectacular.plumbing import ComponentRegistry


class DeleteBodySchema(AutoSchema):
    """Retain declared command bodies that spectacular omits for DELETE methods."""

    def get_operation(
        self,
        path: str,
        path_regex: str,
        path_prefix: str,
        method: str,
        registry: ComponentRegistry,
    ) -> dict[str, Any] | None:
        """Include the existing input serializer for revision-checked deletions."""
        operation = super().get_operation(
            path, path_regex, path_prefix, method, registry
        )
        if operation is not None and method == "DELETE":
            serializer = self.get_request_serializer()
            if serializer is not None:
                component = self.resolve_serializer(serializer, "request")
                operation["requestBody"] = {
                    "required": bool((component.schema or {}).get("required")),
                    "content": {"application/json": {"schema": component.ref}},
                }
        return operation
