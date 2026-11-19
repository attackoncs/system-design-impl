"""Persistent storage remains bound to its database across cwd changes."""

import pytest
from url_shortener import Base62Strategy, URLShortener
from url_shortener.sqlite_storage import SQLiteStorage


def test_relative_database_keeps_mapping_and_allocator_after_chdir(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    storage = SQLiteStorage("urls.db")
    service = URLShortener(storage=storage, strategy=Base62Strategy(storage))
    url = "https://example.org/original"
    code = service.shorten(url).rsplit("/", 1)[1]
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = SQLiteStorage(other_dir / "urls.db")
    monkeypatch.chdir(other_dir)
    mapping = storage.get_by_short_code(code)
    observed = (
        mapping.long_url if mapping else None,
        storage.next_id(),
        other.next_id(),
    )
    assert observed == (url, 2, 1)


@pytest.mark.parametrize("path", ["", ":memory:"])
def test_temporary_database_paths_are_rejected_before_initialization(path):
    with pytest.raises(ValueError, match="persistent"):
        SQLiteStorage(path)
