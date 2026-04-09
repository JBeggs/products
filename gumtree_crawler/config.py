"""
Config defaults for the Gumtree crawler.

The crawler persists scenario config in SQLite, but these defaults seed first-run data
and keep the rule model in one place instead of spreading hardcoded values through the
crawler and UI.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class SearchConfig:
    """Single Gumtree search target used by one or more scenarios."""

    name: str
    url: str
    category: str
    min_price: int
    max_price: int
    seller_type: str = "owner"
    path_slugs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ScenarioConfig:
    """Scenario definition used to evaluate and display listings."""

    slug: str
    name: str
    description: str
    enabled: bool
    category: str
    searches: list[SearchConfig]
    min_price: int
    max_price: int
    required_keywords_all: list[str] = field(default_factory=list)
    required_any_groups: list[list[str]] = field(default_factory=list)
    excluded_keywords: list[str] = field(default_factory=list)
    urgency_keywords: list[str] = field(default_factory=list)
    strong_urgency_keywords: list[str] = field(default_factory=list)
    min_year: int | None = None
    max_year: int | None = None
    require_year: bool = False
    required_fields_all: list[str] = field(default_factory=list)
    required_attribute_keys: list[str] = field(default_factory=list)
    min_numeric: dict[str, float] = field(default_factory=dict)
    max_numeric: dict[str, float] = field(default_factory=dict)
    seller_allowlist: list[str] = field(default_factory=list)
    seller_denylist: list[str] = field(default_factory=list)
    sort_weights: dict[str, float] = field(default_factory=dict)


DEFAULT_LOCATION_PREFERENCES: dict[str, Any] = {
    "preferred_provinces": ["Gauteng"],
    "preferred_cities": ["Pretoria", "Brits"],
    "preferred_suburbs": [],
    "weights": {
        "province": 20,
        "city": 35,
        "suburb": 45,
    },
}


def _search(
    name: str,
    url: str,
    category: str,
    min_price: int,
    max_price: int,
    *,
    seller_type: str = "owner",
    path_slugs: list[str] | None = None,
) -> SearchConfig:
    return SearchConfig(
        name=name,
        url=url,
        category=category,
        min_price=min_price,
        max_price=max_price,
        seller_type=seller_type,
        path_slugs=path_slugs or [],
    )


DEFAULT_SCENARIOS: list[ScenarioConfig] = [
    ScenarioConfig(
        slug="motor-bikes",
        name="Motor Bikes",
        description="Sports bikes between R5,000 and R75,000 with model and year checks.",
        enabled=True,
        category="motorcycles",
        searches=[
            _search(
                "Motorcycles & scooters",
                "https://www.gumtree.co.za/s-motorcycles-scooters/v1c9027p1?pr=5000,75000&st=ownr",
                "motorcycles",
                5000,
                75000,
                path_slugs=["motorcycles-scooters", "motorcycles", "scooters"],
            ),
        ],
        min_price=5000,
        max_price=75000,
        required_any_groups=[
            ["yamaha r6", "r6"],
            ["yamaha r1", "r1"],
            ["kawasaki zx9", "zx9", "zx-9"],
            ["kawasaki zx10", "zx10", "zx-10"],
        ],
        excluded_keywords=["for parts", "spares only", "non runner"],
        urgency_keywords=["urgent sale", "must sell", "price reduced", "negotiable"],
        strong_urgency_keywords=["relocating", "relocation", "moving", "immigrating", "desperate"],
        min_year=2005,
        require_year=True,
        required_fields_all=["price", "location", "description"],
        required_attribute_keys=["year"],
        seller_allowlist=["owner", "private"],
        seller_denylist=["dealer"],
        sort_weights={"match": 0.55, "location": 0.25, "price": 0.20},
    ),
    ScenarioConfig(
        slug="ai-hardware",
        name="AI Hardware",
        description="Desktop computers with discrete graphics cards and enough RAM to be useful.",
        enabled=True,
        category="desktop-computers",
        searches=[
            _search(
                "Desktop computers",
                "https://www.gumtree.co.za/s-desktop-computers/v1c9436p1?pr=10000,20000",
                "desktop-computers",
                10000,
                20000,
                path_slugs=["desktop-computers", "gaming-pcs", "computers-laptops", "computers"],
            ),
            _search(
                "Computers & laptops",
                "https://www.gumtree.co.za/s-computers-laptops/v1c9199p1?pr=9000,18000",
                "laptops",
                9000,
                18000,
                path_slugs=["gaming-pcs", "desktop-computers", "computers-laptops", "pc-laptops", "laptops"],
            ),
        ],
        min_price=10000,
        max_price=20000,
        required_any_groups=[["rtx", "gtx", "quadro", "radeon", "graphics card", "gpu"]],
        excluded_keywords=["monitor only", "case only", "broken", "wanted"],
        urgency_keywords=["urgent sale", "need cash", "negotiable", "price reduced"],
        strong_urgency_keywords=["relocating", "moving", "must go", "desperate"],
        required_fields_all=["price", "location", "description"],
        required_attribute_keys=["gpu_model", "system_ram_gb"],
        min_numeric={"system_ram_gb": 32},
        seller_allowlist=["owner", "private"],
        seller_denylist=["dealer"],
        sort_weights={"match": 0.60, "location": 0.15, "price": 0.25},
    ),
    ScenarioConfig(
        slug="personal-transport",
        name="Personal Transport",
        description="Affordable scooters, e-bikes, skateboards, and off-road bikes.",
        enabled=True,
        category="personal-transport",
        searches=[
            _search(
                "Motorcycles & scooters",
                "https://www.gumtree.co.za/s-motorcycles-scooters/v1c9027p1?pr=5000,15000&st=ownr",
                "motorcycles",
                5000,
                15000,
                path_slugs=["motorcycles-scooters", "motorcycles", "scooters"],
            ),
            _search(
                "Bicycles",
                "https://www.gumtree.co.za/s-bicycles/v1q0p1?pr=5000,15000",
                "bicycles",
                5000,
                15000,
                path_slugs=["bicycles", "bicycle", "bike"],
            ),
            _search(
                "Skateboarding gear",
                "https://www.gumtree.co.za/s-skateboarding+gear/v1q0p1?pr=5000,15000",
                "skateboarding",
                5000,
                15000,
                path_slugs=["skateboarding", "skateboard"],
            ),
        ],
        min_price=5000,
        max_price=15000,
        required_any_groups=[["scooter", "skateboard", "e bike", "ebike", "off road", "dirt bike"]],
        excluded_keywords=["kids toy", "helmet only", "for parts"],
        urgency_keywords=["urgent", "need cash", "price reduced"],
        strong_urgency_keywords=["moving", "relocating", "must go"],
        required_fields_all=["price", "location", "description"],
        seller_allowlist=["owner", "private"],
        seller_denylist=["dealer"],
        sort_weights={"match": 0.50, "location": 0.30, "price": 0.20},
    ),
    ScenarioConfig(
        slug="cars",
        name="Cars",
        description="Cars and bakkies between R30,000 and R100,000 with urgency and price-change signals.",
        enabled=True,
        category="cars-bakkies",
        searches=[
            _search(
                "Cars & bakkies",
                "https://www.gumtree.co.za/s-cars-bakkies/v1c9077p1?pr=30000,100000&st=ownr",
                "cars-bakkies",
                30000,
                100000,
                path_slugs=["cars-bakkies", "cars", "bakkies"],
            ),
        ],
        min_price=30000,
        max_price=100000,
        excluded_keywords=["for parts", "spares", "strip"],
        urgency_keywords=["urgent sale", "need cash", "must sell", "price reduced"],
        strong_urgency_keywords=["relocating", "moving", "immigrating", "must go", "desperate"],
        max_year=2005,
        require_year=True,
        required_fields_all=["price", "location", "description"],
        required_attribute_keys=["year"],
        seller_allowlist=["owner", "private"],
        seller_denylist=["dealer"],
        sort_weights={"match": 0.50, "location": 0.20, "price": 0.30},
    ),
    ScenarioConfig(
        slug="laptops",
        name="Laptops",
        description="Laptops with enough visible spec and condition data to resell confidently.",
        enabled=True,
        category="laptops",
        searches=[
            _search(
                "Computers & laptops",
                "https://www.gumtree.co.za/s-computers-laptops/v1c9199p1?pr=1000,20000",
                "laptops",
                1000,
                20000,
                path_slugs=["computers-laptops", "computers", "laptops"],
            ),
        ],
        min_price=1000,
        max_price=20000,
        excluded_keywords=["broken", "for parts", "wanted"],
        urgency_keywords=["urgent sale", "need cash", "negotiable"],
        strong_urgency_keywords=["moving", "relocating", "must go"],
        required_fields_all=["price", "location", "description"],
        required_attribute_keys=["system_ram_gb", "storage_gb"],
        seller_allowlist=["owner", "private"],
        seller_denylist=["dealer"],
        sort_weights={"match": 0.55, "location": 0.20, "price": 0.25},
    ),
    ScenarioConfig(
        slug="cell-phones",
        name="Cell Phones",
        description="Phone bargains with usable specs, avoiding low-value burner devices.",
        enabled=True,
        category="cell-phones",
        searches=[
            _search(
                "Cell phones",
                "https://www.gumtree.co.za/s-cell-phones/v1c9419p1?pr=500,5000",
                "cell-phones",
                500,
                5000,
                path_slugs=["cell-phones", "cell-phones-accessories", "phones"],
            ),
        ],
        min_price=500,
        max_price=5000,
        required_any_groups=[["iphone", "samsung", "pixel", "xiaomi", "huawei", "smartphone"]],
        excluded_keywords=[
            "burner",
            "feature phone",
            "nokia 105",
            "itel",
            "vodacom smart",
            "telkom easy",
            "broken screen",
            "cracked screen",
        ],
        urgency_keywords=["urgent sale", "need cash", "price reduced"],
        strong_urgency_keywords=["moving", "relocating", "must go"],
        required_fields_all=["price", "location", "description"],
        required_attribute_keys=["phone_storage_gb"],
        seller_allowlist=["owner", "private"],
        seller_denylist=["dealer"],
        sort_weights={"match": 0.55, "location": 0.15, "price": 0.30},
    ),
]


def scenario_to_dict(scenario: ScenarioConfig) -> dict[str, Any]:
    """Convert a scenario dataclass to a JSON-serializable dict."""

    return asdict(scenario)


def get_default_scenarios() -> list[dict[str, Any]]:
    """Return deep-copied default scenarios for DB seeding."""

    return [scenario_to_dict(s) for s in deepcopy(DEFAULT_SCENARIOS)]


def get_default_location_preferences() -> dict[str, Any]:
    """Return deep-copied default location preferences."""

    return deepcopy(DEFAULT_LOCATION_PREFERENCES)
