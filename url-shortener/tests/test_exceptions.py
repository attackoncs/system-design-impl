"""Unit tests for the custom exception hierarchy.

Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5
"""

import pytest

from url_shortener.exceptions import (
    CollisionLimitExceededError,
    ShortCodeNotFoundError,
    URLShortenerError,
    URLValidationError,
)


class TestExceptionHierarchy:
    """Test that all custom exceptions inherit from URLShortenerError."""

    def test_url_shortener_error_inherits_from_exception(self):
        """URLShortenerError should be a subclass of Exception."""
        assert issubclass(URLShortenerError, Exception)

    def test_url_validation_error_inherits_from_url_shortener_error(self):
        """URLValidationError should be a subclass of URLShortenerError."""
        assert issubclass(URLValidationError, URLShortenerError)

    def test_short_code_not_found_error_inherits_from_url_shortener_error(self):
        """ShortCodeNotFoundError should be a subclass of URLShortenerError."""
        assert issubclass(ShortCodeNotFoundError, URLShortenerError)

    def test_collision_limit_exceeded_error_inherits_from_url_shortener_error(self):
        """CollisionLimitExceededError should be a subclass of URLShortenerError."""
        assert issubclass(CollisionLimitExceededError, URLShortenerError)

    def test_url_validation_error_inherits_from_exception(self):
        """URLValidationError should also be a subclass of Exception (transitive)."""
        assert issubclass(URLValidationError, Exception)

    def test_short_code_not_found_error_inherits_from_exception(self):
        """ShortCodeNotFoundError should also be a subclass of Exception (transitive)."""
        assert issubclass(ShortCodeNotFoundError, Exception)

    def test_collision_limit_exceeded_error_inherits_from_exception(self):
        """CollisionLimitExceededError should also be a subclass of Exception (transitive)."""
        assert issubclass(CollisionLimitExceededError, Exception)


class TestCatchAllBehavior:
    """Test that all exceptions can be caught with `except URLShortenerError`."""

    def test_catch_url_validation_error_as_base(self):
        """URLValidationError should be catchable as URLShortenerError."""
        with pytest.raises(URLShortenerError):
            raise URLValidationError("invalid url")

    def test_catch_short_code_not_found_error_as_base(self):
        """ShortCodeNotFoundError should be catchable as URLShortenerError."""
        with pytest.raises(URLShortenerError):
            raise ShortCodeNotFoundError("code not found")

    def test_catch_collision_limit_exceeded_error_as_base(self):
        """CollisionLimitExceededError should be catchable as URLShortenerError."""
        with pytest.raises(URLShortenerError):
            raise CollisionLimitExceededError("too many collisions")

    def test_catch_base_error_as_exception(self):
        """URLShortenerError should be catchable as Exception."""
        with pytest.raises(Exception):
            raise URLShortenerError("base error")


class TestDescriptiveMessages:
    """Test that all exceptions carry descriptive messages via str()."""

    def test_url_shortener_error_message(self):
        """URLShortenerError should carry the provided message."""
        msg = "something went wrong"
        err = URLShortenerError(msg)
        assert str(err) == msg

    def test_url_validation_error_message(self):
        """URLValidationError should carry a descriptive message."""
        msg = "URL cannot be empty"
        err = URLValidationError(msg)
        assert str(err) == msg

    def test_url_validation_error_detailed_message(self):
        """URLValidationError should support detailed messages about the failure."""
        msg = "URL missing scheme (http/https): 'example.com'"
        err = URLValidationError(msg)
        assert str(err) == msg
        assert "scheme" in str(err)

    def test_short_code_not_found_error_message(self):
        """ShortCodeNotFoundError should carry a descriptive message."""
        msg = "Short code 'abc1234' not found"
        err = ShortCodeNotFoundError(msg)
        assert str(err) == msg
        assert "abc1234" in str(err)

    def test_collision_limit_exceeded_error_message(self):
        """CollisionLimitExceededError should carry a descriptive message."""
        msg = "Failed to generate unique short code after 10 retries"
        err = CollisionLimitExceededError(msg)
        assert str(err) == msg
        assert "10" in str(err)

    def test_exceptions_with_empty_message(self):
        """Exceptions should work with empty messages too."""
        err = URLShortenerError("")
        assert str(err) == ""


class TestIssubclassChecks:
    """Explicit issubclass checks for the exception hierarchy."""

    def test_issubclass_url_shortener_error(self):
        assert issubclass(URLShortenerError, Exception)
        assert not issubclass(URLShortenerError, TypeError)

    def test_issubclass_url_validation_error(self):
        assert issubclass(URLValidationError, URLShortenerError)
        assert issubclass(URLValidationError, Exception)

    def test_issubclass_short_code_not_found_error(self):
        assert issubclass(ShortCodeNotFoundError, URLShortenerError)
        assert issubclass(ShortCodeNotFoundError, Exception)

    def test_issubclass_collision_limit_exceeded_error(self):
        assert issubclass(CollisionLimitExceededError, URLShortenerError)
        assert issubclass(CollisionLimitExceededError, Exception)

    def test_sibling_exceptions_are_not_subclasses_of_each_other(self):
        """Sibling exceptions should not be subclasses of each other."""
        assert not issubclass(URLValidationError, ShortCodeNotFoundError)
        assert not issubclass(URLValidationError, CollisionLimitExceededError)
        assert not issubclass(ShortCodeNotFoundError, URLValidationError)
        assert not issubclass(ShortCodeNotFoundError, CollisionLimitExceededError)
        assert not issubclass(CollisionLimitExceededError, URLValidationError)
        assert not issubclass(CollisionLimitExceededError, ShortCodeNotFoundError)
