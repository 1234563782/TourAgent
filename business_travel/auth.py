"""Password accounts and revocable, user-scoped PostgreSQL sessions."""

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pwdlib import PasswordHash

from .memory_pg import MemoryIdentity, memory_connection
from .settings import read_environment

PASSWORD_HASH = PasswordHash.recommended()
DUMMY_HASH = PASSWORD_HASH.hash(secrets.token_urlsafe(32))
COOKIE_NAME = "travel_session"


@dataclass(frozen=True)
class AuthSettings:
    origin: str = "http://127.0.0.1:8000"
    session_seconds: int = 28800
    rate_seconds: int = 300
    rate_limit: int = 30
    max_failures: int = 10
    lock_seconds: int = 900

    def __post_init__(self):
        parsed = urlsplit(self.origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("TRAVEL_WEB_ORIGIN must be an origin without a path")
        try:
            parsed.port
        except ValueError:
            raise ValueError("Invalid web origin port") from None
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Non-loopback web deployments require HTTPS")
        if not (300 <= self.session_seconds <= 86400 and 10 <= self.rate_seconds <= 3600
                and 1 <= self.rate_limit <= 100 and 1 <= self.max_failures <= 20
                and 60 <= self.lock_seconds <= 86400):
            raise ValueError("Invalid authentication limits")

    @property
    def secure_cookie(self):
        return self.origin.startswith("https://")

    @classmethod
    def from_env(cls):
        env = read_environment(Path(__file__).resolve().parent.parent / ".env")
        return cls(
            origin=env.get("TRAVEL_WEB_ORIGIN") or cls.origin,
            session_seconds=int(env.get("TRAVEL_AUTH_SESSION_SECONDS") or cls.session_seconds),
            rate_seconds=int(env.get("TRAVEL_AUTH_RATE_SECONDS") or cls.rate_seconds),
            rate_limit=int(env.get("TRAVEL_AUTH_RATE_LIMIT") or cls.rate_limit),
            max_failures=int(env.get("TRAVEL_AUTH_MAX_FAILURES") or cls.max_failures),
            lock_seconds=int(env.get("TRAVEL_AUTH_LOCK_SECONDS") or cls.lock_seconds),
        )


class LoginLimited(Exception):
    pass


def username_value(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", value):
        raise ValueError("Username must contain 3-64 ASCII letters, digits, underscores or hyphens")
    return value.lower()


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 12 <= len(password) <= 128 or not password.strip():
        raise ValueError("Password must contain 12-128 characters and cannot be blank")
    return PASSWORD_HASH.hash(password)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def setup_auth(settings):
    """Requires the explicit memory initialization step to have created the schema."""
    with memory_connection(settings) as connection, connection.transaction():
        connection.execute("""
            CREATE TABLE IF NOT EXISTS auth_users (
                user_id TEXT NOT NULL,
                username TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT true,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id),
                UNIQUE (username)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at TIMESTAMPTZ NOT NULL,
                FOREIGN KEY (user_id)
                    REFERENCES auth_users(user_id) ON DELETE CASCADE
            )
        """)
        connection.execute('CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions(user_id)')
        connection.execute("CREATE INDEX IF NOT EXISTS auth_sessions_expiry ON auth_sessions(expires_at)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS auth_login_limits (
                client_hash TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (client_hash)
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS auth_personnel (
                user_id TEXT NOT NULL,
                personnel_type TEXT,
                academic_title TEXT,
                technical_grade INTEGER,
                management_grade INTEGER,
                lodging_standard TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id),
                FOREIGN KEY (user_id)
                    REFERENCES auth_users(user_id) ON DELETE CASCADE
            )
        """)


class AuthStore:
    def __init__(self, settings, config: AuthSettings):
        self.settings = settings
        self.config = config

    def check(self):
        with memory_connection(self.settings) as connection:
            connection.execute("SELECT user_id FROM auth_users LIMIT 0")
            connection.execute("SELECT token_hash FROM auth_sessions LIMIT 0")
            connection.execute("SELECT client_hash FROM auth_login_limits LIMIT 0")
            connection.execute("SELECT user_id FROM auth_personnel LIMIT 0")

    def create_user(self, username, password):
        username = username_value(username)
        encoded = hash_password(password)
        user_id = uuid.uuid4().hex
        with memory_connection(self.settings) as connection:
            connection.execute(
                'INSERT INTO auth_users(user_id,username,password_hash) VALUES(%s,%s,%s)',
                (user_id, username, encoded),
            )
        return MemoryIdentity(user_id)

    def manage_user(self, username, *, password=None, active=None):
        username = username_value(username)
        encoded = hash_password(password) if password is not None else None
        with memory_connection(self.settings) as connection, connection.transaction():
            row = connection.execute(
                'SELECT user_id FROM auth_users WHERE username=%s FOR UPDATE',
                (username,),
            ).fetchone()
            if row is None:
                raise ValueError("Account not found")
            connection.execute(
                'UPDATE auth_users SET password_hash=COALESCE(%s,password_hash), active=COALESCE(%s,active),failed_attempts=0,locked_until=NULL WHERE user_id=%s',
                (encoded, active, row["user_id"]),
            )
            connection.execute('DELETE FROM auth_sessions WHERE user_id=%s',
                               (row["user_id"],))

    def login(self, username, password, client):
        username = username_value(username)
        with memory_connection(self.settings) as connection:
            # Atomic fixed windows work across processes; do not trust forwarded IP headers.
            rate = connection.execute(
                "INSERT INTO auth_login_limits(client_hash,attempts,expires_at) VALUES(%s,1,now()+%s*interval '1 second') ON CONFLICT(client_hash) DO UPDATE SET attempts=CASE WHEN auth_login_limits.expires_at<=now() THEN 1 ELSE auth_login_limits.attempts+1 END, expires_at=CASE WHEN auth_login_limits.expires_at<=now() THEN excluded.expires_at ELSE auth_login_limits.expires_at END RETURNING attempts",
                (token_digest(client), self.config.rate_seconds),
            ).fetchone()
            if rate["attempts"] > self.config.rate_limit:
                raise LoginLimited()
            with connection.transaction():
                user = connection.execute(
                    'SELECT *, locked_until>now() AS locked FROM auth_users WHERE username=%s FOR UPDATE', (username,),
                ).fetchone()
                valid = PASSWORD_HASH.verify(password, user["password_hash"] if user else DUMMY_HASH)
                if user is None:
                    return None
                if not user["active"] or user["locked"]:
                    return None
                if not valid:
                    connection.execute(
                        "UPDATE auth_users SET failed_attempts=CASE WHEN locked_until IS NOT NULL THEN 1 ELSE failed_attempts+1 END, locked_until=CASE WHEN (CASE WHEN locked_until IS NOT NULL THEN 1 ELSE failed_attempts+1 END)>=%s THEN now()+%s*interval '1 second' ELSE NULL END WHERE user_id=%s",
                        (self.config.max_failures, self.config.lock_seconds, user["user_id"]),
                    )
                    return None
                connection.execute(
                    'UPDATE auth_users SET failed_attempts=0,locked_until=NULL WHERE user_id=%s', (user["user_id"],),
                )
                token = secrets.token_urlsafe(32)
                connection.execute(
                    "INSERT INTO auth_sessions(token_hash,user_id,expires_at) VALUES(%s,%s,now()+%s*interval '1 second')",
                    (token_digest(token), user["user_id"], self.config.session_seconds),
                )
                return token

    def current_user(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return None
        with memory_connection(self.settings) as connection:
            return connection.execute(
                'SELECT u.user_id,u.username FROM auth_sessions s JOIN auth_users u USING(user_id) WHERE s.token_hash=%s AND s.expires_at>now() AND u.active',
                (token_digest(token),),
            ).fetchone()

    def logout(self, token):
        if isinstance(token, str) and len(token) <= 128:
            with memory_connection(self.settings) as connection:
                connection.execute('DELETE FROM auth_sessions WHERE token_hash=%s',
                                   (token_digest(token),))


    def get_personnel_by_username(self, username):
        username = username_value(username)
        with memory_connection(self.settings) as connection:
            user = connection.execute(
                'SELECT user_id FROM auth_users WHERE username=%s',
                (username,),
            ).fetchone()
        if user is None:
            raise ValueError("账号不存在")
        return self.get_personnel(user["user_id"],)

    def get_personnel(self, user_id):
        from .personnel import PersonnelProfile
        with memory_connection(self.settings) as connection:
            row = connection.execute(
                'SELECT personnel_type, academic_title, technical_grade, management_grade, lodging_standard FROM auth_personnel WHERE user_id=%s',
                (user_id,),
            ).fetchone()
        return PersonnelProfile.model_validate(dict(row)) if row else PersonnelProfile()

    def set_personnel(self, username, profile):
        from .personnel import PersonnelProfile
        username = username_value(username)
        profile = profile if isinstance(profile, PersonnelProfile) else PersonnelProfile.model_validate(profile)
        with memory_connection(self.settings) as connection, connection.transaction():
            user = connection.execute(
                'SELECT user_id FROM auth_users WHERE username=%s',
                (username,),
            ).fetchone()
            if user is None:
                raise ValueError("账号不存在")
            connection.execute(
                'INSERT INTO auth_personnel(user_id,personnel_type,academic_title,technical_grade,management_grade,lodging_standard) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET personnel_type=excluded.personnel_type, academic_title=excluded.academic_title, technical_grade=excluded.technical_grade, management_grade=excluded.management_grade, lodging_standard=excluded.lodging_standard, updated_at=now()',
                (user["user_id"], profile.personnel_type, profile.academic_title,
                 profile.technical_grade, profile.management_grade, profile.lodging_standard),
            )
        return profile

    def prune(self):
        with memory_connection(self.settings) as connection, connection.transaction():
            connection.execute('DELETE FROM auth_sessions WHERE expires_at<=now()')
            connection.execute('DELETE FROM auth_login_limits WHERE expires_at<=now()')
