"""SQLAlchemy mappings that preserve the existing two-table SQLite file format."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    and_,
    column,
    func,
    or_,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.domain import (
    Batch,
    BatchState,
    FailureKind,
    HospitalInput,
    HospitalRow,
    RowState,
)


def utc_datetime(value: datetime | None = None) -> datetime:
    """Normalize a clock value for persistence and attempt timing."""
    value = value if value is not None else datetime.now(UTC)
    if value.utcoffset() is None:
        raise ValueError("Attempt timestamps must be timezone-aware")
    return value.astimezone(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Map aware datetimes to the ISO text used by existing SQLite databases."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> str | None:
        """Encode a typed value before SQLAlchemy binds it."""
        return utc_datetime(value).isoformat() if value is not None else None

    def process_result_value(
        self, value: str | None, dialect: Dialect
    ) -> datetime | None:
        """Restore typed evidence without leaking storage-format work to callers."""
        return (
            utc_datetime(datetime.fromisoformat(value)) if value is not None else None
        )


def _state_type(enum_type: type[StrEnum]) -> Enum:
    """Reuse validated non-native enum mapping and generated CHECK constraints."""
    return Enum(
        enum_type,
        values_callable=lambda enum: [member.value for member in enum],
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        name=enum_type.__name__.lower(),
    )


class Base(DeclarativeBase):
    """Shared ORM metadata for the two agreed tables, without a migration framework."""


class BatchModel(Base):
    """Session-scoped mutable aggregate with the seven existing batch columns."""

    __tablename__ = "batches"

    batch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    state: Mapped[BatchState] = mapped_column(
        "status", _state_type(BatchState), default=BatchState.QUEUED
    )
    total_hospitals: Mapped[int] = mapped_column(Integer)
    batch_activated: Mapped[bool] = mapped_column(
        Boolean(create_constraint=True), default=False
    )
    processing_time_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    attempt_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    rows: Mapped[list[HospitalRowModel]] = relationship(
        back_populates="batch",
        order_by="HospitalRowModel.row_number",
        lazy="raise",
    )

    # These expressions generate DDL; no handwritten data queries are needed.
    __table_args__ = (
        CheckConstraint(column("total_hospitals").between(1, 20), name="batch_size"),
        CheckConstraint(
            column("processing_time_seconds") >= 0, name="nonnegative_duration"
        ),
        CheckConstraint(
            or_(
                and_(
                    column("status") == BatchState.COMPLETED.value,
                    column("batch_activated").is_(True),
                    column("attempt_started_at").is_(None),
                ),
                and_(
                    column("status") != BatchState.COMPLETED.value,
                    column("batch_activated").is_(False),
                ),
            ),
            name="completion_requires_activation",
        ),
    )

    def row_at(self, row_number: int) -> HospitalRowModel | None:
        """Reuse lookup inside the already loaded manifest of at most 20 rows."""
        return next((row for row in self.rows if row.row_number == row_number), None)

    def to_domain(self) -> Batch:
        """Detach a consistent immutable snapshot from the live ORM session."""
        return Batch(
            batch_id=UUID(self.batch_id),
            state=self.state,
            total_hospitals=self.total_hospitals,
            batch_activated=self.batch_activated,
            processing_time_seconds=self.processing_time_seconds,
            attempt_started_at=self.attempt_started_at,
            last_error=self.last_error,
            rows=tuple(row.to_domain() for row in self.rows),
        )


class HospitalRowModel(Base):
    """ORM checkpoint for one input record, not the external hospital system itself."""

    __tablename__ = "hospital_rows"

    batch_id: Mapped[str] = mapped_column(
        ForeignKey("batches.batch_id"), primary_key=True
    )
    row_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    hospital_id: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[RowState] = mapped_column(
        "status", _state_type(RowState), default=RowState.PENDING
    )
    failure_kind: Mapped[FailureKind | None] = mapped_column(_state_type(FailureKind))
    last_error: Mapped[str | None] = mapped_column(Text)
    batch: Mapped[BatchModel] = relationship(back_populates="rows", lazy="raise")

    __table_args__ = (
        CheckConstraint(column("row_number").between(1, 20), name="row_position"),
        CheckConstraint(
            func.length(func.trim(column("name"))) > 0, name="nonblank_name"
        ),
        CheckConstraint(
            func.length(func.trim(column("address"))) > 0, name="nonblank_address"
        ),
        CheckConstraint(
            or_(
                column("hospital_id").is_(None),
                func.typeof(column("hospital_id")) == "integer",
            ),
            name="integer_remote_id",
        ),
        CheckConstraint(
            or_(
                column("status").not_in(
                    [RowState.CREATED.value, RowState.ACTIVATED.value]
                ),
                and_(
                    column("hospital_id").is_not(None),
                    column("failure_kind").is_(None),
                    column("last_error").is_(None),
                ),
            ),
            name="confirmed_creation_evidence",
        ),
        CheckConstraint(
            or_(
                and_(
                    column("status").in_(
                        [
                            RowState.PENDING.value,
                            RowState.CREATING.value,
                            RowState.CREATED.value,
                            RowState.ACTIVATED.value,
                        ]
                    ),
                    column("failure_kind").is_(None),
                ),
                and_(
                    column("status") == RowState.FAILED.value,
                    column("failure_kind").is_not(None),
                    column("failure_kind").in_(
                        [FailureKind.RETRYABLE.value, FailureKind.PERMANENT.value]
                    ),
                ),
                and_(
                    column("status") == RowState.UNKNOWN.value,
                    column("failure_kind").is_not(None),
                    column("failure_kind") == FailureKind.UNCERTAIN.value,
                ),
            ),
            name="failure_evidence",
        ),
        Index(
            "uq_batch_hospital_id",
            "batch_id",
            "hospital_id",
            unique=True,
            sqlite_where=column("hospital_id").is_not(None),
        ),
    )

    def to_domain(self) -> HospitalRow:
        """Project scalars into the transport/database-independent row type."""
        return HospitalRow(
            row_number=self.row_number,
            hospital=HospitalInput(self.name, self.address, self.phone),
            state=self.state,
            hospital_id=self.hospital_id,
            failure_kind=self.failure_kind,
            last_error=self.last_error,
        )
