"""Exclusive local credential provisioning without publishing token contents."""

import os
from pathlib import Path
import secrets


def provision_token(store, user, admin, path):
    token, target, created = secrets.token_urlsafe(32), Path(path), None
    try:
        with open(
            target,
            "x",
            encoding="utf-8",
            opener=lambda name, flags: os.open(name, flags, 0o600),
        ) as output:
            created = os.fstat(output.fileno())
            output.write(token)
            output.flush()
            os.fsync(output.fileno())
        store.provision(user, token, admin)
    except BaseException:
        if created is not None:
            try:
                current = target.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    target.unlink()
            except OSError:
                pass
        raise
