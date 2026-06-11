from __future__ import annotations

from datetime import datetime  # noqa: TC003
from uuid import UUID, uuid4

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    display_name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)  # citext handled via migration
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Identity(Base):
    __tablename__ = "identities"

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    provider_subject: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    identity_provider: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_ct: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PendingLogin(Base):
    __tablename__ = "oauth_pending_logins"

    # The random `state` token (secrets.token_urlsafe) that correlates a login
    # redirect with its callback. Server-side home for the user-supplied `next`
    # path so it never rides in a browser cookie.
    state: Mapped[str] = mapped_column(Text, primary_key=True)
    next_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    refresh_token_ct: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    access_token_cache_ct: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("user_id", "provider", "provider_account_id"),)


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, primary_key=True)


class UserPreferences(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    custom_instructions: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class McpServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_token_ct: Mapped[bytes | None] = mapped_column(LargeBinary)
    allowed_roles: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    model: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status in ('queued','running','succeeded','failed','cancelled')",
            name="runs_status_enum",
        ),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id")
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "role in ('user','assistant','system','tool')", name="messages_role_enum"
        ),
    )


class RunEvent(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("run_id", "sequence"),)
