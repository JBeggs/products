# Supplier Session Handling

This document describes how each supplier stores and restores login sessions for scraping. Use this when implementing "place order" flows that need to reuse saved sessions.

## Overview

Suppliers use one of two session mechanisms:

1. **JSON storage state** – Playwright `storage_state` saved to a `.json` file. Restored on each scrape run.
2. **Persistent Chrome profile** – Real Chrome user data directory. Session persists across runs; fewer captchas.

## Per-Supplier Behavior

| Supplier | Mechanism | Session Location | Notes |
|----------|-----------|------------------|-------|
| **Takealot** | Persistent profile | `takealot/chrome_profile/` | `use_persistent_context=True`. Log in once; session persists. |
| **Game** | Persistent profile | `game/chrome_profile/` | Same as Takealot. |
| **Construction Hyper** | Persistent profile | `constructionhyper/chrome_profile/` | Same as Takealot. |
| **Makro** | JSON session | `makro/makro_session.json` | `storage_state` saved on "Save session". |
| **Matrix Warehouse** | JSON session | `matrixwarehouse/matrixwarehouse_session.json` | Same as Makro. |
| **Loot** | JSON session | `loot/loot_session.json` | Same as Makro. |
| **Perfect Dealz** | JSON session | `perfectdealz/perfectdealz_session.json` | Same as Makro. |
| **AliExpress** | JSON session | `aliexpress/aliexpress_session.json` | Same as Makro. |
| **Ubuy** | JSON session | `ubuy/ubuy_session.json` | Same as Makro. |
| **MyRunway** | JSON session | `myrunway/myrunway_session.json` | Same as Makro. |
| **OneDayOnly** | JSON session | `onedayonly/onedayonly_session.json` | Same as Makro. |
| **Temu** | Custom (Chrome profile) | `temu/chrome_profile/` | Uses `launch_persistent_context` directly, not `GenericScraperConfig`. |
| **Gumtree** | Custom (Chrome profile) | `gumtree/chrome_profile/` | Uses `launch_persistent_context` for Gmail/Google OAuth. |

## JSON Session (storage_state)

- **Save**: Click "Save session" during scrape, or `Ctrl+Shift+S`. Writes cookies/localStorage to `{supplier}/{supplier}_session.json`.
- **Restore**: On next scrape, Playwright loads `storage_state` from the file before navigating.
- **Limitation**: Some sites (e.g. OAuth popups, sessionStorage) may not persist in `storage_state`.

## Persistent Chrome Profile

- **Location**: `{supplier}/chrome_profile/` – real Chrome user data directory.
- **Behavior**: Launches Chromium with `launch_persistent_context(user_data_dir)`. Cookies, localStorage, sessionStorage, and login state persist.
- **Advantage**: Fewer captchas; OAuth (Google, etc.) works because sessionStorage persists.
- **Save**: No explicit "Save session" – the profile is written as you browse. Closing the browser preserves state.

## OAuth and Popups

Some suppliers (Takealot, Gumtree) use OAuth (e.g. Google). The generic scraper supports `allow_popup_for_hosts` so OAuth popups are not intercepted. Example: `("accounts.google.com", "firebaseapp.com")`.

## Place-Order Flows (Future)

When implementing automated order placement:

1. **Persistent profile suppliers**: Reuse the same `chrome_profile` directory. Ensure the browser is launched with that profile and the user is already logged in.
2. **JSON session suppliers**: Load `storage_state` from the session file before navigating to the cart/checkout. Session may need to be refreshed if expired.
3. **Per-supplier checkout**: Each supplier has different cart/checkout flows. Document supplier-specific selectors and steps separately.
