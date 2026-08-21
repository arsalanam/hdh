"""
SQLAlchemy ORM models for the Family Medicine synthetic dataset.

The basic patient chart lives in core (design: docs/design/
core-chart-expansion.md): profile, family structure and histories, a
unified problem list (Condition), medications past and active, procedures,
immunizations, allergies, and stored visit notes. Reference entities
(Provider, Specialty) are deliberately thin — identifier + name — and gain
richness through schema-registry extension modules, never core changes.
"""

import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
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


class ConditionStatus(str, enum.Enum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    REMISSION = "remission"


class MedicationStatus(str, enum.Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    STOPPED = "stopped"


class AllergySeverity(str, enum.Enum):
    MILD = "mild"
    MODERATE = "moderate"
    SEVERE = "severe"


class NoteType(str, enum.Enum):
    SOAP = "soap"
    ADDENDUM = "addendum"


class EditSource(str, enum.Enum):
    """Which surface made a chart change (design chart-maintenance.md §3.2)."""

    CLI = "cli"
    AGENT = "agent"
    PIPELINE = "pipeline"  # the comprehension applier


class AuditAction(str, enum.Enum):
    """What happened to the row. Wider than EditAction: creation is
    audited (comprehension's own writes) but is not something the edit
    API performs."""

    CREATE = "create"
    AMEND = "amend"
    VOID = "void"


class ServiceKind(str, enum.Enum):
    """What was asked for (design service-requests §2). One table with a
    discriminator, not four: the kinds share a lifecycle, an authoring
    visit, a requester, a code and a fulfilment link."""

    MEDICATION = "medication"  # → FHIR MedicationRequest
    LAB = "lab"  # → FHIR ServiceRequest
    REFERRAL = "referral"  # → FHIR ServiceRequest
    PROCEDURE = "procedure"  # → FHIR ServiceRequest
    FOLLOW_UP = "follow_up"  # source of truth; Visit.follow_up_days derives


class RequestStatus(str, enum.Enum):
    """FHIR's request lifecycle, trimmed to states we can actually reach."""

    DRAFT = "draft"  # comprehended or generated, not yet released
    ACTIVE = "active"  # released — sent, awaiting fulfilment
    COMPLETED = "completed"  # result or dispense received
    REVOKED = "revoked"  # cancelled by a human
    ENTERED_IN_ERROR = "entered_in_error"  # voided via chartedit


class RequestOrigin(str, enum.Enum):
    """WHERE the row came from — OMOP's ``*_type_concept_id`` lesson
    (design §3). We had provenance only in the audit trail, which means a
    row could not say for itself whether a human, the generator, a note or
    an outside partner put it there."""

    GENERATED = "generated"  # the synthetic generator
    COMPREHENSION = "comprehension"  # extracted from a note
    AGENT = "agent"
    CLINICIAN = "clinician"  # entered directly via CLI/UI
    EXTERNAL = "external"  # arrived from a partner


# ─── Reference entities (thin: identity only — modules add richness) ─────────


class Specialty(Base):
    """A clinical specialty — thin reference row (code + name)."""

    __tablename__ = "specialties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(80))


class Provider(Base):
    """A care provider — thin reference row (identifier + name + specialty)."""

    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    identifier: Mapped[str] = mapped_column(String(20), unique=True)  # NPI-like
    name: Mapped[str] = mapped_column(String(100))
    specialty_id: Mapped[int | None] = mapped_column(ForeignKey("specialties.id"))

    specialty: Mapped["Specialty | None"] = relationship()


# ─── Patient and family ──────────────────────────────────────────────────────


class Patient(Base):
    """A patient: demographics, profile, and the chart's root."""

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
    marital_status: Mapped[str | None] = mapped_column(String(20))
    language: Mapped[str | None] = mapped_column(String(30))
    deceased: Mapped[bool | None] = mapped_column(Boolean, default=False)
    deceased_date: Mapped[date | None] = mapped_column(Date)
    # use_alter breaks the patients ↔ family_members FK cycle at create time
    emergency_contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("family_members.id", use_alter=True, name="fk_patients_emergency_contact")
    )
    # Lifestyle
    smoker: Mapped[bool | None] = mapped_column(Boolean, default=False)
    bmi_baseline: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow)

    visits: Mapped[list["Visit"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan", order_by="Visit.visit_date"
    )
    conditions: Mapped[list["Condition"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan"
    )
    family_members: Mapped[list["FamilyMember"]] = relationship(
        back_populates="patient",
        cascade="all, delete-orphan",
        foreign_keys="FamilyMember.patient_id",
    )
    family_history: Mapped[list["FamilyHistory"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan"
    )
    allergies: Mapped[list["Allergy"]] = relationship(back_populates="patient", cascade="all, delete-orphan")
    medications: Mapped[list["MedicationStatement"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan"
    )
    procedures: Mapped[list["Procedure"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan"
    )
    immunizations: Mapped[list["Immunization"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan"
    )
    emergency_contact: Mapped["FamilyMember | None"] = relationship(
        foreign_keys=[emergency_contact_id], post_update=True
    )

    @property
    def age(self) -> int:
        today = datetime.today().date()
        dob = self.date_of_birth
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


class FamilyMember(Base):
    """One relative: a linked generated patient, or a lightweight row with
    a narrative summary ("father lived to 75, T2DM/HTN well managed")."""

    __tablename__ = "family_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    relationship_type: Mapped[str] = mapped_column(String(30))  # mother/father/sibling/child/spouse…
    name: Mapped[str | None] = mapped_column(String(100))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    related_patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"))
    deceased: Mapped[bool | None] = mapped_column(Boolean, default=False)
    deceased_age: Mapped[int | None] = mapped_column(Integer)
    phone: Mapped[str | None] = mapped_column(String(15))
    summary: Mapped[str | None] = mapped_column(Text)  # narrative life/health summary

    patient: Mapped["Patient"] = relationship(back_populates="family_members", foreign_keys=[patient_id])
    related_patient: Mapped["Patient | None"] = relationship(foreign_keys=[related_patient_id])


class FamilyHistory(Base):
    """A hereditary-relevant condition in a relative: 'mother, breast
    cancer, onset 52' — structured, unlike the old boolean flags."""

    __tablename__ = "family_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    family_member_id: Mapped[int | None] = mapped_column(ForeignKey("family_members.id"))
    relationship_type: Mapped[str] = mapped_column(String(30))
    condition: Mapped[str] = mapped_column(String(200))
    icd10_code: Mapped[str | None] = mapped_column(String(10))
    onset_age: Mapped[int | None] = mapped_column(Integer)

    patient: Mapped["Patient"] = relationship(back_populates="family_history")
    family_member: Mapped["FamilyMember | None"] = relationship()


# ─── The unified problem list ─────────────────────────────────────────────────


class Condition(Base):
    """One problem-list entry — unifies the old ChronicCondition and
    visit-level Diagnosis (design §11 decision 4). `visit_id` records the
    encounter where it was recorded/diagnosed, when known."""

    __tablename__ = "conditions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    visit_id: Mapped[int | None] = mapped_column(ForeignKey("visits.id"))
    icd10_code: Mapped[str] = mapped_column(String(10))
    description: Mapped[str] = mapped_column(String(200))
    chronic: Mapped[bool | None] = mapped_column(Boolean, default=False)
    status: Mapped[ConditionStatus] = mapped_column(SAEnum(ConditionStatus), default=ConditionStatus.ACTIVE)
    controlled: Mapped[bool | None] = mapped_column(Boolean)  # chronic + active only
    is_primary: Mapped[bool | None] = mapped_column(Boolean, default=True)
    onset_date: Mapped[date | None] = mapped_column(Date)
    resolved_date: Mapped[date | None] = mapped_column(Date)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime)  # entered in error (chartedit)

    patient: Mapped["Patient"] = relationship(back_populates="conditions")
    visit: Mapped["Visit | None"] = relationship(back_populates="conditions")


# ─── Encounters and their contents ────────────────────────────────────────────


class Visit(Base):
    """One outpatient encounter; owns vitals, conditions recorded,
    prescriptions, labs, procedures, and the stored note."""

    __tablename__ = "visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    visit_date: Mapped[date] = mapped_column(Date)
    visit_type: Mapped[VisitType] = mapped_column(SAEnum(VisitType))
    chief_complaint: Mapped[str | None] = mapped_column(String(200))
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id"))

    patient: Mapped["Patient"] = relationship(back_populates="visits")
    provider: Mapped["Provider | None"] = relationship()
    vitals: Mapped["Vital | None"] = relationship(back_populates="visit", cascade="all, delete-orphan")
    conditions: Mapped[list["Condition"]] = relationship(back_populates="visit")
    prescriptions: Mapped[list["Prescription"]] = relationship(
        back_populates="visit", cascade="all, delete-orphan"
    )
    lab_results: Mapped[list["LabResult"]] = relationship(
        back_populates="visit", cascade="all, delete-orphan"
    )
    procedures: Mapped[list["Procedure"]] = relationship(back_populates="visit")
    notes: Mapped[list["VisitNote"]] = relationship(back_populates="visit", cascade="all, delete-orphan")
    service_requests: Mapped[list["ServiceRequest"]] = relationship(back_populates="visit")
    voided_at: Mapped[datetime | None] = mapped_column(DateTime)  # entered in error (chartedit)

    @property
    def follow_up_request(self) -> "ServiceRequest | None":
        """The order saying when this patient should be seen again."""
        for request in self.service_requests:
            if request.kind is ServiceKind.FOLLOW_UP and request.voided_at is None:
                return request
        return None

    @property
    def follow_up_days(self) -> int | None:
        """Days until the requested return visit — DERIVED (issue #59).

        This used to be a column written beside the request. Two writable
        copies of one fact drift, and silently, so the ``FOLLOW_UP``
        ``ServiceRequest`` is now the source of truth and this reads from
        it. ``None`` still means PRN — no return visit was asked for.

        Reading this lazy-loads the visit's requests. Callers in a hot loop
        should use the value they already have (the generator passes the
        condition profile's number straight to ``render_soap``) or
        eager-load ``service_requests``.
        """
        request = self.follow_up_request
        if request is None or request.occurrence_date is None:
            return None
        return (request.occurrence_date - self.visit_date).days


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
    voided_at: Mapped[datetime | None] = mapped_column(DateTime)  # entered in error (chartedit)

    visit: Mapped["Visit"] = relationship(back_populates="vitals")


class Prescription(Base):
    """A medication ordered or continued at one visit (the order event —
    the cross-visit medication list lives in MedicationStatement)."""

    __tablename__ = "prescriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id: Mapped[int] = mapped_column(ForeignKey("visits.id"))
    request_id: Mapped[int | None] = mapped_column(ForeignKey("service_requests.id"))
    drug_name: Mapped[str] = mapped_column(String(100))
    drug_class: Mapped[str | None] = mapped_column(String(80))
    dose: Mapped[str | None] = mapped_column(String(40))
    frequency: Mapped[str | None] = mapped_column(String(40))
    duration_days: Mapped[int | None] = mapped_column(Integer)
    refills: Mapped[int | None] = mapped_column(Integer, default=0)
    is_new: Mapped[bool | None] = mapped_column(Boolean, default=True)  # False = continuation
    voided_at: Mapped[datetime | None] = mapped_column(DateTime)  # entered in error (chartedit)

    visit: Mapped["Visit"] = relationship(back_populates="prescriptions")
    request: Mapped["ServiceRequest | None"] = relationship(back_populates="prescriptions")


class LabResult(Base):
    """A LOINC-coded lab value with reference range and status flag.

    ``value`` is nullable because a result is not always a number. A urine
    culture reads "no growth", a pregnancy test "positive", a sensitive
    troponin "<0.01" — OMOP carries these as ``value_as_concept_id`` and
    ``operator_concept_id``, and without them whole classes of real result
    are unstorable (design service-requests §3).
    """

    __tablename__ = "lab_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id: Mapped[int] = mapped_column(ForeignKey("visits.id"))
    request_id: Mapped[int | None] = mapped_column(ForeignKey("service_requests.id"))
    test_name: Mapped[str] = mapped_column(String(100))
    value: Mapped[float | None] = mapped_column(Float)
    value_text: Mapped[str | None] = mapped_column(String(120))  # "positive", "no growth"
    comparator: Mapped[str | None] = mapped_column(String(2))  # "<", ">", "<=", ">="
    unit: Mapped[str | None] = mapped_column(String(20))
    reference_low: Mapped[float | None] = mapped_column(Float)
    reference_high: Mapped[float | None] = mapped_column(Float)
    status: Mapped[LabStatus] = mapped_column(SAEnum(LabStatus))
    loinc_code: Mapped[str | None] = mapped_column(String(10))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime)  # entered in error (chartedit)

    visit: Mapped["Visit"] = relationship(back_populates="lab_results")
    request: Mapped["ServiceRequest | None"] = relationship(back_populates="lab_results")


class ServiceRequest(Base):
    """Something the chart ASKED FOR: a drug, a panel, a referral, a
    procedure, a return visit (design service-requests-and-interchange.md).

    The chart could previously record only what HAPPENED. A lab existed
    only once it had a result, so "basic metabolic panel before the next
    visit" had nowhere to live, and a referral was smuggled through a
    condition's drug formulary.

    Three deliberate choices, all from §2:

    - **``code`` is nullable.** A request is real before it is coded —
      that is the point of ordering it. An uncoded request is legitimate
      state, and exactly what the LOINC and RxNorm modules will fill in.
      Refuse-don't-guess: we never invent a code to satisfy a column.
    - **``reason_condition_id`` persists the TREATS relation.**
      Comprehension already derives "lisinopril *for hypertension*" and
      then discarded it after FHIR export. This is where it lands.
    - **``detail`` is JSON for the long tail only.** The fields OMOP shows
      are load-bearing are real columns; genuinely kind-specific extras
      (panel members, referral specialty) stay in JSON rather than four
      kinds' worth of mostly-NULL columns.
    """

    __tablename__ = "service_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    visit_id: Mapped[int | None] = mapped_column(ForeignKey("visits.id"))
    requester_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id"))
    kind: Mapped[ServiceKind] = mapped_column(SAEnum(ServiceKind))
    status: Mapped[RequestStatus] = mapped_column(SAEnum(RequestStatus))
    origin: Mapped[RequestOrigin] = mapped_column(SAEnum(RequestOrigin))
    display: Mapped[str] = mapped_column(String(200))  # "Basic metabolic panel"
    code_system: Mapped[str | None] = mapped_column(String(20))  # loinc | rxnorm | snomed_ct
    code: Mapped[str | None] = mapped_column(String(40))  # None until a coder resolves it
    reason_condition_id: Mapped[int | None] = mapped_column(ForeignKey("conditions.id"))
    requested_date: Mapped[date] = mapped_column(Date)
    occurrence_date: Mapped[date | None] = mapped_column(Date)  # "before the next visit"
    end_date: Mapped[date | None] = mapped_column(Date)  # explicit, never derived (§3)
    quantity: Mapped[float | None] = mapped_column(Float)
    route: Mapped[str | None] = mapped_column(String(40))
    sig: Mapped[str | None] = mapped_column(String(300))  # verbatim directions
    stop_reason: Mapped[str | None] = mapped_column(String(200))  # why it ended early
    detail: Mapped[dict | None] = mapped_column(JSON)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime)  # entered in error (chartedit)

    patient: Mapped["Patient"] = relationship()
    visit: Mapped["Visit | None"] = relationship(back_populates="service_requests")
    requester: Mapped["Provider | None"] = relationship()
    reason_condition: Mapped["Condition | None"] = relationship()
    # Fulfilment points BACK at the order, because one order can be
    # fulfilled many times: a basic metabolic panel returns eight results,
    # and a prescription is dispensed again at every refill. §4 draws this
    # as `fulfilled_by` from the request, which is the reading direction —
    # the foreign key has to sit on the many side to say it.
    prescriptions: Mapped[list["Prescription"]] = relationship(back_populates="request")
    lab_results: Mapped[list["LabResult"]] = relationship(back_populates="request")

    @property
    def fulfilled_by(self) -> list:
        """Everything that came back for this order, whatever its kind."""
        return [*self.prescriptions, *self.lab_results]


# ─── The rest of the chart ────────────────────────────────────────────────────


class Allergy(Base):
    """A structured allergy: substance, reaction, severity."""

    __tablename__ = "allergies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    substance: Mapped[str] = mapped_column(String(100))
    reaction: Mapped[str | None] = mapped_column(String(100))
    severity: Mapped[AllergySeverity | None] = mapped_column(SAEnum(AllergySeverity))
    noted_date: Mapped[date | None] = mapped_column(Date)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime)  # entered in error (chartedit)

    patient: Mapped["Patient"] = relationship(back_populates="allergies")


class MedicationStatement(Base):
    """The cross-visit medication list: what the patient is (or was) on,
    with status and indication — fed by Prescription order events."""

    __tablename__ = "medication_statements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    drug_name: Mapped[str] = mapped_column(String(100))
    drug_class: Mapped[str | None] = mapped_column(String(80))
    dose: Mapped[str | None] = mapped_column(String(40))
    frequency: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[MedicationStatus] = mapped_column(
        SAEnum(MedicationStatus), default=MedicationStatus.ACTIVE
    )
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    indication_id: Mapped[int | None] = mapped_column(ForeignKey("conditions.id"))

    patient: Mapped["Patient"] = relationship(back_populates="medications")
    indication: Mapped["Condition | None"] = relationship()


class Procedure(Base):
    """A performed procedure/intervention, optionally tied to a visit."""

    __tablename__ = "procedures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    visit_id: Mapped[int | None] = mapped_column(ForeignKey("visits.id"))
    description: Mapped[str] = mapped_column(String(200))
    performed_date: Mapped[date | None] = mapped_column(Date)
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id"))

    patient: Mapped["Patient"] = relationship(back_populates="procedures")
    visit: Mapped["Visit | None"] = relationship(back_populates="procedures")
    provider: Mapped["Provider | None"] = relationship()


class Immunization(Base):
    """One administered vaccine dose."""

    __tablename__ = "immunizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    vaccine: Mapped[str] = mapped_column(String(100))
    cvx_code: Mapped[str | None] = mapped_column(String(10))
    administered_date: Mapped[date] = mapped_column(Date)
    dose_number: Mapped[int | None] = mapped_column(Integer)

    patient: Mapped["Patient"] = relationship(back_populates="immunizations")


class VisitNote(Base):
    """The stored clinical note for a visit — deterministic SOAP text by
    default; the comprehension service's input."""

    __tablename__ = "visit_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visit_id: Mapped[int] = mapped_column(ForeignKey("visits.id"))
    note_type: Mapped[NoteType] = mapped_column(SAEnum(NoteType), default=NoteType.SOAP)
    text: Mapped[str] = mapped_column(Text)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id"))
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow)

    visit: Mapped["Visit"] = relationship(back_populates="notes")
    author: Mapped["Provider | None"] = relationship()


class ChartAuditEvent(Base):
    """One recorded change to one chart row — who, what, when, why.

    Append-only by construction: nothing in the codebase updates or
    deletes these rows, which is what makes an agent-maintained chart
    auditable (design chart-maintenance.md §3.3). ``before``/``after``
    hold only the touched fields, so the trail stays readable.
    """

    __tablename__ = "chart_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    actor_name: Mapped[str] = mapped_column(String(120))
    actor_source: Mapped[EditSource] = mapped_column(SAEnum(EditSource))
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id"))
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    entity: Mapped[str] = mapped_column(String(40))
    row_id: Mapped[int] = mapped_column(Integer)
    action: Mapped[AuditAction] = mapped_column(SAEnum(AuditAction))
    reason: Mapped[str] = mapped_column(String(400), default="")
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)

    patient: Mapped["Patient"] = relationship()
    provider: Mapped["Provider | None"] = relationship()

    __table_args__ = (Index("ix_chart_audit_patient", "patient_id", "occurred_at"),)


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


def tool_guard(session):
    """Decorator for agent tools sharing one session: an exception rolls
    the transaction back and becomes a readable message for the model.

    PostgreSQL aborts the whole transaction after any failed statement
    (``InFailedSqlTransaction``) — without the rollback, one bad query
    poisons every later tool call in the chat. The returned message keeps
    the agent's retry-with-feedback loop alive instead of surfacing a
    traceback."""
    import functools

    def decorator(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as err:  # noqa: BLE001 — any tool failure must reset the session
                if session is not None:
                    session.rollback()
                return f"Tool failed ({type(err).__name__}): {err} — the transaction was reset; adjust the arguments and retry."

        return wrapped

    return decorator


# Voided rows stop being visible to ORM reads the moment this module is
# imported — voiding is meaningless if every reader still returns the row
# (design chart-maintenance.md §3.3). Opt back in per query with
# ``execution_options(include_voided=True)``.
from hdh.core.chartedit.visibility import install as _install_void_filter  # noqa: E402

_install_void_filter()
