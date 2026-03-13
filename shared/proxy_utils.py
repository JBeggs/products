"""
Free proxy utilities for Playwright scrapers.
Uses ProxyScrape API: https://api.proxyscrape.com/v2/
"""
import urllib.request
import urllib.error

PROXY_COUNTRIES = [
    ("ZA", "South Africa"),
    ("US", "United States"),
    ("GB", "United Kingdom"),
    ("DE", "Germany"),
    ("FR", "France"),
    ("AU", "Australia"),
    ("CA", "Canada"),
    ("NL", "Netherlands"),
    ("IN", "India"),
    ("IT", "Italy"),
    ("ES", "Spain"),
    ("JP", "Japan"),
    ("BR", "Brazil"),
    ("PL", "Poland"),
    ("MX", "Mexico"),
]


def fetch_free_proxy(country_code: str, max_retries: int = 3) -> str | None:
    """
    Fetch a free HTTPS proxy for the given country from ProxyScrape API.
    Returns 'http://ip:port' or None if none available or API fails.
    Tries up to max_retries different proxies from the list.
    """
    url = (
        "https://api.proxyscrape.com/v2/"
        f"?request=displayproxies&protocol=https&country={country_code.upper()}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8", errors="ignore").strip()
    except (urllib.error.URLError, OSError, Exception):
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        if i >= max_retries:
            break
        parts = line.split(":")
        if len(parts) >= 2:
            ip, port = parts[0], parts[1]
            if ip and port and ip.replace(".", "").isdigit():
                return f"http://{ip}:{port}"
    return None
