"""
Gumtree crawler config: fixed searches, category constraints, used/owner checks.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class SearchConfig:
    """Single search configuration."""
    name: str
    url: str
    category: str  # e.g. "laptops", "motorcycles"
    min_price: int
    max_price: int
    seller_type: str = "owner"  # owner = private sellers only


# Seed search inputs from plan
SEARCHES: list[SearchConfig] = [
    SearchConfig(
        name="Motorcycles",
        url="https://www.gumtree.co.za/s-motorcycles-scooters/v1c9027p1?pr=2000,50000&st=ownr",
        category="motorcycles",
        min_price=2000,
        max_price=50000,
        seller_type="owner",
    ),
    SearchConfig(
        name="Laptops",
        url="https://www.gumtree.co.za/s-computers-laptops/v1c9199p1?pr=500,10000&st=ownr",
        category="laptops",
        min_price=500,
        max_price=10000,
        seller_type="owner",
    ),
]

# Category constraints for validation
CATEGORY_SLUGS = {"motorcycles", "laptops"}

# Seller type: only owner/private
REQUIRED_SELLER_TYPE = "owner"

# Condition: used only (owner listings are typically used)
REQUIRED_CONDITION = "used"
