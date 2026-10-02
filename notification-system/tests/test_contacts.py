"""Unit tests for ContactInfoStore.

Tests add/remove device tokens, resolve endpoint, multiple tokens.
Requirements: 11.1, 11.2, 11.3, 11.4, 11.5
"""

import pytest

from notification_system.contacts import ContactInfoStore
from notification_system.models import Channel, ContactInfo


class TestContactInfoStoreGetSetRemove:
    """Tests for basic CRUD operations on ContactInfoStore."""

    def test_get_returns_none_for_unknown_user(self):
        store = ContactInfoStore()
        assert store.get("unknown") is None

    def test_set_and_get_contact(self):
        store = ContactInfoStore()
        contact = ContactInfo(user_id="u1", email="user@example.com", phone="+1234567890")
        store.set(contact)
        result = store.get("u1")
        assert result is not None
        assert result.user_id == "u1"
        assert result.email == "user@example.com"
        assert result.phone == "+1234567890"

    def test_set_overwrites_existing_contact(self):
        store = ContactInfoStore()
        store.set(ContactInfo(user_id="u1", email="old@example.com"))
        store.set(ContactInfo(user_id="u1", email="new@example.com"))
        result = store.get("u1")
        assert result.email == "new@example.com"

    def test_remove_existing_user(self):
        store = ContactInfoStore()
        store.set(ContactInfo(user_id="u1", email="user@example.com"))
        store.remove("u1")
        assert store.get("u1") is None

    def test_remove_nonexistent_user_is_noop(self):
        store = ContactInfoStore()
        store.remove("nonexistent")  # Should not raise


class TestContactInfoStoreDeviceTokens:
    """Tests for device token management (Requirement 11.2, 11.4, 11.5)."""

    def test_add_device_token_creates_contact_if_missing(self):
        store = ContactInfoStore()
        store.add_device_token("u1", "token_abc")
        contact = store.get("u1")
        assert contact is not None
        assert contact.device_tokens == ["token_abc"]

    def test_add_multiple_device_tokens(self):
        store = ContactInfoStore()
        store.add_device_token("u1", "token_1")
        store.add_device_token("u1", "token_2")
        store.add_device_token("u1", "token_3")
        contact = store.get("u1")
        assert contact.device_tokens == ["token_1", "token_2", "token_3"]

    def test_add_duplicate_token_is_ignored(self):
        store = ContactInfoStore()
        store.add_device_token("u1", "token_1")
        store.add_device_token("u1", "token_1")
        contact = store.get("u1")
        assert contact.device_tokens == ["token_1"]

    def test_remove_device_token(self):
        store = ContactInfoStore()
        store.set(ContactInfo(user_id="u1", device_tokens=["tok1", "tok2", "tok3"]))
        store.remove_device_token("u1", "tok2")
        contact = store.get("u1")
        assert contact.device_tokens == ["tok1", "tok3"]

    def test_remove_device_token_nonexistent_user_is_noop(self):
        store = ContactInfoStore()
        store.remove_device_token("unknown", "token")  # Should not raise

    def test_remove_device_token_not_in_list_is_noop(self):
        store = ContactInfoStore()
        store.set(ContactInfo(user_id="u1", device_tokens=["tok1"]))
        store.remove_device_token("u1", "nonexistent")
        contact = store.get("u1")
        assert contact.device_tokens == ["tok1"]


class TestContactInfoStoreResolveEndpoint:
    """Tests for resolve_endpoint (Requirement 11.3)."""

    def test_resolve_email(self):
        store = ContactInfoStore()
        store.set(ContactInfo(user_id="u1", email="user@example.com"))
        assert store.resolve_endpoint("u1", Channel.EMAIL) == "user@example.com"

    def test_resolve_sms(self):
        store = ContactInfoStore()
        store.set(ContactInfo(user_id="u1", phone="+1234567890"))
        assert store.resolve_endpoint("u1", Channel.SMS) == "+1234567890"

    def test_resolve_ios_push_returns_first_device_token(self):
        store = ContactInfoStore()
        store.set(ContactInfo(user_id="u1", device_tokens=["ios_tok1", "ios_tok2"]))
        assert store.resolve_endpoint("u1", Channel.IOS_PUSH) == "ios_tok1"

    def test_resolve_android_push_returns_first_device_token(self):
        store = ContactInfoStore()
        store.set(ContactInfo(user_id="u1", device_tokens=["android_tok1", "android_tok2"]))
        assert store.resolve_endpoint("u1", Channel.ANDROID_PUSH) == "android_tok1"

    def test_resolve_returns_none_for_unknown_user(self):
        store = ContactInfoStore()
        assert store.resolve_endpoint("unknown", Channel.EMAIL) is None

    def test_resolve_returns_none_when_email_not_set(self):
        store = ContactInfoStore()
        store.set(ContactInfo(user_id="u1", phone="+123"))
        assert store.resolve_endpoint("u1", Channel.EMAIL) is None

    def test_resolve_returns_none_when_phone_not_set(self):
        store = ContactInfoStore()
        store.set(ContactInfo(user_id="u1", email="a@b.com"))
        assert store.resolve_endpoint("u1", Channel.SMS) is None

    def test_resolve_returns_none_when_no_device_tokens(self):
        store = ContactInfoStore()
        store.set(ContactInfo(user_id="u1", email="a@b.com"))
        assert store.resolve_endpoint("u1", Channel.IOS_PUSH) is None
        assert store.resolve_endpoint("u1", Channel.ANDROID_PUSH) is None
