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

---

## Supplier Selection

**Select the supplier before starting a scrape.** The web UI and CLI both support Temu, Gumtree, and AliExpress.

### Web UI

1. Run `python app.py` → http://127.0.0.1:5001
2. Click **Scrape**
3. Choose supplier from the dropdown (Temu, Gumtree, or AliExpress)
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

3. Add to `cli.py` choices: `choices=["temu", "gumtree", "aliexpress", "newsupplier"]`

4. Edit UI tabs and SOURCES are sourced from the registry automatically.

---

## Sources

| Source     | URLs file              | Scraped output                  |
|------------|------------------------|---------------------------------|
| Temu       | `temu/urls.txt`        | `temu/scraped/products.json`   |
| Gumtree    | `gumtree/urls.txt`     | `gumtree/scraped/products.json` |
| AliExpress | `aliexpress/urls.txt`  | `aliexpress/scraped/products.json` |
