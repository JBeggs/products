"""Registry of supplier browser sessions for setup_browser_sessions.py."""
from __future__ import annotations

import ast
import importlib
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

PRODUCTS_ROOT = Path(__file__).resolve().parent.parent

# Chrome-profile suppliers without module-level BASE_URL
CHROME_PROFILE_LOGIN_FALLBACK: dict[str, str] = {
    "gumtree": "https://www.gumtree.co.za/",
}

# Real Chrome CDP (dedicated setup scripts)
CDP_SUPPLIERS: dict[str, int] = {
    "temu": 9223,
    "junkmail": 9222,
}

# Persistent Chromium user-data dirs (login persists in profile)
CHROME_PROFILE_SLUGS: frozenset[str] = frozenset(
    {"takealot", "game", "constructionhyper", "gumtree"}
)


class SessionKind(str, Enum):
    CDP = "cdp"
    CHROME_PROFILE = "chrome_profile"
    JSON = "json"
    NONE = "none"


@dataclass(frozen=True)
class SupplierSessionEntry:
    slug: str
    display_name: str
    kind: SessionKind
    session_path: Path | None  # json file or chrome_profile dir
    login_url: str | None
    port: int | None = None  # CDP only
    interactive: bool = True

    @property
    def id(self) -> str:
        return self.slug


def _profile_has_session(path: Path) -> bool:
    if not path.is_dir():
        return False
    # Chromium profile is "present" once Default exists or any non-empty child
    default = path / "Default"
    if default.is_dir():
        return True
    try:
        return any(path.iterdir())
    except OSError:
        return False


def _json_has_session(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def session_present(entry: SupplierSessionEntry) -> bool:
    if entry.kind == SessionKind.NONE:
        return True
    if entry.kind == SessionKind.CDP:
        if entry.port is None:
            return False
        if entry.slug == "temu":
            from temu.browser_utils import is_cdp_available

            return is_cdp_available(f"http://127.0.0.1:{entry.port}")
        if entry.slug == "junkmail":
            from junkmail.browser_utils import is_cdp_available

            return is_cdp_available(f"http://127.0.0.1:{entry.port}")
        return False
    if entry.session_path is None:
        return False
    if entry.kind == SessionKind.CHROME_PROFILE:
        return _profile_has_session(entry.session_path)
    if entry.kind == SessionKind.JSON:
        return _json_has_session(entry.session_path)
    return False


def _ast_string(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level NAME = \"...\" assignments (e.g. BASE_URL)."""
    consts: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        val = _ast_string(node.value)
        if val:
            consts[target.id] = val
    return consts


def _resolve_ast_string(node: ast.AST | None, consts: dict[str, str]) -> str | None:
    val = _ast_string(node)
    if val:
        return val
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    return None


def _parse_generic_scraper_config_from_file(slug: str) -> dict[str, str]:
    """Extract login_url/base_url from GenericScraperConfig(...) in scrape_*.py."""
    path = PRODUCTS_ROOT / slug / f"scrape_{slug}.py"
    if not path.is_file():
        return {}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {}
    consts = _module_string_constants(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_generic = (
            isinstance(func, ast.Name)
            and func.id == "GenericScraperConfig"
        ) or (
            isinstance(func, ast.Attribute)
            and func.attr == "GenericScraperConfig"
        )
        if not is_generic:
            continue
        out: dict[str, str] = {}
        for kw in node.keywords:
            if kw.arg in ("login_url", "base_url"):
                val = _resolve_ast_string(kw.value, consts)
                if val:
                    out[kw.arg] = val
        if out:
            return out
    return {}


def _first_goto_url_in_scrape_file(slug: str) -> str | None:
    """Fallback for custom scrapers (e.g. gumtree) that use page.goto in scrape_*.py."""
    path = PRODUCTS_ROOT / slug / f"scrape_{slug}.py"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for match in re.finditer(r'page\.goto\(\s*["\'](https?://[^"\']+)["\']', text):
        return match.group(1)
    return None


def _load_scrape_urls(slug: str) -> tuple[str | None, Path | None]:
    """Return (login_url, session_file) from scrape module when available."""
    default_session = PRODUCTS_ROOT / slug / f"{slug}_session.json"
    parsed = _parse_generic_scraper_config_from_file(slug)

    mod = None
    try:
        mod = importlib.import_module(f"{slug}.scrape_{slug}")
    except ImportError:
        pass

    login = (
        (getattr(mod, "LOGIN_URL", None) if mod is not None else None)
        or (getattr(mod, "BASE_URL", None) if mod is not None else None)
        or parsed.get("login_url")
        or parsed.get("base_url")
        or _first_goto_url_in_scrape_file(slug)
        or CHROME_PROFILE_LOGIN_FALLBACK.get(slug)
    )
    session_file = getattr(mod, "SESSION_FILE", None) if mod is not None else None
    if session_file is None:
        session_file = default_session
    return (str(login) if login else None, Path(session_file))


def _session_kind_for_slug(slug: str) -> SessionKind:
    if slug == "manual":
        return SessionKind.NONE
    if slug in CDP_SUPPLIERS:
        return SessionKind.CDP
    if slug in CHROME_PROFILE_SLUGS:
        return SessionKind.CHROME_PROFILE
    return SessionKind.JSON


def build_registry() -> list[SupplierSessionEntry]:
    from shared.suppliers import SUPPLIERS

    entries: list[SupplierSessionEntry] = []
    for slug in sorted(SUPPLIERS.keys()):
        info = SUPPLIERS[slug]
        kind = _session_kind_for_slug(slug)
        if kind == SessionKind.NONE:
            entries.append(
                SupplierSessionEntry(
                    slug=slug,
                    display_name=info.display_name,
                    kind=kind,
                    session_path=None,
                    login_url=None,
                    interactive=False,
                )
            )
            continue

        login_url, session_file = _load_scrape_urls(slug)
        port = CDP_SUPPLIERS.get(slug)
        session_path: Path | None
        if kind == SessionKind.CDP:
            session_path = None
        elif kind == SessionKind.CHROME_PROFILE:
            session_path = PRODUCTS_ROOT / slug / "chrome_profile"
        else:
            session_path = session_file

        entries.append(
            SupplierSessionEntry(
                slug=slug,
                display_name=info.display_name,
                kind=kind,
                session_path=session_path,
                login_url=login_url,
                port=port,
                interactive=True,
            )
        )
    return entries


def registry_by_slug() -> dict[str, SupplierSessionEntry]:
    return {e.slug: e for e in build_registry()}


def session_backed_entries(
    *,
    include_none: bool = False,
    kinds: frozenset[SessionKind] | None = None,
) -> list[SupplierSessionEntry]:
    out: list[SupplierSessionEntry] = []
    for entry in build_registry():
        if entry.kind == SessionKind.NONE and not include_none:
            continue
        if kinds is not None and entry.kind not in kinds:
            continue
        if entry.kind == SessionKind.NONE:
            continue
        out.append(entry)
    return out


def cdp_entries() -> list[SupplierSessionEntry]:
    return session_backed_entries(kinds=frozenset({SessionKind.CDP}))


def all_interactive_entries() -> list[SupplierSessionEntry]:
    return [e for e in build_registry() if e.interactive and e.kind != SessionKind.NONE]
