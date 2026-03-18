"""
Makro crawler config: fixed searches, category constraints.
"""
from dataclasses import dataclass


@dataclass
class SearchConfig:
    """Single search configuration."""
    name: str
    url: str
    category: str  # e.g. "food-products", "preowned-mobiles"
    requires_session: bool = False  # True for URLs that trigger human verification


# Seed search inputs from plan (dedupe repeated preowned URL)
SEARCHES: list[SearchConfig] = [
    SearchConfig(
        name="Food Products 50%+",
        url="https://www.makro.co.za/food-products/pr?sid=eat&otracker=categorytree&p%5B%5D=facets.discount_range_v1%255B%255D%3D50%2525%2Bor%2Bmore",
        category="food-products",
        requires_session=False,
    ),
    SearchConfig(
        name="Preowned Mobiles",
        url="https://www.makro.co.za/all/mobiles-accessories/preowned-mobiles/pr?sid=all,tyy,yiu&otracker=categorytree",
        category="preowned-mobiles",
        requires_session=True,
    ),
]

# Category slugs for validation
CATEGORY_SLUGS = {"food-products", "preowned-mobiles"}
