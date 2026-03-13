"""
Generic session-based scraper for suppliers that require login.
Used by makro, constructionhyper, game, matrixwarehouse, takealot, loot, perfectdealz.
"""
import logging
import re

LOG = logging.getLogger("products.scraper")
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from shared.playwright_utils import CHROMIUM_PERFORMANCE_ARGS, PAGE_LOAD_TIMEOUT

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Launch args: performance + anti-detection
LAUNCH_ARGS = CHROMIUM_PERFORMANCE_ARGS + [
    "--disable-blink-features=AutomationControlled",
]


@dataclass
class GenericScraperConfig:
    base_url: str
    login_url: str
    session_file: Path
    hostname_pattern: str  # e.g. "makro.co.za" for location.hostname.includes check
    supplier_slug: str
    skip_script_on_paths: tuple[str, ...] = ()  # e.g. ("login", "challenge") - skip injection on these paths (Shopify captcha/CSP)
    use_persistent_context: bool = False  # Use real Chrome profile - session persists, fewer captchas
    persistent_user_data_dir: Path | None = None  # Required if use_persistent_context
    allow_popup_for_hosts: tuple[str, ...] = ()  # e.g. ("accounts.google", "firebaseapp") - don't intercept popups for OAuth
    button_position: str = "right"  # "left" or "right" - position of the Save button


def _path_skip_check(skip_paths: tuple[str, ...]) -> str:
    """JS snippet: return true if we should skip script on current path."""
    if not skip_paths:
        return "false"
    parts = ", ".join(repr(p) for p in skip_paths)
    return f"(function(){{ var p=(location.pathname||'').toLowerCase(); return [{parts}].some(function(x){{ return p.indexOf(x)>=0; }}); }})()"


def _allow_popup_check(allow_hosts: tuple[str, ...]) -> str:
    """JS snippet: return true if URL should allow popup (OAuth etc)."""
    if not allow_hosts:
        return "false"
    parts = ", ".join(repr(h) for h in allow_hosts)
    return f"(function(u){{ if (!u) return false; var l=u.toLowerCase(); return [{parts}].some(function(h){{ return l.indexOf(h.toLowerCase())>=0; }}); }})"


def _prevent_new_tab_script(
    hostname_pattern: str,
    skip_paths: tuple[str, ...] = (),
    allow_popup_hosts: tuple[str, ...] = (),
) -> str:
    """Generate script that redirects target=_blank links to same tab on the given hostname.
    allow_popup_hosts: URLs matching these hosts (e.g. accounts.google, firebaseapp) keep popup behavior for OAuth."""
    skip_cond = _path_skip_check(skip_paths) if skip_paths else "false"
    allow_popup_fn = _allow_popup_check(allow_popup_hosts) if allow_popup_hosts else "function(){ return false; }"
    return f"""
(function() {{
  if (!location.hostname.includes('{hostname_pattern}')) return;
  if ({skip_cond}) return;
  var _nativeOpen = window.open;
  window.open = function(url, target, features) {{
    var u = (url && typeof url === 'string') ? url.trim() : '';
    if ({allow_popup_fn}(u)) return _nativeOpen ? _nativeOpen.apply(this, arguments) : null;
    if (u && u !== 'about:blank' && (u.startsWith('http') || u.startsWith('/'))) {{
      window.location.href = u;
    }}
    return null;
  }};
  document.addEventListener('click', function(e) {{
    var a = e.target.closest('a');
    if (!a || !a.href) return;
    var href = (a.getAttribute('href') || a.href || '').trim();
    if (!href || href === '#' || href.startsWith('javascript:')) return;
    if (a.target === '_blank' || a.getAttribute('target') === '_blank' || e.ctrlKey || e.metaKey) {{
      if ({allow_popup_fn}(href)) return;
      e.preventDefault();
      e.stopPropagation();
      if (a.href && a.href !== 'about:blank') window.location.href = a.href;
      return false;
    }}
  }}, true);
  function stripBlankTarget() {{
    try {{ document.querySelectorAll('a[target="_blank"]').forEach(function(el) {{
      if (el.href && {allow_popup_fn}(el.href)) return;
      el.removeAttribute('target');
    }}); }} catch(e) {{}}
  }}
  if (document.body) {{ stripBlankTarget(); var obs = new MutationObserver(stripBlankTarget); obs.observe(document.body, {{ childList: true, subtree: true }}); }}
  else document.addEventListener('DOMContentLoaded', function() {{ stripBlankTarget(); var obs = new MutationObserver(stripBlankTarget); obs.observe(document.body, {{ childList: true, subtree: true }}); }});
}})();
"""


def _floating_button_script(
    hostname_pattern: str,
    trigger_var: str,
    btn_id: str,
    skip_paths: tuple[str, ...] = (),
    button_position: str = "right",
) -> str:
    """Generate floating Save button script for the given hostname."""
    skip_cond = _path_skip_check(skip_paths) if skip_paths else "false"
    pos_css = "left:20px" if button_position == "left" else "right:20px"
    return f"""
if (!location.hostname.includes('{hostname_pattern}')) void 0;
else if ({skip_cond}) void 0;
else {{
  function addBtn() {{
    if (!document.body || document.getElementById('{btn_id}')) return;
    const btn = document.createElement('button');
    btn.id = '{btn_id}';
    btn.textContent = 'Save product';
    btn.style.cssText = 'position:fixed;bottom:20px;{pos_css};z-index:99999;padding:8px 16px;background:#2a7;color:white;border:none;border-radius:6px;cursor:pointer;font-size:14px;box-shadow:0 2px 8px rgba(0,0,0,0.3)';
    btn.onclick = function() {{
      try {{
        window.{trigger_var}TargetUrl = window.location.href || '';
        window.{trigger_var}ClickedAt = Date.now();
        window.{trigger_var} = true;
        btn.textContent = 'Saving...';
        setTimeout(function(){{ btn.textContent = 'Save product'; }}, 1500);
      }} catch (e) {{ btn.textContent = 'Error'; setTimeout(function(){{ btn.textContent = 'Save product'; }}, 2000); }}
    }};
    document.body.appendChild(btn);
  }}
  if (document.body) addBtn();
  else document.addEventListener('DOMContentLoaded', addBtn);
}}
"""


def _url_to_slug(url: str) -> str:
    """Convert URL to a safe filename slug."""
    parsed = urlparse(url)
    path = (parsed.path or "").strip("/") or "page"
    slug = re.sub(r"[^\w\-]", "_", path)[:80]
    return slug or "page"


def run_generic_scrape_session(
    config: GenericScraperConfig,
    output_dir: Path,
    stop_flag: threading.Event,
    save_session_flag: threading.Event,
    scrape_callback,
    build_index_callback=None,
    scrape_options: dict | None = None,
) -> None:
    """
    Run Playwright scrape session for a generic session-based supplier.
    - If no session: goto login_url, print login instructions
    - If session exists: goto base_url
    - Injects floating Save button on supplier domain
    - On Save: calls scrape_callback(page, output_dir). If False/raises: saves HTML to debug_html/ and prints alert
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    trigger_var = f"__{config.supplier_slug}ScraperSaveTrigger"
    btn_id = f"{config.supplier_slug}-scraper-save-btn"
    check_script = f"""
    () => {{
        if (window.{trigger_var}) {{
            window.{trigger_var} = false;
            return {{
                triggered: true,
                targetUrl: window.{trigger_var}TargetUrl || (window.location && window.location.href) || '',
                clickedAt: window.{trigger_var}ClickedAt || null,
            }};
        }}
        return {{
            triggered: false,
            targetUrl: '',
            clickedAt: null,
        }};
    }}
    """

    skip_paths = getattr(config, "skip_script_on_paths", ()) or ()
    allow_popup = getattr(config, "allow_popup_for_hosts", ()) or ()
    btn_pos = getattr(config, "button_position", "right") or "right"
    prevent_script = _prevent_new_tab_script(config.hostname_pattern, skip_paths, allow_popup)
    button_script = _floating_button_script(config.hostname_pattern, trigger_var, btn_id, skip_paths, btn_pos)

    use_persistent = getattr(config, "use_persistent_context", False) and getattr(config, "persistent_user_data_dir", None)
    opts = scrape_options or {}
    proxy = {"server": opts["proxy_server"]} if opts.get("proxy_server") else None

    LOG.info("Starting %s scraper (persistent=%s)", config.supplier_slug, use_persistent)

    with sync_playwright() as p:
        launch_opts = {
            "headless": False,
            "args": LAUNCH_ARGS,
            "user_agent": USER_AGENT,
            "viewport": None,
            "locale": "en-ZA",
        }
        if proxy:
            launch_opts["proxy"] = proxy
        if use_persistent:
            user_data = config.persistent_user_data_dir
            user_data.mkdir(parents=True, exist_ok=True)
            context = p.chromium.launch_persistent_context(str(user_data), **launch_opts)
            browser = None  # context is the browser context directly
        else:
            # launch() only accepts headless, args, etc. - not user_agent/viewport/locale (those go on new_context)
            browser = p.chromium.launch(
                headless=launch_opts.get("headless", False),
                args=launch_opts.get("args", LAUNCH_ARGS),
            )
            ctx_opts = {
                "user_agent": USER_AGENT,
                "viewport": None,
                "locale": "en-ZA",
            }
            if proxy:
                ctx_opts["proxy"] = proxy
            if config.session_file.exists():
                ctx_opts["storage_state"] = str(config.session_file)
            context = browser.new_context(**ctx_opts)

        try:
            LOG.debug("Launching browser for %s", config.supplier_slug)
            context.add_init_script(prevent_script)
            context.add_init_script(button_script)
            page = context.new_page()

            if use_persistent:
                page.goto(config.base_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                print("  Using persistent Chrome profile. Log in once; session persists in profile.")
            elif config.session_file.exists():
                page.goto(config.base_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            else:
                page.goto(config.login_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                print("  No session. Log in in the browser, then click 'Save session' in the web UI.")

            def close_blank_popup(new_page):
                try:
                    if new_page.url in ("about:blank", "") or "about:blank" in new_page.url:
                        new_page.close()
                except Exception:
                    pass

            context.on("page", close_blank_popup)
            try:
                page.evaluate("(function(){ " + prevent_script + button_script + " })()")
            except Exception:
                pass

            while not stop_flag.is_set():
                for pg in context.pages:
                    try:
                        state = pg.evaluate(check_script)
                        if state and state.get("triggered"):
                            target_url = (state.get("targetUrl") or "").strip()
                            # Give SPA transitions a moment to settle so we don't save the previous product.
                            try:
                                pg.wait_for_load_state("domcontentloaded", timeout=3000)
                            except Exception:
                                pass
                            try:
                                pg.wait_for_load_state("networkidle", timeout=3000)
                            except Exception:
                                pass
                            if target_url:
                                try:
                                    current_url = (pg.url or "").split("#")[0]
                                    if current_url != target_url.split("#")[0]:
                                        pg.wait_for_timeout(600)
                                except Exception:
                                    pass
                            try:
                                if scrape_callback and scrape_callback(pg, output_dir):
                                    print(f"  Saved: {pg.url[:70]}...")
                                else:
                                    _save_html_no_logic(pg, output_dir, config.supplier_slug)
                            except Exception as e:
                                _save_html_no_logic(pg, output_dir, config.supplier_slug, str(e))
                            break
                    except Exception:
                        pass
                if save_session_flag.is_set() and not use_persistent:
                    config.session_file.parent.mkdir(parents=True, exist_ok=True)
                    context.storage_state(path=str(config.session_file))
                    save_session_flag.clear()
                    print("  Session saved.")
                time.sleep(0.3)
        finally:
            if browser:
                browser.close()
            elif use_persistent:
                context.close()

    if build_index_callback:
        build_index_callback(output_dir)


def _save_html_no_logic(page, output_dir: Path, supplier_slug: str, error: str = "") -> None:
    """Save page HTML to debug_html/ and print alert when no extract logic exists."""
    from datetime import datetime

    debug_dir = output_dir / "debug_html"
    debug_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    url_slug = _url_to_slug(page.url)
    fname = f"{ts}_{url_slug}.html"
    path = debug_dir / fname
    try:
        html = page.content()
        path.write_text(html, encoding="utf-8")
    except Exception as e:
        path = None
        error = str(e)
    msg = f"  No extract logic for {supplier_slug}. HTML saved to {path} - please provide for building extractor."
    if error:
        msg += f" (Error: {error})"
    print(msg)
