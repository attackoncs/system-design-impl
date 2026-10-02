"""Notification templates with named placeholder substitution.

Supports {variable_name} syntax for placeholders in both title and body.
Templates are registered by unique ID in a TemplateRegistry for reuse.
"""

from __future__ import annotations

import re
from typing import Optional

from notification_system.exceptions import TemplateRenderError


class NotificationTemplate:
    """Notification template with named placeholder substitution.

    Supports {variable_name} syntax for placeholders in both title and body.
    Templates are registered by unique ID for reuse.
    """

    PLACEHOLDER_PATTERN = re.compile(r"\{(\w+)\}")

    def __init__(self, template_id: str, title: str, body: str) -> None:
        self._template_id = template_id
        self._title = title
        self._body = body

    @property
    def template_id(self) -> str:
        return self._template_id

    @property
    def title_template(self) -> str:
        return self._title

    @property
    def body_template(self) -> str:
        return self._body

    def get_required_keys(self) -> set[str]:
        """Extract all placeholder names from title and body."""
        title_keys = set(self.PLACEHOLDER_PATTERN.findall(self._title))
        body_keys = set(self.PLACEHOLDER_PATTERN.findall(self._body))
        return title_keys | body_keys

    def render(self, params: dict[str, str]) -> tuple[str, str]:
        """Render the template with the given parameters.

        Args:
            params: Dictionary mapping placeholder names to values.

        Returns:
            Tuple of (rendered_title, rendered_body).

        Raises:
            TemplateRenderError: If required placeholders are missing.
        """
        required = self.get_required_keys()
        missing = required - set(params.keys())
        if missing:
            raise TemplateRenderError(self._template_id, sorted(missing))

        rendered_title = self.PLACEHOLDER_PATTERN.sub(
            lambda m: params[m.group(1)], self._title
        )
        rendered_body = self.PLACEHOLDER_PATTERN.sub(
            lambda m: params[m.group(1)], self._body
        )
        return rendered_title, rendered_body


class TemplateRegistry:
    """Registry for notification templates, keyed by template ID."""

    def __init__(self) -> None:
        self._templates: dict[str, NotificationTemplate] = {}

    def register(self, template: NotificationTemplate) -> None:
        """Register a template by its ID."""
        self._templates[template.template_id] = template

    def get(self, template_id: str) -> Optional[NotificationTemplate]:
        """Retrieve a template by ID. Returns None if not found."""
        return self._templates.get(template_id)

    def list_ids(self) -> list[str]:
        """List all registered template IDs."""
        return list(self._templates.keys())
