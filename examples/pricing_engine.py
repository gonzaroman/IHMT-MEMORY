"""Pricing engine for the warehouse checkout flow.

Demonstrates AST-driven chunking: imports, module-level constants, functions
and classes each land in their own block, and no definition is ever cut.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

DEFAULT_VAT = 0.21
BULK_THRESHOLD = 50
CURRENCY = "EUR"


@dataclass(frozen=True)
class LineItem:
    """One line of a customer order."""

    sku: str
    quantity: int
    unit_price: float
    discount_code: Optional[str] = None

    @property
    def gross(self) -> float:
        """Price before discounts and tax."""
        return self.quantity * self.unit_price


def apply_bulk_discount(item: LineItem) -> float:
    """Return the discounted subtotal for a single line.

    Orders of at least ``BULK_THRESHOLD`` units get 8% off; anything above
    ten times that gets 15%.
    """
    if item.quantity >= BULK_THRESHOLD * 10:
        return item.gross * 0.85
    if item.quantity >= BULK_THRESHOLD:
        return item.gross * 0.92
    return item.gross


def round_currency(value: float) -> float:
    """Round half-up to two decimals, the way invoices expect."""
    return math.floor(value * 100 + 0.5) / 100


class PricingEngine:
    """Computes order totals, applying discounts and VAT in the right order."""

    def __init__(self, vat_rate: float = DEFAULT_VAT, promotions: Optional[Dict[str, float]] = None) -> None:
        if not 0.0 <= vat_rate < 1.0:
            raise ValueError(f"vat_rate must lie in [0, 1): {vat_rate}")
        self.vat_rate = vat_rate
        self.promotions = promotions or {}

    def subtotal(self, items: Iterable[LineItem]) -> float:
        """Sum of line subtotals after bulk and promotional discounts."""
        total = 0.0
        for item in items:
            line = apply_bulk_discount(item)
            if item.discount_code and item.discount_code in self.promotions:
                line *= 1.0 - self.promotions[item.discount_code]
            total += line
        return round_currency(total)

    def tax(self, subtotal: float) -> float:
        """VAT owed on a subtotal."""
        return round_currency(subtotal * self.vat_rate)

    def total(self, items: Iterable[LineItem]) -> float:
        """Grand total: discounted subtotal plus VAT."""
        subtotal = self.subtotal(items)
        return round_currency(subtotal + self.tax(subtotal))

    def breakdown(self, items: Iterable[LineItem]) -> Dict[str, float]:
        """Itemized totals, suitable for rendering on an invoice."""
        materialized: List[LineItem] = list(items)
        subtotal = self.subtotal(materialized)
        tax = self.tax(subtotal)
        return {
            "subtotal": subtotal,
            "tax": tax,
            "total": round_currency(subtotal + tax),
            "lines": float(len(materialized)),
        }


def quote(items: Iterable[LineItem], engine: Optional[PricingEngine] = None) -> str:
    """Render a human-readable quote for a set of line items."""
    engine = engine or PricingEngine()
    figures = engine.breakdown(items)
    return (
        f"{int(figures['lines'])} lines · subtotal {figures['subtotal']:.2f} {CURRENCY} · "
        f"VAT {figures['tax']:.2f} · total {figures['total']:.2f} {CURRENCY}"
    )
