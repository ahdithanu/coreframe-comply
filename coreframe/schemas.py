"""Core data contracts for Coreframe Comply.

Two schemas matter:
  * Rule     - what the LLM extracts from a routing guide.
  * Shipment - what the deterministic checker evaluates rules against.

The bridge between them is `Parameter.target`: a dotted path into Shipment
drawn from a fixed registry (CHECKABLE_TARGETS). The LLM may only mark a rule
`parameterized` if every parameter maps to a registered target. Anything else
is `needs_human`. That's what keeps pass/fail decisions out of the model.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# --------------------------------------------------------------------------- #
# Shared enums
# --------------------------------------------------------------------------- #

class Category(str, Enum):
    labeling = "labeling"
    asn_edi = "asn_edi"
    carton = "carton"
    pallet = "pallet"
    routing_carrier = "routing_carrier"
    appointment = "appointment"
    documentation = "documentation"
    chargeback_policy = "chargeback_policy"
    other = "other"


class Operator(str, Enum):
    eq = "eq"            # value: scalar
    lte = "lte"          # value: number
    gte = "gte"          # value: number
    between = "between"  # value: [low, high], inclusive
    in_ = "in"           # value: list of allowed scalars
    before = "before"    # value: duration; target must occur >= value before `reference`
    exists = "exists"    # value: true => target must be present/non-empty; false => must be absent


class Confidence(str, Enum):
    low = "low"
    med = "med"
    high = "high"


class RuleStatus(str, Enum):
    parameterized = "parameterized"
    needs_human = "needs_human"


# Shipment fields the checker knows how to evaluate: target -> (type, unit, meaning).
# `[]` = applies to every element of that list. Extending this is a code change + a
# test, on purpose. The extraction prompt is rendered from this table.
TARGETS: dict[str, tuple[str, str | None, str]] = {
    "shipment.carrier":            ("string", None, "carrier name"),
    "shipment.shipment_type":      ("string", None, "parcel | ltl | tl | intermodal | other"),
    "shipment.destination_dc":     ("string", None, "destination DC identifier, e.g. '6012'"),
    "shipment.asn_sent_at":        ("datetime", None, "when the ASN (EDI 856) was sent; absent if never sent"),
    "shipment.appointment_time":   ("datetime", None, "scheduled delivery appointment; absent if none"),
    "shipment.ship_date":          ("date", None, "date the shipment left the facility"),
    "shipment.documents":          ("list[string]", None, "documents included, e.g. BOL, packing_list, commercial_invoice"),
    "cartons[].length_in":         ("number", "in", "carton length"),
    "cartons[].width_in":          ("number", "in", "carton width"),
    "cartons[].height_in":         ("number", "in", "carton height"),
    "cartons[].weight_lbs":        ("number", "lbs", "carton gross weight"),
    "cartons[].label_type":        ("string", None, "shipping label symbology, e.g. GS1-128"),
    "cartons[].label_position":    ("string", None, "where the label sits, snake_case"),
    "cartons[].sscc_present":      ("bool", None, "carton label carries a scannable SSCC-18"),
    "pallets[].height_in":         ("number", "in", "pallet height including the pallet"),
    "pallets[].weight_lbs":        ("number", "lbs", "gross pallet weight"),
    "pallets[].footprint":         ("string", "in", "pallet footprint 'LxW', e.g. '40x48'"),
    "pallets[].stacked":           ("bool", None, "pallet is double-stacked"),
    "pallets[].label_positions":   ("list[string]", None, "sides/positions carrying a pallet label"),
}
CHECKABLE_TARGETS: frozenset[str] = frozenset(TARGETS)

# Fields an applies_to condition may key on.
CONDITION_FIELDS: frozenset[str] = frozenset({
    "shipment_type", "carrier", "destination_dc", "product_type",
})

Scalar = Union[float, int, str, bool]


# --------------------------------------------------------------------------- #
# Rule
# --------------------------------------------------------------------------- #

class Parameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Human-readable name, e.g. 'max carton weight'.")
    target: str | None = Field(
        default=None,
        description="Shipment field path from CHECKABLE_TARGETS. Null => rule cannot be auto-checked.",
    )
    operator: Operator
    value: Union[Scalar, list[Scalar]]
    unit: str | None = Field(default=None, description="e.g. in, lbs, cm, kg, hours, days. Null for enums/bools.")
    reference: str | None = Field(
        default=None,
        description="Only for operator='before': the target this must precede, e.g. 'shipment.appointment_time'.",
    )

    @model_validator(mode="after")
    def _check_operator_shape(self) -> "Parameter":
        op, v = self.operator, self.value
        if op == Operator.between:
            if not (isinstance(v, list) and len(v) == 2):
                raise ValueError("operator 'between' needs value=[low, high]")
        elif op == Operator.in_:
            if not (isinstance(v, list) and v):
                raise ValueError("operator 'in' needs a non-empty list value")
        elif isinstance(v, list):
            raise ValueError(f"operator '{op.value}' takes a scalar value, got a list")
        if op in (Operator.lte, Operator.gte) and (isinstance(v, bool) or not isinstance(v, (int, float))):
            raise ValueError(f"operator '{op.value}' needs a numeric value")
        if op == Operator.exists and not isinstance(v, bool):
            raise ValueError("operator 'exists' needs a boolean value")
        if op == Operator.before:
            if self.reference is None:
                raise ValueError("operator 'before' needs a reference field")
            if not self.unit:
                raise ValueError("operator 'before' needs a duration unit (minutes/hours/days)")
        elif self.reference is not None:
            raise ValueError("reference is only valid with operator 'before'")
        if self.target is not None and self.target not in CHECKABLE_TARGETS:
            raise ValueError(f"unknown target '{self.target}'")
        return self


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    operator: Literal["eq", "in", "not_in"]
    value: Union[str, list[str]]

    @field_validator("field")
    @classmethod
    def _known_field(cls, v: str) -> str:
        if v not in CONDITION_FIELDS:
            raise ValueError(f"applies_to field must be one of {sorted(CONDITION_FIELDS)}")
        return v


class Chargeback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: float | None = Field(default=None, description="Numeric amount if a flat/unit fee is stated.")
    currency: str = "USD"
    basis: Literal["flat", "per_carton", "per_pallet", "per_unit", "per_shipment",
                   "percent_of_invoice", "other"] | None = None
    text: str = Field(description="The fee/formula as stated, e.g. '$2.50 per carton, $250 minimum'.")


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1)
    section: str
    snippet: str = Field(description="Verbatim from the guide, under 25 words.")
    verified: bool | None = Field(
        default=None,
        description="Set by code: snippet found on the cited page. None = page has no text layer.",
    )

    @field_validator("snippet")
    @classmethod
    def _short_snippet(cls, v: str) -> str:
        if len(v.split()) >= 25:
            raise ValueError("snippet must be under 25 words")
        return v


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(description="Stable id, e.g. 'acme_v2024.1_labeling_003'.")
    retailer: str
    guide_version: str
    category: Category
    requirement: str = Field(description="Normalized one-sentence statement.")
    parameters: list[Parameter] = Field(default_factory=list)
    applies_to: Union[Literal["all"], list[Condition]] = "all"
    chargeback: Chargeback | None = None
    sources: list[SourceRef] = Field(min_length=1, description="All places this rule appears (after dedup).")
    confidence: Confidence
    status: RuleStatus
    review_note: str | None = Field(
        default=None, description="Why a human should look: unmappable parameter, cross-reference, ambiguity.",
    )

    @model_validator(mode="after")
    def _status_consistent(self) -> "Rule":
        if self.status == RuleStatus.parameterized:
            if not self.parameters:
                raise ValueError("parameterized rule needs at least one parameter")
            untargeted = [p.name for p in self.parameters if p.target is None]
            if untargeted:
                raise ValueError(f"parameterized rule has unmapped parameters {untargeted}; mark needs_human")
        return self


class RuleSet(BaseModel):
    """Contents of data/rules/<retailer>_<version>.json."""
    model_config = ConfigDict(extra="forbid")

    retailer: str
    guide_version: str
    guide_sha256: str
    prompt_version: str
    model: str
    extracted_at: datetime
    rules: list[Rule]


# --------------------------------------------------------------------------- #
# Shipment
# --------------------------------------------------------------------------- #

class Carton(BaseModel):
    model_config = ConfigDict(extra="forbid")

    carton_id: str
    length_in: float = Field(gt=0)
    width_in: float = Field(gt=0)
    height_in: float = Field(gt=0)
    weight_lbs: float = Field(gt=0)
    label_type: str | None = None        # e.g. "GS1-128", "UCC-128"
    label_position: str | None = None    # e.g. "long_side_lower_right"
    sscc_present: bool


class Pallet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pallet_id: str
    height_in: float = Field(gt=0)
    weight_lbs: float = Field(gt=0)
    footprint: str                       # "40x48" (L x W, inches)
    stacked: bool
    label_positions: list[str] = Field(default_factory=list)

    @field_validator("footprint")
    @classmethod
    def _footprint_format(cls, v: str) -> str:
        parts = v.lower().replace(" ", "").split("x")
        if len(parts) != 2 or not all(p.replace(".", "", 1).isdigit() for p in parts):
            raise ValueError("footprint must look like '40x48'")
        return v


class Shipment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shipment_id: str
    retailer: str
    shipment_type: Literal["parcel", "ltl", "tl", "intermodal", "other"] | None = None
    product_type: str | None = None
    ship_date: date
    asn_sent_at: datetime | None = None      # None => ASN not sent
    carrier: str
    appointment_time: datetime | None = None
    destination_dc: str
    cartons: list[Carton] = Field(default_factory=list)
    pallets: list[Pallet] = Field(default_factory=list)
    documents: list[str] = Field(default_factory=list)  # e.g. ["BOL", "packing_list"]
