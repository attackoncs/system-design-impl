"""Focused persistence operations composed by Store."""

import hashlib
import secrets
from .common import Error, identifier
from .contracts import session as validate_session


class SessionMixin:
    def session_create(self, user):
        identifier(user)
        token = user + "." + secrets.token_hex(24)
        with self.transaction() as db:
            self.check_partition(db, user)
            db.execute("DELETE FROM sessions WHERE expires<=?", (self.now(),))
            if db.execute("SELECT count(*) FROM sessions").fetchone()[0] >= 1000:
                raise Error(429, "reference session capacity reached")
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), user, self.now() + 3600),
            )
        return {"session": token, "expires_in_seconds": 3600}

    def session_check(self, user, token):
        try:
            validate_session(user, token)
        except ValueError:
            raise Error(401, "session invalid or expired") from None
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            if not row or row["user"] != user or row["expires"] <= self.now():
                raise Error(401, "session invalid or expired")
        return {"user": user}
