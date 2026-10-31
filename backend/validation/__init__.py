"""Validation engine — does a candidate model behave like the original on real requests? See engine.py."""
from validation.spec import Spec, Item, Candidate
from validation.engine import validate, validate_many

__all__ = ["Spec", "Item", "Candidate", "validate", "validate_many"]
