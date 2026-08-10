"""
SQLAlchemy ORM models for the Family Medicine synthetic dataset.
"""

import enum
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)


class Base(DeclarativeBase):
    pass


# ─── Enums ────────────────────────────────────────────────────────────────────


class Sex(str, enum.Enum):
    MALE = "M"
    FEMALE = "F"


class VisitType(str, enum.Enum):
    ACUTE = "acute"
    FOLLOW_UP = "follow_up"
    PREVENTIVE = "preventive"  # annual physical / well-child
    URGENT = "urgent"


class LabStatus(str, enum.Enum):
    NORMAL = "normal"
    HIGH = "high"
    LOW = "low"
    CRITICAL = "critical"


# ─── Core Tables ──────────────────────────────────────────────────────────────


class Patient(Base):
    """A patient: demographics, insurance, allergies, family history, lifestyle."""

    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mrn: Mapped[str] = mapped_column(String(12), unique=True)  # Medical Record Number
    first_name: Mapped[str] = mapped_column(String(50))
    last_name: Mapped[str] = mapped_column(String(50))
    date_of_birth: Mapped[date] = mapped_column(Date)
    sex: Mapped[Sex] = mapped_column(SAEnum(Sex))
    # 50: "American Indian or Alaska Native" is 32 chars — PostgreSQL enforces
    # VARCHAR lengths that SQLite silently ignores
    race: Mapped[str | None] = mapped_column(String(50))
    ethnicity: Mapped[str | None] = mapped_column(String(30))
    address: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(60))
    state: Mapped[str | None] = mapped_column(String(2))
    zip_code: Mapped[str | None] = mapped_column(String(10))
    phone: Mapped[str | None] = mapped_column(String(15))
    email: Mapped[str | None] = mapped_column(String(80))
    insurance_name: Mapped[str | None] = mapped_column(String(80))
    insurance_id: Mapped[str | None] = mapped_column(String(20))
    blood_type: Mapped[str | None] = mapped_column(String(4))
    # Known allergies (pipe-separated string for simplicity)
    allergies: Mapped[str | None] = mapped_column(Text)
    # Family history flags
    fam_hx_diabetes: Mapped[bool | None] = mapped_column(Boolean, default=False)
    fam_hx_hypertension: Mapped[bool | None] = mapped_column(Boolean, default=False)
    fam_hx_heart_disease: Mapped[bool | None] = mapped_column(Boolean, default=False)
    fam_hx_cancer: Mapped[bool | None] = mapped_column(Boolean, default=False)
    # Smoking / lifestyle
    smoker: Mapped[bool | None] = mapped_column(Boolean, default=False)
    bmi_baseline: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow)

    visits: Mapped[list["Visit"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan", order_by="Visit.visit_date"
    )
    chronic_conditions: Mapped[list["ChronicCondition"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan"
    )

    @property
    def age(self) -> int:
        today = datetime.today().date()
        dob = self.date_of_birth
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


class ChronicCondition(Base):
    """Persistent diagnoses that are tracked separately from visit-level diagnoses."""

    __tablename__ = "chronic_conditions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    icd10_code: Mapped[str] = mapped_column(String(10))
    description: Mapped[str] = mapped_column(String(200))
    onset_date: Mapped[date | None] = mapped_column(Date)
    controlled: Mapped[bool | None] = mapped_column(Boolean, default=True)  # well-controlled vs not

    patient: Mapped["Patient"] = relationship(back_populates="chronic_conditions")


class Visit(Base):
    """One outpatient encounter; owns vitals, diagnoses, prescriptions, labs."""

    __tablename__ = "visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    visit_date: Mapped[date] = mapped_column(Date)
    visit_type: Mapped[VisitType] = mapped_column(SAEnum(VisitType))
    chief_complaint: Mapped[str | None] = mapped_column(String(200))
    provider_name: Mapped[str | None] = mapped_column(String(80))
    # Follow-up scheduling
    follow_up_days: Mapped[int | None] = mapped_column(Integer)  # None = PRN

    patient: Mapped["Patient"] = relationship(back_populates="visits")
    vitals: Mapped["Vital | None"] = relationship(back_populates="visit", cascade="all, delete-orphan")
    diagnoses: Mapped[list["Diagnosis"]] = relationship(back_populates="visit", cascade="all, delete-orphan")
    prescriptions: Mapped[list["Prescription"]] = relationship(
        back_populates="visit", cascade="all, delete-orphan"
    )
    lab_results: Mapped[list["LabResult"]] = relationship(
        back_populates="visit", cascade="all, delete-orphan"
    )


class Vital(Base):
    """The vitals panel recorded at one visit."""

    __tablename__ = "vitals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id: Mapped[int] = mapped_column(ForeignKey("visits.id"))
    # Blood pressure
    bp_systolic: Mapped[int | None] = mapped_column(Integer)
    bp_diastolic: Mapped[int | None] = mapped_column(Integer)
    heart_rate: Mapped[int | None] = mapped_column(Integer)
    respiratory_rate: Mapped[int | None] = mapped_column(Integer)
    temperature_f: Mapped[float | None] = mapped_column(Float)
    oxygen_sat: Mapped[int | None] = mapped_column(Integer)  # SpO2 %
    weight_kg: Mapped[float | None] = mapped_column(Float)
    height_cm: Mapped[float | None] = mapped_column(Float)
    bmi: Mapped[float | None] = mapped_column(Float)
    pain_scale: Mapped[int | None] = mapped_column(Integer)  # 0-10

    visit: Mapped["Visit"] = relationship(back_populates="vitals")


class Diagnosis(Base):
    """An ICD-10 coded diagnosis made at one visit."""

    __tablename__ = "diagnoses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id: Mapped[int] = mapped_column(ForeignKey("visits.id"))
    icd10_code: Mapped[str] = mapped_column(String(10))
    description: Mapped[str] = mapped_column(String(200))
    is_primary: Mapped[bool | None] = mapped_column(Boolean, default=True)

    visit: Mapped["Visit"] = relationship(back_populates="diagnoses")


class Prescription(Base):
    """A medication ordered or continued at one visit."""

    __tablename__ = "prescriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id: Mapped[int] = mapped_column(ForeignKey("visits.id"))
    drug_name: Mapped[str] = mapped_column(String(100))
    drug_class: Mapped[str | None] = mapped_column(String(80))
    dose: Mapped[str | None] = mapped_column(String(40))
    frequency: Mapped[str | None] = mapped_column(String(40))
    duration_days: Mapped[int | None] = mapped_column(Integer)
    refills: Mapped[int | None] = mapped_column(Integer, default=0)
    is_new: Mapped[bool | None] = mapped_column(Boolean, default=True)  # False = continuation

    visit: Mapped["Visit"] = relationship(back_populates="prescriptions")


class LabResult(Base):
    """A LOINC-coded lab value with reference range and status flag."""

    __tablename__ = "lab_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id: Mapped[int] = mapped_column(ForeignKey("visits.id"))
    test_name: Mapped[str] = mapped_column(String(100))
    value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(20))
    reference_low: Mapped[float | None] = mapped_column(Float)
    reference_high: Mapped[float | None] = mapped_column(Float)
    status: Mapped[LabStatus] = mapped_column(SAEnum(LabStatus))
    loinc_code: Mapped[str | None] = mapped_column(String(10))

    visit: Mapped["Visit"] = relationship(back_populates="lab_results")


# ─── Database setup helper ─────────────────────────────────────────────────────


def get_engine(db_path: str = "family_medicine.db", db_url: str | None = None):
    """Create the engine, create missing tables, and add any extension
    columns the schema registry contributed (see core/schema_registry.py).

    Resolution order: explicit ``db_url`` argument → ``HDH_DB_URL``
    environment variable (e.g. the `just deps` PostgreSQL container) →
    SQLite file at ``db_path`` (the transitional default; see the SQLite
    retirement plan in docs/design/icd10cm-ontology-module.md §5.3).
    """
    import os

    url = db_url or os.environ.get("HDH_DB_URL") or f"sqlite:///{db_path}"
    kwargs: dict = {"echo": False}
    if url.startswith("postgresql"):
        kwargs.update(pool_pre_ping=True, pool_size=5, max_overflow=5)
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    from hdh.core.schema_registry import registry  # lazy: avoid import cycle

    if registry.applied:
        registry.ensure_columns(engine)
    return engine


def get_session(engine):
    Session = sessionmaker(bind=engine)
    return Session()
