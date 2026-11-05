"""The owned Redis runner must not follow Linux links through Windows stat."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def runner():
    path = Path(__file__).resolve().parents[1] / "tools" / "run_redis_review.py"
    spec = importlib.util.spec_from_file_location("chat_review_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("exists", [True, False])
def test_wsl_runtime_existence_does_not_follow_linux_symlink(
    monkeypatch, tmp_path, exists
):
    module = runner()
    checked = []

    def lexists(path):
        checked.append(path)
        return exists

    def inaccessible_target(path):
        raise OSError("Windows cannot stat the Linux symlink target")

    monkeypatch.setattr(
        module,
        "os",
        SimpleNamespace(name="nt", environ={}, path=SimpleNamespace(lexists=lexists)),
    )
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "is_file", inaccessible_target)
    monkeypatch.setattr(module, "wsl_path", lambda path: "/mnt/runtime/" + path.name)
    if exists:
        command = module.command(tmp_path, 12345, "test-only-password")
        assert command[:4] == ["wsl", "-d", "Ubuntu-26.04", "--"]
        assert "/mnt/runtime/redis-server" in command
        assert command[-2:] == ["--dir", "/mnt/runtime/" + tmp_path.name]
    else:
        with pytest.raises(RuntimeError, match="no Redis mock"):
            module.command(tmp_path, 12345, "test-only-password")
    assert checked == [module.ROOT / ".runtime/extracted/usr/bin/redis-server"]
