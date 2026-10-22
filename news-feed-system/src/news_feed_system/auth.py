"""Authentication module with abstract interface and in-memory
implementation.
"""

import abc
from typing import Optional


class Authenticator(abc.ABC):
    """Abstract interface for token validation."""

    @abc.abstractmethod
    async def validate_token(self, token: str) -> Optional[str]:
        """Validate an auth token and return the associated user_id.

        Args:
            token: The authentication token to validate.

        Returns:
            The user_id associated with the token, or None if invalid.
        """


class InMemoryAuthenticator(Authenticator):
    """In-memory token store for testing and development."""

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}  # token -> user_id

    def register_token(self, token: str, user_id: str) -> None:
        """Register a token for a user."""
        self._tokens[token] = user_id

    def revoke_token(self, token: str) -> None:
        """Revoke a token."""
        self._tokens.pop(token, None)

    async def validate_token(self, token: str) -> Optional[str]:
        """Validate token against the in-memory store."""
        return self._tokens.get(token)
