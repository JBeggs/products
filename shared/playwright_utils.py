"""
Shared Playwright/Chromium launch options for product scrapers.
"""
# Performance args: reduce slowness, GPU hangs, dev-shm issues
CHROMIUM_PERFORMANCE_ARGS = [
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-extensions",
]

# Default page load timeout (ms) - increased for slow connections
PAGE_LOAD_TIMEOUT = 45000

# Verify-all price checks — shorter than session scrape; block heavy assets.
VERIFY_NAV_TIMEOUT_MS = 45_000
VERIFY_CONTENT_TIMEOUT_MS = 20_000


def block_heavy_resources(page) -> None:
    """Skip images/fonts/media during headless verify (JSON-LD/DOM still loads)."""

    def _route(route):
        if route.request.resource_type in {"image", "media", "font"}:
            return route.abort()
        return route.continue_()

    page.route("**/*", _route)
