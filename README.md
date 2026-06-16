# Products Scraper

Unified product scraper for Temu, Gumtree, and AliExpress, plus a scenario-based Gumtree crawler. One codebase with configurable company targets. Upload to one or multiple companies.

**Full guide:** [docs/PRODUCT_SCRAPERS.md](../docs/PRODUCT_SCRAPERS.md)

---

## Quick Start

```bash
cd products
python3 -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

Dependencies (including `requests` for manual product saves) live in **`.venv`**. Activate it before any Python command:

`source .venv/bin/activate` (Windows: `.venv\Scripts\activate`)

---

## Supplier Selection

**Select the supplier before starting a scrape.** The web UI and CLI support all registered suppliers, including Temu, Gumtree, AliExpress, and Nativechild.

### Web UI

1. From `products/`, activate `.venv`, then run `python app.py` → http://127.0.0.1:5001
2. Click **Scrape**
3. Choose supplier from the dropdown (for example Temu, Gumtree, AliExpress, or Nativechild)
4. Click **Start scrape**

- **Temu / Gumtree:** Interactive browse-and-save — a browser opens; browse the site and click the floating "Save" button to add products.
- **AliExpress:** URL-based — add product URLs to `aliexpress/urls.txt`, then start scrape. The browser will iterate through the list.

### Unified CLI

```bash
# From the parent of products/ (e.g. new-crm/)
python -m products scrape temu
python -m products scrape gumtree
python -m products scrape aliexpress

# With upload
python -m products scrape temu --upload
python -m products scrape gumtree --upload --upload-to all

# List available suppliers
python -m products list-suppliers

# Other options (passed through to the scraper)
python -m products scrape temu --list-categories
python -m products scrape temu --save-session
python -m products scrape temu --headless --debug
```

Legacy per-supplier scripts still work:

```bash
python temu/scrape_temu.py --upload
python gumtree/scrape_gumtree.py --upload
python aliexpress/scrape_aliexpress.py --upload
```

---

## Configuration

### Company target(s)

- **Single company:** `COMPANY_SLUG=past-and-present` in `.env`
- **Multiple companies:** `COMPANY_SLUGS=past-and-present,javamellow,plant-sanctuary` in `.env`
- **Run-time override:** `--upload-to past-and-present` or `--upload-to all`

### Category IDs

Set `CATEGORY_IDS` for each company (run `--list-categories` to get UUIDs):

```
CATEGORY_IDS=past-and-present:49742736-01e4-43a6-a730-20218aab6a24,javamellow:xxx,plant-sanctuary:yyy
```

---

## Web UI

```bash
python app.py              # Dashboard + Scrape + Edit at http://127.0.0.1:5001
python edit_products.py    # Edit only at http://127.0.0.1:5001
```

- **Dashboard:** Scrape | Edit | Upload
- **Scrape:** Select supplier, then start scrape
- **Edit:** Edit name, price, cost, images (tabs for Temu, Gumtree, AliExpress)

---

## Gumtree Crawler

The Gumtree crawler is separate from the classic `gumtree/scrape_gumtree.py` product scraper.

- **UI route:** `/gumtree-crawler`
- **Purpose:** discover Gumtree listings, score them against buying scenarios, and only surface strict matches in the scenario tabs
- **Storage:** `gumtree_crawler/gumtree_crawler.db`

### What It Stores

The crawler SQLite database stores:

- crawl jobs
- raw listings
- price history
- scenario configs
- per-listing scenario matches
- ignore rules
- crawler filters such as location preferences

### Default Scenarios

On first run the crawler seeds default scenarios from `gumtree_crawler/config.py`, then persists them in SQLite:

- `motor-bikes`
- `ai-hardware`
- `personal-transport`
- `cars`
- `laptops`
- `cell-phones`

Deleting `gumtree_crawler/gumtree_crawler.db` recreates the schema and reseeds the default scenarios and default location preferences the next time the crawler initializes.

### Scenario Tabs

The crawler page has:

- **`All`**: broad admin/debug view of stored non-ignored listings
- **scenario tabs**: only listings whose `scenario_matches.visible = 1` for that scenario

Each scenario can define:

- one or more Gumtree search URLs
- price range
- required and excluded keywords
- urgency keywords
- year requirements
- extracted attribute requirements such as GPU or RAM
- seller allow/deny rules
- sort weights for match, location, price, and urgency scoring

### Location Preferences

Location ranking is editable in the Gumtree crawler UI and stored separately from the classic scraper flow.

- preferences are saved as ordered province/city/suburb lists
- the crawler converts Gumtree location text into province/city/suburb parts
- earlier entries in the saved lists rank higher
- location affects scoring and display, not hard visibility

### Images

Images are intentionally **not downloaded during crawl**.

- crawl runs store listing data first: price, location, posted date when available, description, seller, condition, extracted attributes, and signals
- images are fetched only on demand from the Gumtree crawler UI/API
- fetched images are saved under the Gumtree source directory and can then be previewed or exported to products

### Runtime Config vs Code Defaults

Code defaults live in:

- `gumtree_crawler/config.py`

Runtime state lives in:

- `gumtree_crawler/gumtree_crawler.db`

Important: after first seed, the app reads the crawler's saved scenario config from SQLite. Updating code defaults does not automatically overwrite existing scenario rows in the database.

### Core Files

- `gumtree_crawler/config.py` - default scenarios and default location preferences
- `gumtree_crawler/crawler.py` - crawl orchestration and scenario evaluation
- `gumtree_crawler/db.py` - schema, scenario config persistence, listing queries
- `gumtree_crawler/scoring.py` - strict scenario matching and location scoring
- `gumtree_crawler/parsers.py` - listing/detail extraction and optional image URL extraction
- `app.py` - `/gumtree-crawler` page and `/api/gumtree-crawler/*` routes

---

## Junk Mail Crawler

The Junk Mail crawler is separate from the classic `junkmail/scrape_junkmail.py` product scraper.

- **UI route:** `/junkmail-crawler`
- **Purpose:** discover Junk Mail listings, score them against buying scenarios (same rule model as Gumtree), and surface strict matches in scenario tabs
- **Storage:** `junkmail_crawler/junkmail_crawler.db`

### Cloudflare and Chrome (required)

Junk Mail is protected by Cloudflare. Headless Playwright is **not** used for crawls — the crawler connects to **real Chrome** via CDP (port 9222) and reuses a session that already passed verification.

**First-time setup** (from `products/`):

```bash
python junkmail/setup_cloudflare.py
```

1. A Chrome window opens using `junkmail/chrome_profile/`
2. Wait until junkmail.co.za shows **listings** (not “Performing security verification”)
3. Press Enter in the terminal — cookies are saved to `junkmail/junkmail_session.json`
4. Leave Chrome open for **Run now**, or close it — the crawler can auto-start Chrome on the next run

If a crawl fails with a Cloudflare error, re-run setup or complete verification in the Junk Mail Chrome window.

### Differences from Gumtree crawler

| Topic | Junk Mail | Gumtree |
|-------|-----------|---------|
| Browser | Real Chrome CDP (`junkmail/cdp_fetch.py`) | Headless Playwright |
| Price filter | Applied in Python after parse (no URL `?pr=`) | In search URL |
| Pagination | `/page2`, `/page3`, … | `p2`, `p3` in URL |
| Ad ID | 32-char hex UUID in path | 15+ digit numeric |
| Default storage cap | 50 listings/search, 5 pages/search | No per-search cap |
| Store-only matches | Off by default (stores all like Gumtree; scenario tabs filter visibility) | Stores all non-ignored cards |

Crawl limits are editable in the Junk Mail crawler UI (**Filters**) or via `/api/junkmail-crawler/filters`:

- `max_pages_per_search` (default 5)
- `max_stored_per_search` (default 50)
- `store_only_scenario_matches` (default false — store all listings; scenario tabs show strict matches only)

Search URLs include Junk Mail price segments (`/pr{min}-{max}/`) built by `build_junkmail_search_url()` in `junkmail_crawler/parsers.py`. Example gaming PC search:

`https://www.junkmail.co.za/pr10000-20000/computers-and-gaming/q-gaming%20pc/so-latest`

Listings without a parsed card price are skipped when a price range is configured. On app startup, existing scenario configs in SQLite get updated search URLs from `config.py` (matched by search name).

### Default scenarios

On first run the crawler seeds from `junkmail_crawler/config.py` (mirrors Gumtree scenarios with Junk Mail search URLs), then persists in SQLite:

- `motor-bikes`, `ai-hardware`, `personal-transport`, `cars`, `laptops`, `cell-phones`, `laser-cutters`, `t-shirt-printing`, …

Deleting `junkmail_crawler/junkmail_crawler.db` recreates the schema and reseeds defaults on next init.

### Images

Images are **not** downloaded during crawl. Use **Fetch images** in the UI (or `POST /api/junkmail-crawler/listings/{id}/fetch-images`). That endpoint uses the same Chrome CDP session as the crawler and saves files under `junkmail/scraped/images/`.

### Parser validation

Offline fixture tests:

```bash
cd products
python -m junkmail_crawler.debug_parse
```

Live probe (requires Cloudflare clearance):

```bash
python -m junkmail_crawler.debug_parse --live "https://www.junkmail.co.za/q-laser%20cutter/so-latest"
```

If live crawls return zero cards after Cloudflare passes, save search/detail HTML and compare against `junkmail_crawler/parsers.py` selectors.

### Core files

- `junkmail_crawler/config.py` — default scenarios and location preferences
- `junkmail_crawler/crawler.py` — crawl orchestration (CDP)
- `junkmail_crawler/crawler_limits.py` — price range and storage caps
- `junkmail_crawler/db.py` — SQLite schema and queries
- `junkmail_crawler/parsers.py` — search/detail parsing
- `junkmail/cdp_fetch.py` — Chrome CDP fetch
- `junkmail/setup_cloudflare.py` — one-time Cloudflare setup
- `app.py` — `/junkmail-crawler` page and `/api/junkmail-crawler/*` routes

---

## Adding a New Supplier

1. Create `products/newsupplier/` with:
   - `scrape_newsupplier.py` — implement `scrape_url()`, `build_scraped_index()`, `_load_products()`
   - `urls.txt` — placeholder for product URLs
   - Optionally `run_scrape_session()` for interactive browse-and-save

2. Register in `products/shared/suppliers.py`:

```python
SUPPLIERS["newsupplier"] = SupplierInfo(
    slug="newsupplier",
    display_name="New Supplier",
    module_name="newsupplier.scrape_newsupplier",
    output_dir=_path("newsupplier/scraped"),
    urls_file=_path("newsupplier/urls.txt"),
    supports_interactive=False,  # True if run_scrape_session exists
)
```

3. `python -m products list-suppliers` — the CLI scrape command uses slugs from `get_suppliers()` in `shared/suppliers.py`; you do not need to hardcode new slugs in `cli.py`.

4. Edit UI tabs and SOURCES are sourced from the registry automatically.

---

## South Africa retail suppliers (browse-and-save)

These use the same interactive session model as Loot (floating **Save product** button, `Ctrl+Shift+S`). PDP fields are extracted with shared JSON-LD / Open Graph / DOM heuristics in `shared/dom_product_extract.py`.

| Slug | Folder | Session JSON |
|------|--------|----------------|
| `northernbolt` | `northernbolt/` | `northernbolt/northernbolt_session.json` |
| `builders` | `builders/` | `builders/builders_session.json` |
| `ahm` | `ahm/` | `ahm/ahm_session.json` |
| `dailydiscounts` | `dailydiscounts/` | `dailydiscounts/dailydiscounts_session.json` |
| `soundselect` | `soundselect/` | `soundselect/soundselect_session.json` |
| `tsawelding` | `tsawelding/` | `tsawelding/tsawelding_session.json` |

They are included in tiered markup (`shared/config.py` → `SUPPLIERS_USING_TIERED_MARKUP`). Configure per-supplier tiers in the scraper UI or `scraper_config.json` before scraping, same as other tiered suppliers.

### Debugging extraction

1. Set **`SCRAPER_DEBUG=1`** in the environment (or use the scrape UI debug option when available) so logs include extract probes.
2. On failed save or empty extract, artifacts are written under **`{supplier}/scraped/debug_capture/`**: `.html`, `.png` (when possible), `.meta.json`, and optional `*_fields.json` with normalized title/price/gallery counts.
3. Tighten PDP URL detection in `shared/retail_product_pipeline.default_is_product_url` or add supplier-specific checks in that supplier’s `scrape_*.py` if saves are skipped on valid product pages.

---

## Verify all (stock & price checker)

On **Edit Products** (`/edit`), use **Verify all** to check every local product (company-scoped `products.json`) against its supplier URL.

### Behaviour

- **Price changed** — auto-updates local `price`, `cost`, and `{supplier}_price` in `products.json`
- **Sold out / unavailable** — sets `in_stock=false`, `stock_quantity=0`, prepends `** SOLD OUT **` to the product name, and auto-selects the row so you can **Deactivate** or **Sync** in batch
- **No change** — logged as OK
- **Unsupported supplier** — skipped with a clear log line (no checker yet)

Progress appears in a modal while the job runs. Changes are saved to disk after each product.

**Junk Mail:** Verify uses saved Cloudflare cookies in `junkmail/junkmail_session.json`. Run `python junkmail/setup_cloudflare.py` first (same session as the Junk Mail crawler). Re-run setup if verify returns “Could not fetch price”.

**Temu:** Verify uses **real Chrome** on port **9223** (not Playwright automation — the security slider fails there). First run `python temu/setup_verify.py`, complete login/slider in that Chrome window, then **Verify all**. Leave that Chrome open during the batch. Verify waits up to **180s** for Chrome to open on the first Temu run, then **120s per Temu product** (HTTP suppliers stay at 20s). If you see “Timed out after 20s”, restart the products app so the new limits apply.

**Browser session setup (from `products/`):**

| Command | Covers |
|---------|--------|
| `python setup_browser_sessions.py` | **2** CDP suppliers: temu (9223), junkmail (9222) |
| `python setup_browser_sessions.py --all-suppliers` | **39** session-backed suppliers (CDP + chrome profiles + JSON) |
| `python setup_browser_sessions.py --all-suppliers --status` | Check which sessions exist on disk (no Chrome) |
| `python setup_browser_sessions.py --list` | Full registry: kind per supplier |
| `python setup_browser_sessions.py --only makro,temu` | Subset |

If Temu **Verify all** still shows “Timed out after 20s”, **restart the products app** so [`shared/verify_timeouts.py`](shared/verify_timeouts.py) is loaded (120s per product, 180s pre-warm).

### Scope

- **View all** mode — checks every supplier for the selected company
- **Single supplier tab** — checks only that supplier’s products

### Supported suppliers (have `fetch_current_pricing`)

`temu`, `gumtree`, `junkmail`, `aliexpress`, `makro`, `constructionhyper`, `game`, `loot`, `perfectdealz`, `ubuy`, `myrunway`, `onedayonly`, `ahm`, `tsawelding`, `outdoorandvelocity`, `hekpoorthoneyfarms`, `seedsandall`, `brendas`, `elanas`, `shein`

Other suppliers are logged as unsupported until a checker is added.

---

## Sources

| Source     | URLs file              | Scraped output                  |
|------------|------------------------|---------------------------------|
| Temu       | `temu/urls.txt`        | `temu/scraped/products.json`   |
| Gumtree    | `gumtree/urls.txt`     | `gumtree/scraped/products.json` |
| AliExpress | `aliexpress/urls.txt`  | `aliexpress/scraped/products.json` |
| Northern Bolt | `northernbolt/urls.txt` | `northernbolt/scraped/products.json` |
| Builders | `builders/urls.txt` | `builders/scraped/products.json` |
| AHM Online | `ahm/urls.txt` | `ahm/scraped/products.json` |
| Daily Discounts | `dailydiscounts/urls.txt` | `dailydiscounts/scraped/products.json` |
| Sound Select | `soundselect/urls.txt` | `soundselect/scraped/products.json` |
| TSA Welding | `tsawelding/urls.txt` | `tsawelding/scraped/products.json` |
| SHEIN | `shein/urls.txt` | `shein/scraped/products.json` |
| Nativechild | `nativechild/urls.txt` | `nativechild/scraped/products.json` |
| Black African | `blackafrican/urls.txt` | `blackafrican/scraped/products.json` |
| Cosmetic Connection | `cosmeticconnection/urls.txt` | `cosmeticconnection/scraped/products.json` |
