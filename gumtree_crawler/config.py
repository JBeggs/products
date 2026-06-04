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
    required_keywords_all: list[str] = field(default_factory=list)
    required_any_groups: list[list[str]] = field(default_factory=list)
    excluded_keywords: list[str] = field(default_factory=list)


# Job-ad noise that should not match hardware searches.
JOB_HUNTING_EXCLUDED = [
    "job vacancy",
    "vacancy",
    "cv for",
    "resume",
    "wanted to buy",
    "wanted:",
    "hiring",
    "position available",
    "apply now",
    "job available",
]

GPU_OR_GROUPS = [["rtx", "gtx", "quadro", "radeon", "graphics card", "gpu"]]

MOTORBIKE_OR_GROUPS = [["motorcycle", "scooter", "motorbike", "bike"]]

JOB_OR_GROUPS = [
    ["job", "vacancy", "hiring", "position"],
    ["developer", "technician", "engineer", "programmer"],
]


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
    require_price: bool = True
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
    required_keywords_all: list[str] | None = None,
    required_any_groups: list[list[str]] | None = None,
    excluded_keywords: list[str] | None = None,
) -> SearchConfig:
    return SearchConfig(
        name=name,
        url=url,
        category=category,
        min_price=min_price,
        max_price=max_price,
        seller_type=seller_type,
        path_slugs=path_slugs or [],
        required_keywords_all=required_keywords_all or [],
        required_any_groups=required_any_groups or [],
        excluded_keywords=excluded_keywords or [],
    )


DEFAULT_SCENARIOS: list[ScenarioConfig] = [
    ScenarioConfig(
        slug="motor-bikes",
        name="Motor Bikes",
        description="Motorcycles and scooters between R5,000 and R75,000 (owner listings).",
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
        required_any_groups=[],
        excluded_keywords=[],
        urgency_keywords=[],
        strong_urgency_keywords=[],
        require_year=False,
        required_fields_all=["price", "location", "description"],
        required_attribute_keys=[],
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
                required_any_groups=GPU_OR_GROUPS,
                excluded_keywords=["monitor only", "case only", "broken", "wanted"] + JOB_HUNTING_EXCLUDED,
            ),
            _search(
                "Computers & laptops",
                "https://www.gumtree.co.za/s-computers-laptops/v1c9199p1?pr=9000,18000",
                "laptops",
                9000,
                18000,
                path_slugs=["gaming-pcs", "desktop-computers", "computers-laptops", "pc-laptops", "laptops"],
                required_any_groups=GPU_OR_GROUPS,
                excluded_keywords=["monitor only", "case only", "broken", "wanted"] + JOB_HUNTING_EXCLUDED,
            ),
            _search(
                "Motorbikes",
                "https://www.gumtree.co.za/s-motorcycles-scooters/v1c9027p1?pr=2500,20000&st=ownr",
                "motorbikes",
                2500,
                20000,
                path_slugs=["motorcycles-scooters", "motorcycles", "scooters"],
                required_any_groups=MOTORBIKE_OR_GROUPS,
                excluded_keywords=JOB_HUNTING_EXCLUDED,
            ),
            _search(
                "Jobs",
                "https://www.gumtree.co.za/s-it-tech-jobs/v1c9050p1",
                "jobs",
                0,
                999999,
                path_slugs=["it-tech-jobs", "jobs"],
                required_any_groups=JOB_OR_GROUPS,
                excluded_keywords=["monitor only", "case only", "rtx", "gtx", "gpu"],
            ),
        ],
        min_price=10000,
        max_price=20000,
        required_any_groups=[],
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
    ScenarioConfig(
        slug="laser-cutters",
        name="Laser Cutters",
        description="CO2, CNC and fibre laser cutters/engravers between R8,000 and R250,000 (owner listings).",
        enabled=True,
        category="laser-cutting",
        searches=[
            _search(
                "Laser cutting machine",
                "https://www.gumtree.co.za/s-laser+cutting+machine/v1q0p1?pr=8000,250000&st=ownr",
                "laser-cutting",
                8000,
                250000,
                path_slugs=["laser-cutting", "laser-cutting-machine", "laser-cutter", "other-power-tools"],
            ),
            _search(
                "Laser cutter",
                "https://www.gumtree.co.za/s-laser+cutter/v1q0p1?pr=8000,250000&st=ownr",
                "laser-cutting",
                8000,
                250000,
                path_slugs=["laser-cutter", "laser-cutting", "other-power-tools"],
            ),
            _search(
                "CNC laser cutting machine",
                "https://www.gumtree.co.za/s-cnc+laser+cutting+machine/v1q0p1?pr=15000,250000&st=ownr",
                "laser-cutting",
                15000,
                250000,
                path_slugs=["laser-cutting", "laser-cutter", "cnc", "industrial-machinery"],
            ),
        ],
        min_price=8000,
        max_price=250000,
        required_any_groups=[
            ["laser cutter", "laser cutting", "co2 laser", "fiber laser", "fibre laser"],
            ["cnc laser", "laser engraver", "laser engraving machine"],
        ],
        excluded_keywords=[
            "job vacancy",
            "vacancy",
            "cv for",
            "resume",
            "wanted to buy",
            "wanted:",
            "laser cleaning only",
            "cleaning machine only",
            "replacement tube only",
            "laser tube only",
        ],
        urgency_keywords=["urgent sale", "must sell", "relocating", "negotiable", "price reduced"],
        strong_urgency_keywords=["moving", "relocating", "must go", "business closing"],
        require_year=False,
        required_fields_all=["price", "location", "description"],
        required_attribute_keys=[],
        seller_allowlist=["owner", "private"],
        seller_denylist=["dealer"],
        sort_weights={"match": 0.50, "location": 0.25, "price": 0.25},
    ),
    ScenarioConfig(
        slug="t-shirt-printing",
        name="T-Shirt Printing",
        description="Screen-print, DTF and garment printing machines between R3,000 and R120,000 (owner listings).",
        enabled=True,
        category="t-shirt-printing",
        searches=[
            _search(
                "T shirt printer",
                "https://www.gumtree.co.za/s-t+shirt+printer/v1q0p1?pr=3000,120000&st=ownr",
                "t-shirt-printing",
                3000,
                120000,
                path_slugs=["t-shirt-printing", "t-shirt-printer", "screen-printing"],
            ),
            _search(
                "Screen printing machine",
                "https://www.gumtree.co.za/s-screen+printing+machine/v1q0p1?pr=3000,120000&st=ownr",
                "screen-printing",
                3000,
                120000,
                path_slugs=["screen-printing", "screen-printing-machine", "t-shirt-printing"],
            ),
            _search(
                "DTF printer",
                "https://www.gumtree.co.za/s-dtf+printer/v1q0p1?pr=5000,120000&st=ownr",
                "dtf-printing",
                5000,
                120000,
                path_slugs=["dtf-printing", "dtf-printer", "t-shirt-printing"],
            ),
        ],
        min_price=3000,
        max_price=120000,
        required_any_groups=[
            ["t shirt printer", "t-shirt printer", "garment printer", "textile printer"],
            ["screen print", "silkscreen", "screen printing machine", "carousel printer"],
            ["dtf printer", "dtf printing", "direct to film"],
        ],
        excluded_keywords=[
            "heat press only",
            "ink only",
            "film only",
            "transfer paper only",
            "consumables only",
            "job vacancy",
            "vacancy",
            "cv for",
            "resume",
            "wanted to buy",
            "wanted:",
        ],
        urgency_keywords=["urgent sale", "must sell", "negotiable", "price reduced", "business closing"],
        strong_urgency_keywords=["relocating", "moving", "must go", "upgrading"],
        require_year=False,
        required_fields_all=["price", "location", "description"],
        required_attribute_keys=[],
        seller_allowlist=["owner", "private"],
        seller_denylist=["dealer"],
        sort_weights={"match": 0.50, "location": 0.25, "price": 0.25},
    ),
    ScenarioConfig(
        slug="dev-jobs",
        name="Developer Jobs",
        description="IT developer job ads: Python, React, Django, and full stack roles on Gumtree.",
        enabled=True,
        category="dev-jobs",
        searches=[
            _search(
                "Python developer",
                "https://www.gumtree.co.za/s-python+developer/v1q0p1",
                "dev-jobs-python",
                0,
                999999,
                path_slugs=["it-tech-jobs", "jobs", "python"],
                required_any_groups=[
                    ["python developer", "python programmer", "python dev", "python software developer"]
                ],
                excluded_keywords=["for sale", "selling", "wanted to buy", "wanted:", "laptop for sale"],
            ),
            _search(
                "React front end developer",
                "https://www.gumtree.co.za/s-react+front+end+developer/v1q0p1",
                "dev-jobs-react",
                0,
                999999,
                path_slugs=["it-tech-jobs", "jobs", "react"],
                required_any_groups=[
                    [
                        "react developer",
                        "react front end",
                        "react frontend",
                        "front end developer",
                        "frontend developer",
                        "react.js",
                    ]
                ],
                excluded_keywords=["for sale", "selling", "wanted to buy", "wanted:", "laptop for sale"],
            ),
            _search(
                "Django developer",
                "https://www.gumtree.co.za/s-django+developer/v1q0p1",
                "dev-jobs-django",
                0,
                999999,
                path_slugs=["it-tech-jobs", "jobs", "django"],
                required_any_groups=[["django", "django developer", "django python"]],
                excluded_keywords=["for sale", "selling", "wanted to buy", "wanted:", "laptop for sale"],
            ),
            _search(
                "Full stack developer",
                "https://www.gumtree.co.za/s-full+stack+developer/v1q0p1",
                "dev-jobs-fullstack",
                0,
                999999,
                path_slugs=["it-tech-jobs", "jobs", "full-stack"],
                required_any_groups=[
                    ["full stack developer", "fullstack developer", "full-stack developer", "full stack dev"]
                ],
                excluded_keywords=["for sale", "selling", "wanted to buy", "wanted:", "laptop for sale"],
            ),
        ],
        min_price=0,
        max_price=999999,
        require_price=False,
        required_any_groups=[],
        excluded_keywords=[
            "for sale",
            "selling my",
            "wanted to buy",
            "wanted:",
            "pc for sale",
            "laptop for sale",
        ],
        urgency_keywords=["urgently hiring", "immediate start", "start asap", "apply now"],
        strong_urgency_keywords=["urgent", "immediate", "asap"],
        require_year=False,
        required_fields_all=["location", "description"],
        required_attribute_keys=[],
        seller_allowlist=[],
        seller_denylist=[],
        sort_weights={"match": 0.65, "location": 0.30, "price": 0.05},
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
