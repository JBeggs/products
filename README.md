# Products Scraper

Unified product scraper for Temu, Gumtree, and AliExpress. One codebase with configurable company targets. Upload to one or multiple companies.

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
