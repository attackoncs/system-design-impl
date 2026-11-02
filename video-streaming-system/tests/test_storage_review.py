"""Source assembly must preserve the checksums accepted during upload."""

import hashlib

import pytest

from video_streaming import VideoService
from video_streaming.storage import LocalObjects


@pytest.mark.parametrize("damaged_index", [0, 1])
def test_assembly_rejects_corrupt_part_and_cleans_staging(tmp_path, damaged_index):
    store = LocalObjects(tmp_path)
    # Exercise the tail after a full streaming buffer as well as a later part.
    parts = [b"a" * (1024 * 1024 + 1), b"second part"]
    keys = [store.put(part) for part in parts]
    damaged = store.file(keys[damaged_index])
    damaged.write_bytes(parts[damaged_index][:-1] + b"!")

    with pytest.raises(OSError, match="checksum"):
        store.assemble(keys)

    assert {path.name for path in tmp_path.iterdir()} == set(keys)
    assert store.put(parts[damaged_index]) == keys[damaged_index]
    key, size = store.assemble(keys)
    expected = b"".join(parts)
    assert key == hashlib.sha256(expected).hexdigest()
    assert size == len(expected) and store.file(key).read_bytes() == expected


@pytest.mark.parametrize("damaged_index", [0, 1])
def test_finish_without_checksum_rejects_corrupt_part_and_can_retry(
    tmp_path, damaged_index
):
    service = VideoService(tmp_path / "metadata.sqlite3", tmp_path / "objects")
    try:
        token = service.issue("alice")
        parts = [b"abc", b"def"]
        upload = service.create(token, "Video", 6, part_size=3)
        for grant, part in zip(upload["parts"], parts):
            assert service.put_part(grant, part)
        key = hashlib.sha256(parts[damaged_index]).hexdigest()
        service.objects.file(key).write_bytes(b"bad")

        with pytest.raises(OSError, match="checksum"):
            service.finish(token, upload["video_id"])

        assert service.status(token, upload["video_id"])["state"] == "UPLOADING"
        assert service.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert not list(service.objects.root.glob("*.tmp"))
        # The original grant retry repairs the immutable object before reuse.
        assert not service.put_part(
            upload["parts"][damaged_index], parts[damaged_index]
        )
        service.finish(token, upload["video_id"])
        assert service.status(token, upload["video_id"])["state"] == "PROCESSING"
    finally:
        service.close()
