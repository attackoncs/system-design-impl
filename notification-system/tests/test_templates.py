"""Unit tests for the Notification Templates module."""

import pytest

from notification_system.exceptions import TemplateRenderError
from notification_system.templates import NotificationTemplate, TemplateRegistry


class TestNotificationTemplatePlaceholderSubstitution:
    """Tests for placeholder substitution in templates."""

    def test_render_substitutes_single_placeholder_in_body(self):
        template = NotificationTemplate("t1", "Hello", "Hi {name}!")
        title, body = template.render({"name": "Alice"})
        assert body == "Hi Alice!"

    def test_render_substitutes_single_placeholder_in_title(self):
        template = NotificationTemplate("t1", "Welcome {name}", "Body text")
        title, body = template.render({"name": "Bob"})
        assert title == "Welcome Bob"

    def test_render_substitutes_multiple_placeholders(self):
        template = NotificationTemplate(
            "t1", "{greeting} {name}", "Your order {order_id} is {status}."
        )
        params = {
            "greeting": "Hello",
            "name": "Charlie",
            "order_id": "ORD-123",
            "status": "shipped",
        }
        title, body = template.render(params)
        assert title == "Hello Charlie"
        assert body == "Your order ORD-123 is shipped."

    def test_render_with_no_placeholders(self):
        template = NotificationTemplate("t1", "Static Title", "Static Body")
        title, body = template.render({})
        assert title == "Static Title"
        assert body == "Static Body"

    def test_render_same_placeholder_used_multiple_times(self):
        template = NotificationTemplate("t1", "{name}", "{name} said hi to {name}")
        title, body = template.render({"name": "Eve"})
        assert title == "Eve"
        assert body == "Eve said hi to Eve"

    def test_render_extra_params_are_ignored(self):
        template = NotificationTemplate("t1", "Hi {name}", "Body")
        title, body = template.render({"name": "Dave", "extra": "ignored"})
        assert title == "Hi Dave"
        assert body == "Body"


class TestGetRequiredKeys:
    """Tests for get_required_keys method."""

    def test_returns_empty_set_for_no_placeholders(self):
        template = NotificationTemplate("t1", "Hello", "World")
        assert template.get_required_keys() == set()

    def test_returns_keys_from_title(self):
        template = NotificationTemplate("t1", "Hi {name}", "Body")
        assert template.get_required_keys() == {"name"}

    def test_returns_keys_from_body(self):
        template = NotificationTemplate("t1", "Title", "Order {order_id} is {status}")
        assert template.get_required_keys() == {"order_id", "status"}

    def test_returns_union_of_title_and_body_keys(self):
        template = NotificationTemplate(
            "t1", "Hello {name}", "Your code is {code}"
        )
        assert template.get_required_keys() == {"name", "code"}

    def test_deduplicates_keys_across_title_and_body(self):
        template = NotificationTemplate("t1", "Hi {name}", "Welcome {name}")
        assert template.get_required_keys() == {"name"}


class TestTemplateRenderErrorOnMissingKeys:
    """Tests for TemplateRenderError raised on missing placeholders."""

    def test_raises_on_single_missing_key(self):
        template = NotificationTemplate("t1", "Hi {name}", "Body")
        with pytest.raises(TemplateRenderError) as exc_info:
            template.render({})
        assert exc_info.value.template_id == "t1"
        assert "name" in exc_info.value.missing_keys

    def test_raises_on_multiple_missing_keys(self):
        template = NotificationTemplate(
            "t2", "{greeting} {name}", "Order {order_id}"
        )
        with pytest.raises(TemplateRenderError) as exc_info:
            template.render({})
        assert exc_info.value.template_id == "t2"
        assert set(exc_info.value.missing_keys) == {"greeting", "name", "order_id"}

    def test_raises_when_only_some_keys_provided(self):
        template = NotificationTemplate("t3", "{a} {b}", "{c}")
        with pytest.raises(TemplateRenderError) as exc_info:
            template.render({"a": "val_a"})
        assert set(exc_info.value.missing_keys) == {"b", "c"}

    def test_error_message_contains_template_id(self):
        template = NotificationTemplate("my-template", "Hi {name}", "Body")
        with pytest.raises(TemplateRenderError, match="my-template"):
            template.render({})


class TestSeparateTitleBodyRendering:
    """Tests for separate title and body rendering."""

    def test_render_returns_tuple_of_title_and_body(self):
        template = NotificationTemplate("t1", "Title {x}", "Body {y}")
        result = template.render({"x": "A", "y": "B"})
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_title_and_body_rendered_independently(self):
        template = NotificationTemplate("t1", "Title {x}", "Body {y}")
        title, body = template.render({"x": "TitleVal", "y": "BodyVal"})
        assert title == "Title TitleVal"
        assert body == "Body BodyVal"

    def test_title_template_property(self):
        template = NotificationTemplate("t1", "My Title", "My Body")
        assert template.title_template == "My Title"

    def test_body_template_property(self):
        template = NotificationTemplate("t1", "My Title", "My Body")
        assert template.body_template == "My Body"

    def test_template_id_property(self):
        template = NotificationTemplate("unique-id", "Title", "Body")
        assert template.template_id == "unique-id"


class TestTemplateRegistryRegister:
    """Tests for TemplateRegistry register method."""

    def test_register_stores_template(self):
        registry = TemplateRegistry()
        template = NotificationTemplate("t1", "Title", "Body")
        registry.register(template)
        assert registry.get("t1") is template

    def test_register_overwrites_existing_template(self):
        registry = TemplateRegistry()
        template1 = NotificationTemplate("t1", "Title1", "Body1")
        template2 = NotificationTemplate("t1", "Title2", "Body2")
        registry.register(template1)
        registry.register(template2)
        assert registry.get("t1") is template2


class TestTemplateRegistryGet:
    """Tests for TemplateRegistry get method."""

    def test_get_returns_registered_template(self):
        registry = TemplateRegistry()
        template = NotificationTemplate("t1", "Title", "Body")
        registry.register(template)
        assert registry.get("t1") is template

    def test_get_returns_none_for_unknown_id(self):
        registry = TemplateRegistry()
        assert registry.get("nonexistent") is None

    def test_get_returns_none_on_empty_registry(self):
        registry = TemplateRegistry()
        assert registry.get("anything") is None


class TestTemplateRegistryListIds:
    """Tests for TemplateRegistry list_ids method."""

    def test_list_ids_empty_registry(self):
        registry = TemplateRegistry()
        assert registry.list_ids() == []

    def test_list_ids_single_template(self):
        registry = TemplateRegistry()
        registry.register(NotificationTemplate("t1", "Title", "Body"))
        assert registry.list_ids() == ["t1"]

    def test_list_ids_multiple_templates(self):
        registry = TemplateRegistry()
        registry.register(NotificationTemplate("t1", "Title1", "Body1"))
        registry.register(NotificationTemplate("t2", "Title2", "Body2"))
        registry.register(NotificationTemplate("t3", "Title3", "Body3"))
        ids = registry.list_ids()
        assert set(ids) == {"t1", "t2", "t3"}
        assert len(ids) == 3
