"""Grant verification can be tested without a database or media worker."""
import pytest
from video_streaming import grants


def test_signed_grant_kind_expiry_and_tampering():
    secret = b'test-secret'
    token = grants.sign(secret, {'kind': 'upload', 'expires': 20})
    assert grants.verify(secret, lambda: 10, token, 'upload')['expires'] == 20
    for now, value, kind in [(20, token, 'upload'), (10, token, 'play'), (10, token+'0', 'upload')]:
        with pytest.raises(PermissionError):
            grants.verify(secret, lambda: now, value, kind)
