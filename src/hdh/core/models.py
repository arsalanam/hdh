"""
SQLAlchemy ORM models for the Family Medicine synthetic dataset.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
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
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker


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
    __tablename__ = "patients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mrn = Column(String(12), unique=True, nullable=False)  # Medical Record Number
    first_name = Column(String(50), nullable=False)
    last_name = Column(String(50), nullable=False)
    date_of_birth = Column(Date, nullable=False)
    sex = Column(SAEnum(Sex), nullable=False)
    race = Column(String(30))
    ethnicity = Column(String(30))
    address = Column(String(200))
    city = Column(String(60))
    state = Column(String(2))
    zip_code = Column(String(10))
    phone = Column(String(15))
    email = Column(String(80))
    insurance_name = Column(String(80))
    insurance_id = Column(String(20))
    blood_type = Column(String(4))
    # Known allergies (pipe-separated string for simplicity)
    allergies = Column(Text)
    # Family history flags
    fam_hx_diabetes = Column(Boolean, default=False)
    fam_hx_hypertension = Column(Boolean, default=False)
    fam_hx_heart_disease = Column(Boolean, default=False)
    fam_hx_cancer = Column(Boolean, default=False)
    # Smoking / lifestyle
    smoker = Column(Boolean, default=False)
    bmi_baseline = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)

    visits = relationship(
        "Visit", back_populates="patient", cascade="all, delete-orphan", order_by="Visit.visit_date"
    )
    chronic_conditions = relationship(
        "ChronicCondition", back_populates="patient", cascade="all, delete-orphan"
    )

    @property
    def age(self):
        today = datetime.today().date()
        dob = self.date_of_birth
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


class ChronicCondition(Base):
    """Persistent diagnoses that are tracked separately from visit-level diagnoses."""

    __tablename__ = "chronic_conditions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    icd10_code = Column(String(10), nullable=False)
    description = Column(String(200), nullable=False)
    onset_date = Column(Date)
    controlled = Column(Boolean, default=True)  # well-controlled vs not

    patient = relationship("Patient", back_populates="chronic_conditions")


class Visit(Base):
    __tablename__ = "visits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    visit_date = Column(Date, nullable=False)
    visit_type = Column(SAEnum(VisitType), nullable=False)
    chief_complaint = Column(String(200))
    provider_name = Column(String(80))
    # Follow-up scheduling
    follow_up_days = Column(Integer)  # None = PRN

    patient = relationship("Patient", back_populates="visits")
    vitals = relationship("Vital", back_populates="visit", cascade="all, delete-orphan", uselist=False)
    diagnoses = relationship("Diagnosis", back_populates="visit", cascade="all, delete-orphan")
    prescriptions = relationship("Prescription", back_populates="visit", cascade="all, delete-orphan")
    lab_results = relationship("LabResult", back_populates="visit", cascade="all, delete-orphan")


class Vital(Base):
    __tablename__ = "vitals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    visit_id = Column(Integer, ForeignKey("visits.id"), nullable=False)
    # Blood pressure
    bp_systolic = Column(Integer)
    bp_diastolic = Column(Integer)
    heart_rate = Column(Integer)
    respiratory_rate = Column(Integer)
    temperature_f = Column(Float)
    oxygen_sat = Column(Integer)  # SpO2 %
    weight_kg = Column(Float)
    height_cm = Column(Float)
    bmi = Column(Float)
    pain_scale = Column(Integer)  # 0-10

    visit = relationship("Visit", back_populates="vitals")


class Diagnosis(Base):
    __tablename__ = "diagnoses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    visit_id = Column(Integer, ForeignKey("visits.id"), nullable=False)
    icd10_code = Column(String(10), nullable=False)
    description = Column(String(200), nullable=False)
    is_primary = Column(Boolean, default=True)

    visit = relationship("Visit", back_populates="diagnoses")


class Prescription(Base):
    __tablename__ = "prescriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    visit_id = Column(Integer, ForeignKey("visits.id"), nullable=False)
    drug_name = Column(String(100), nullable=False)
    drug_class = Column(String(80))
    dose = Column(String(40))
    frequency = Column(String(40))
    duration_days = Column(Integer)
    refills = Column(Integer, default=0)
    is_new = Column(Boolean, default=True)  # False = continuation

    visit = relationship("Visit", back_populates="prescriptions")


class LabResult(Base):
    __tablename__ = "lab_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    visit_id = Column(Integer, ForeignKey("visits.id"), nullable=False)
    test_name = Column(String(100), nullable=False)
    value = Column(Float)
    unit = Column(String(20))
    reference_low = Column(Float)
    reference_high = Column(Float)
    status = Column(SAEnum(LabStatus), nullable=False)
    loinc_code = Column(String(10))

    visit = relationship("Visit", back_populates="lab_results")


# ─── Database setup helper ─────────────────────────────────────────────────────


def get_engine(db_path: str = "family_medicine.db"):
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    return engine


def get_session(engine):
    Session = sessionmaker(bind=engine)
    return Session()
