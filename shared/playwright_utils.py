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
