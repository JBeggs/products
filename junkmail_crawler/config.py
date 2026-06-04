"""
Config defaults for the Junk Mail crawler.

Mirrors Gumtree scenario rules with Junk Mail search URLs. Price ranges are applied
in the URL via /pr{min}-{max}/ and enforced again during crawl as a safety net.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any

from junkmail_crawler.parsers import build_junkmail_search_url

JM = "https://www.junkmail.co.za"

# Junk Mail-specific noise in keyword searches (job ads, service listings).
JUNKMAIL_EXTRA_EXCLUDED = [
    "operator",
    "job",
    "vacancy",
    "cv",
    "sales service and repairs",
]


@dataclass(slots=True)
class SearchConfig:
    """Single Junk Mail search target used by one or more scenarios."""

    name: str
    url: str
    category: str
    min_price: int
    max_price: int
    seller_type: str = "owner"
    path_slugs: list[str] = field(default_factory=list)
    query: str | None = None
    category_path: str | None = None
    private_only: bool = False
    required_keywords_all: list[str] = field(default_factory=list)
    required_any_groups: list[list[str]] = field(default_factory=list)
    excluded_keywords: list[str] = field(default_factory=list)


GPU_OR_GROUPS = [["rtx", "gtx", "quadro", "radeon", "graphics card", "gpu"]]

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

# Junk Mail CV ads (job-seekers section) — not employer vacancies.
JOB_SEEKER_CV_EXCLUDED = [
    "looking for work",
    "seeking work",
    "seeking employment",
    "looking for employment",
    "job seeker",
    "seeking a position",
    "seeking position",
    "available for employment",
    "my cv",
    "cv available",
    "resume available",
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
    category: str,
    min_price: int,
    max_price: int,
    *,
    query: str | None = None,
    category_path: str | None = None,
    private_only: bool = False,
    seller_type: str = "owner",
    path_slugs: list[str] | None = None,
    url: str | None = None,
    required_keywords_all: list[str] | None = None,
    required_any_groups: list[list[str]] | None = None,
    excluded_keywords: list[str] | None = None,
) -> SearchConfig:
    if url is None:
        url = build_junkmail_search_url(
            min_price=min_price,
            max_price=max_price,
            query=query,
            category_path=category_path,
            private_only=private_only,
        )
    return SearchConfig(
        name=name,
        url=url,
        category=category,
        min_price=min_price,
        max_price=max_price,
        seller_type=seller_type,
        path_slugs=path_slugs or [],
        query=query,
        category_path=category_path,
        private_only=private_only,
        required_keywords_all=required_keywords_all or [],
        required_any_groups=required_any_groups or [],
        excluded_keywords=excluded_keywords or [],
    )


DEFAULT_SCENARIOS: list[ScenarioConfig] = [
    ScenarioConfig(
        slug="motor-bikes",
        name="Motor Bikes",
        description="Motorcycles and scooters between R5,000 and R75,000 (private listings).",
        enabled=True,
        category="motorcycles",
        searches=[
            _search(
                "Bikes (private)",
                "motorcycles",
                5000,
                75000,
                category_path="bikes",
                private_only=True,
                path_slugs=["bikes", "motorcycle", "scooter"],
            ),
            _search(
                "Motorcycle search",
                "motorcycles",
                5000,
                75000,
                query="motorcycle",
                private_only=True,
                path_slugs=["motorcycle", "bikes", "scooter"],
            ),
            _search(
                "Scooter search",
                "motorcycles",
                5000,
                75000,
                query="scooter",
                private_only=True,
                path_slugs=["scooter", "motorcycle", "bikes"],
            ),
        ],
        min_price=5000,
        max_price=75000,
        required_any_groups=[],
        excluded_keywords=JUNKMAIL_EXTRA_EXCLUDED,
        urgency_keywords=[],
        strong_urgency_keywords=[],
        require_year=False,
        required_fields_all=["price", "location", "description"],
        required_attribute_keys=[],
        seller_allowlist=["owner", "private"],
        seller_denylist=["dealer", "business"],
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
                "Gaming PC",
                "desktop-computers",
                10000,
                20000,
                query="gaming pc",
                category_path="computers-and-gaming",
                path_slugs=["computers-and-gaming", "gaming", "desktop"],
                required_any_groups=GPU_OR_GROUPS,
                excluded_keywords=["monitor only", "case only", "broken", "wanted"]
                + JOB_HUNTING_EXCLUDED
                + JUNKMAIL_EXTRA_EXCLUDED,
            ),
            _search(
                "RTX search",
                "desktop-computers",
                10000,
                20000,
                query="rtx",
                category_path="computers-and-gaming",
                path_slugs=["computers-and-gaming", "gaming", "rtx"],
                required_any_groups=GPU_OR_GROUPS,
                excluded_keywords=["monitor only", "case only", "broken", "wanted"]
                + JOB_HUNTING_EXCLUDED
                + JUNKMAIL_EXTRA_EXCLUDED,
            ),
        ],
        min_price=10000,
        max_price=20000,
        required_any_groups=[],
        excluded_keywords=["monitor only", "case only", "broken", "wanted"] + JUNKMAIL_EXTRA_EXCLUDED,
        urgency_keywords=["urgent sale", "need cash", "negotiable", "price reduced"],
        strong_urgency_keywords=["relocating", "moving", "must go", "desperate"],
        required_fields_all=["price", "location", "description"],
        required_attribute_keys=["gpu_model", "system_ram_gb"],
        min_numeric={"system_ram_gb": 32},
        seller_allowlist=["owner", "private"],
        seller_denylist=["dealer", "business"],
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
                "Bikes (private)",
                "motorcycles",
                5000,
                15000,
                category_path="bikes",
                private_only=True,
                path_slugs=["bikes", "scooter", "ebike"],
            ),
            _search(
                "E-bike search",
                "bicycles",
                5000,
                15000,
                query="e bike",
                path_slugs=["bike", "ebike", "bicycle"],
            ),
            _search(
                "Skateboard search",
                "skateboarding",
                5000,
                15000,
                query="skateboard",
                path_slugs=["skateboard"],
            ),
        ],
        min_price=5000,
        max_price=15000,
        required_any_groups=[["scooter", "skateboard", "e bike", "ebike", "off road", "dirt bike"]],
        excluded_keywords=["kids toy", "helmet only", "for parts"] + JUNKMAIL_EXTRA_EXCLUDED,
        urgency_keywords=["urgent", "need cash", "price reduced"],
        strong_urgency_keywords=["moving", "relocating", "must go"],
        required_fields_all=["price", "location", "description"],
        seller_allowlist=["owner", "private"],
        seller_denylist=["dealer", "business"],
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
                "Used cars (private)",
                "cars-bakkies",
                30000,
                100000,
                category_path="cars/used",
                private_only=True,
                path_slugs=["cars", "used"],
            ),
        ],
        min_price=30000,
        max_price=100000,
        excluded_keywords=["for parts", "spares", "strip"] + JUNKMAIL_EXTRA_EXCLUDED,
        urgency_keywords=["urgent sale", "need cash", "must sell", "price reduced"],
        strong_urgency_keywords=["relocating", "moving", "immigrating", "must go", "desperate"],
        max_year=2005,
        require_year=True,
        required_fields_all=["price", "location", "description"],
        required_attribute_keys=["year"],
        seller_allowlist=["owner", "private"],
        seller_denylist=["dealer", "business"],
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
                "Laptop search",
                "laptops",
                1000,
                20000,
                query="laptop",
                category_path="computers-and-gaming",
                path_slugs=["computers-and-gaming", "laptop"],
            ),
            _search(
                "Gaming laptop",
                "laptops",
                1000,
                20000,
                query="gaming laptop",
                category_path="computers-and-gaming",
                path_slugs=["laptop", "gaming"],
            ),
        ],
        min_price=1000,
        max_price=20000,
        excluded_keywords=["broken", "for parts", "wanted"] + JUNKMAIL_EXTRA_EXCLUDED,
        urgency_keywords=["urgent sale", "need cash", "negotiable"],
        strong_urgency_keywords=["moving", "relocating", "must go"],
        required_fields_all=["price", "location", "description"],
        required_attribute_keys=["system_ram_gb", "storage_gb"],
        seller_allowlist=["owner", "private"],
        seller_denylist=["dealer", "business"],
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
                "iPhone search",
                "cell-phones",
                500,
                5000,
                query="iphone",
                category_path="mobile-devices",
                path_slugs=["mobile", "iphone", "phone"],
            ),
            _search(
                "Samsung Galaxy",
                "cell-phones",
                500,
                5000,
                query="samsung galaxy",
                path_slugs=["mobile", "samsung", "galaxy"],
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
        ]
        + JUNKMAIL_EXTRA_EXCLUDED,
        urgency_keywords=["urgent sale", "need cash", "price reduced"],
        strong_urgency_keywords=["moving", "relocating", "must go"],
        required_fields_all=["price", "location", "description"],
        required_attribute_keys=["phone_storage_gb"],
        seller_allowlist=["owner", "private"],
        seller_denylist=["dealer", "business"],
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
                "laser-cutting",
                8000,
                250000,
                query="laser cutting machine",
                path_slugs=["laser", "cutting", "cutter"],
            ),
            _search(
                "Laser cutter",
                "laser-cutting",
                8000,
                250000,
                query="laser cutter",
                path_slugs=["laser", "cutter"],
            ),
            _search(
                "Fiber laser",
                "laser-cutting",
                8000,
                250000,
                query="fiber laser",
                path_slugs=["laser", "fiber", "fibre"],
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
            "operator",
            "job",
            "cv",
            "sales service and repairs",
        ],
        urgency_keywords=["urgent sale", "must sell", "relocating", "negotiable", "price reduced"],
        strong_urgency_keywords=["moving", "relocating", "must go", "business closing"],
        require_year=False,
        required_fields_all=["price", "location", "description"],
        required_attribute_keys=[],
        seller_allowlist=["owner", "private"],
        seller_denylist=["dealer", "business"],
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
                "t-shirt-printing",
                3000,
                120000,
                query="t shirt printer",
                path_slugs=["printer", "shirt", "garment"],
            ),
            _search(
                "DTF printer",
                "dtf-printing",
                5000,
                120000,
                query="dtf printer",
                path_slugs=["dtf", "printer"],
            ),
            _search(
                "Screen printing machine",
                "screen-printing",
                3000,
                120000,
                query="screen printing machine",
                path_slugs=["screen", "printing", "printer"],
            ),
            _search(
                "Printers category",
                "screen-printing",
                3000,
                120000,
                query="screen",
                category_path="office-and-business/office-equipment/printers-scanners-and-copiers",
                path_slugs=["printer", "screen", "office"],
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
            "operator",
            "job",
            "cv",
            "sales service and repairs",
        ],
        urgency_keywords=["urgent sale", "must sell", "negotiable", "price reduced", "business closing"],
        strong_urgency_keywords=["relocating", "moving", "must go", "upgrading"],
        require_year=False,
        required_fields_all=["price", "location", "description"],
        required_attribute_keys=[],
        seller_allowlist=["owner", "private"],
        seller_denylist=["dealer", "business"],
        sort_weights={"match": 0.50, "location": 0.25, "price": 0.25},
    ),
    ScenarioConfig(
        slug="dev-jobs",
        name="Developer Jobs",
        description="IT developer job ads: Python, React, Django, and full stack roles on Junk Mail.",
        enabled=True,
        category="dev-jobs",
        searches=[
            _search(
                "Python developer",
                "dev-jobs-python",
                0,
                999999,
                query="Python developer",
                category_path="jobs",
                path_slugs=["jobs", "python", "developer"],
                required_any_groups=[
                    [
                        "python",
                        "python developer",
                        "python programmer",
                        "python dev",
                        "python software",
                    ]
                ],
                excluded_keywords=["for sale", "selling", "wanted to buy", "wanted:", "laptop for sale"]
                + JOB_SEEKER_CV_EXCLUDED,
            ),
            _search(
                "React front end developer",
                "dev-jobs-react",
                0,
                999999,
                query="REACT",
                category_path="jobs",
                path_slugs=["jobs", "react", "frontend", "developer"],
                required_any_groups=[
                    [
                        "react",
                        "react developer",
                        "react front end",
                        "react frontend",
                        "front end developer",
                        "frontend developer",
                        "react.js",
                    ]
                ],
                excluded_keywords=["for sale", "selling", "wanted to buy", "wanted:", "laptop for sale"]
                + JOB_SEEKER_CV_EXCLUDED,
            ),
            _search(
                "Django developer",
                "dev-jobs-django",
                0,
                999999,
                query="django",
                category_path="jobs",
                path_slugs=["jobs", "django", "developer"],
                required_any_groups=[["django", "django developer", "django python"]],
                excluded_keywords=["for sale", "selling", "wanted to buy", "wanted:", "laptop for sale"]
                + JOB_SEEKER_CV_EXCLUDED,
            ),
            _search(
                "Full stack developer",
                "dev-jobs-fullstack",
                0,
                999999,
                query="full stack",
                category_path="jobs",
                path_slugs=["jobs", "fullstack", "developer", "full stack"],
                required_any_groups=[
                    [
                        "full stack",
                        "fullstack",
                        "full-stack",
                        "full stack developer",
                        "fullstack developer",
                        "full-stack developer",
                    ]
                ],
                excluded_keywords=["for sale", "selling", "wanted to buy", "wanted:", "laptop for sale"]
                + JOB_SEEKER_CV_EXCLUDED,
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
        ]
        + JOB_SEEKER_CV_EXCLUDED,
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
