"""Unit tests for data models (RedirectType, URLMapping, ClickRecord).

Validates: Requirements 1.5, 2.4, 6.1
"""

import dataclasses
from datetime import datetime, timezone
from enum import IntEnum

import pytest

from url_shortener.models import ClickRecord, RedirectType, URLMapping


class TestRedirectType:
    """Tests for the RedirectType enum."""

    def test_permanent_value_is_301(self):
        assert RedirectType.PERMANENT == 301

    def test_temporary_value_is_302(self):
        assert RedirectType.TEMPORARY == 302

    def test_is_int_enum(self):
        """RedirectType is an IntEnum and can be used as an int."""
        assert isinstance(RedirectType.PERMANENT, IntEnum)
        assert isinstance(RedirectType.TEMPORARY, IntEnum)
        # Can be used directly as an integer
        assert RedirectType.PERMANENT + 0 == 301
        assert RedirectType.TEMPORARY + 0 == 302


class TestURLMapping:
    """Tests for the URLMapping frozen dataclass."""

    def test_creation_with_all_fields(self):
        now = datetime.now(timezone.utc)
        mapping = URLMapping(
            short_code="abc1234",
            long_url="https://example.com/page",
            redirect_type=RedirectType.PERMANENT,
            created_at=now,
        )
        assert mapping.short_code == "abc1234"
        assert mapping.long_url == "https://example.com/page"
        assert mapping.redirect_type == RedirectType.PERMANENT
        assert mapping.created_at == now

    def test_defaults_redirect_type_to_temporary(self):
        mapping = URLMapping(
            short_code="abc1234",
            long_url="https://example.com",
        )
        assert mapping.redirect_type == RedirectType.TEMPORARY

    def test_defaults_created_at_auto_set(self):
        before = datetime.now(timezone.utc)
        mapping = URLMapping(
            short_code="abc1234",
            long_url="https://example.com",
        )
        after = datetime.now(timezone.utc)
        assert before <= mapping.created_at <= after

    def test_is_frozen_immutable(self):
        mapping = URLMapping(
            short_code="abc1234",
            long_url="https://example.com",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            mapping.short_code = "xyz9999"


class TestClickRecord:
    """Tests for the ClickRecord frozen dataclass."""

    def test_creation_with_all_fields(self):
        now = datetime.now(timezone.utc)
        record = ClickRecord(
            short_code="abc1234",
            timestamp=now,
            client_id="user-42",
        )
        assert record.short_code == "abc1234"
        assert record.timestamp == now
        assert record.client_id == "user-42"

    def test_client_id_defaults_to_none(self):
        record = ClickRecord(short_code="abc1234")
        assert record.client_id is None

    def test_is_frozen_immutable(self):
        record = ClickRecord(short_code="abc1234")
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.short_code = "xyz9999"

    def test_timestamp_auto_set(self):
        before = datetime.now(timezone.utc)
        record = ClickRecord(short_code="abc1234")
        after = datetime.now(timezone.utc)
        assert before <= record.timestamp <= after
