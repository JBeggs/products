"""Per-supplier timeouts for Edit Products → Verify all."""

from __future__ import annotations

# HTTP / JSON-LD verify is usually fast; browser suppliers need longer.
DEFAULT_VERIFY_TIMEOUT_S = 20.0

VERIFY_TIMEOUT_BY_SOURCE: dict[str, float] = {
    # Real Chrome via CDP: window open, login/slider, goto PDP, rawData/price
    "temu": 120.0,
    # Headless Playwright batch browser (nav up to 45s + content wait)
    "gumtree": 60.0,
    "hekpoorthoneyfarms": 60.0,
    "ahm": 60.0,
    "tsawelding": 60.0,
}

# One-time Temu Chrome attach + optional login/slider (not counted per product)
TEMU_PREWARM_TIMEOUT_S = 180.0


def verify_timeout_for(source: str) -> float:
    return VERIFY_TIMEOUT_BY_SOURCE.get((source or "").strip(), DEFAULT_VERIFY_TIMEOUT_S)
