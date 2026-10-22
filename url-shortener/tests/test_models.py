"""Unit tests for data models (RedirectType, URLMapping, ClickRecord).

Validates: Requirements 1.5, 2.4, 6.1
"""

import dataclasses
import datetime
import enum

import pytest
import url_shortener.models


class TestRedirectType:
    """Tests for the RedirectType enum."""

    def test_permanent_value_is_301(self):
        assert url_shortener.models.RedirectType.PERMANENT == 301

    def test_temporary_value_is_302(self):
        assert url_shortener.models.RedirectType.TEMPORARY == 302

    def test_is_int_enum(self):
        """RedirectType is an IntEnum and can be used as an int."""
        assert isinstance(url_shortener.models.RedirectType.PERMANENT, enum.IntEnum)
        assert isinstance(url_shortener.models.RedirectType.TEMPORARY, enum.IntEnum)
        # Can be used directly as an integer
        assert url_shortener.models.RedirectType.PERMANENT + 0 == 301
        assert url_shortener.models.RedirectType.TEMPORARY + 0 == 302


class TestURLMapping:
    """Tests for the URLMapping frozen dataclass."""

    def test_creation_with_all_fields(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        mapping = url_shortener.models.URLMapping(
            short_code="abc1234",
            long_url="https://example.com/page",
            redirect_type=url_shortener.models.RedirectType.PERMANENT,
            created_at=now,
        )
        assert mapping.short_code == "abc1234"
        assert mapping.long_url == "https://example.com/page"
        assert mapping.redirect_type == url_shortener.models.RedirectType.PERMANENT
        assert mapping.created_at == now

    def test_defaults_redirect_type_to_temporary(self):
        mapping = url_shortener.models.URLMapping(
            short_code="abc1234",
            long_url="https://example.com",
        )
        assert mapping.redirect_type == url_shortener.models.RedirectType.TEMPORARY

    def test_defaults_created_at_auto_set(self):
        before = datetime.datetime.now(datetime.timezone.utc)
        mapping = url_shortener.models.URLMapping(
            short_code="abc1234",
            long_url="https://example.com",
        )
        after = datetime.datetime.now(datetime.timezone.utc)
        assert before <= mapping.created_at <= after

    def test_is_frozen_immutable(self):
        mapping = url_shortener.models.URLMapping(
            short_code="abc1234",
            long_url="https://example.com",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            mapping.short_code = "xyz9999"


class TestClickRecord:
    """Tests for the ClickRecord frozen dataclass."""

    def test_creation_with_all_fields(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        record = url_shortener.models.ClickRecord(
            short_code="abc1234",
            timestamp=now,
            client_id="user-42",
        )
        assert record.short_code == "abc1234"
        assert record.timestamp == now
        assert record.client_id == "user-42"

    def test_client_id_defaults_to_none(self):
        record = url_shortener.models.ClickRecord(short_code="abc1234")
        assert record.client_id is None

    def test_is_frozen_immutable(self):
        record = url_shortener.models.ClickRecord(short_code="abc1234")
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.short_code = "xyz9999"

    def test_timestamp_auto_set(self):
        before = datetime.datetime.now(datetime.timezone.utc)
        record = url_shortener.models.ClickRecord(short_code="abc1234")
        after = datetime.datetime.now(datetime.timezone.utc)
        assert before <= record.timestamp <= after
