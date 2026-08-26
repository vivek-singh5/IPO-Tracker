# Live IPO Tracker

Static dashboard for Indian mainboard IPOs: GMP, category-wise subscription,
realised listing gain, registrar allotment links and Narada links.

- `scrape.py` runs headless Chrome and writes `docs/ipos.json`
- `docs/index.html` fetches that JSON and renders it — no server needed
- `.github/workflows/scrape.yml` runs the scraper on a cron and commits the result

Served by GitHub Pages from the `docs/` folder on `main`.

## Local run

    pip install -r requirements.txt
    python scrape.py
    cd docs && python -m http.server 8000    # then open http://localhost:8000

Opening `docs/index.html` directly as a `file://` URL will not work — the
`fetch` of `ipos.json` is blocked by CORS. Use the local server above.

## Symbols

`symbols.json` accumulates NSE ticker lookups. Issues that have closed but not
yet listed appear in no public NSE list; add those to `SYMBOL_FIXES` in
`scrape.py` using the key the scraper prints to the log.
