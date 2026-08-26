"""
IPO scraper — writes docs/ipos.json for the static dashboard
============================================================
Runs headless Chrome over the live GMP report, the live subscription report,
the IPO Watch registrar links and the NSE symbol lists, then writes a single
JSON snapshot the static page fetches. Designed to run on a GitHub Actions
cron; there is no web server here.

Run locally:
    pip install -r requirements.txt
    python scrape.py
"""


import csv
import io
import json
import os
import re
import time
from datetime import date, timedelta

from bs4 import BeautifulSoup

URL_GMP = "https://www.investorgain.com/report/ipo-gmp-live/331/ipo/"
URL_SUB = "https://www.investorgain.com/report/ipo-subscription-live/333/ipo/"
URL_ALM = "https://ipowatch.in/ipo-allotment-status-how-to-check/"
URL_EQ  = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
URL_NSE_CUR = "https://www.nseindia.com/api/ipo-current-issue"
URL_NSE_UP  = "https://www.nseindia.com/api/all-upcoming-issues?category=ipo"
NARADA_TMPL = "https://trynarada.com/ipos/{symbol}/allotment/"
SYMBOL_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "symbols.json")
PAGE_WAIT = 20           # max seconds to wait for the table to render
MAINBOARD_ONLY = True    # set False to let SME issues back in

# Manual overrides for issues the automatic lookup can't resolve — the window
# between an issue closing and appearing in EQUITY_L.csv has no public source.
# Keys are key_of(clean_name(...)) values; run with -v to see unresolved names.
SYMBOL_FIXES = {
    # Issues that have closed but not yet listed exist in no public NSE list.
    # Paste the console's "add to SYMBOL_FIXES" line here as they come up.
    "tempsensinstruments": "TEMPSENS",
}

# ----------------------------------------------------------------------------
# output
# ----------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_JSON = os.path.join(HERE, "docs", "ipos.json")

# ----------------------------------------------------------------------------
# cell parsers
# ----------------------------------------------------------------------------
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_NUM = r"-?\d+(?:\.\d+)?"
_POS = r"\d+(?:\.\d+)?"
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec)"


def _f(value):
    """Best-effort float, tolerating '--', '', 'NA' and stray symbols."""
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if text in {"", "-", "--", "NA", "N/A"}:
        return None
    match = re.search(_NUM, text)
    return float(match.group()) if match else None


def parse_gmp(text):
    """Parses '₹ 19 (13.77%) 19 ↓ / 19 ↑' into (gmp, gain_pct, low, high)."""
    if not text:
        return None, None, None, None

    clean = text.replace(",", "")
    gmp = None
    money = re.search(r"₹\s*(" + _NUM + r"|--)", clean)
    if money and money.group(1) != "--":
        gmp = float(money.group(1))

    gain = None
    pct = re.search(r"\((" + _NUM + r")\s*%\)", clean)
    if pct:
        gain = float(pct.group(1))

    low = high = None
    pair = re.search(r"(" + _NUM + r")\s*[↓▼]?\s*/\s*(" + _NUM + r")\s*[↑▲]?", clean)
    if pair:
        low, high = float(pair.group(1)), float(pair.group(2))
        if low > high:
            low, high = high, low

    if gmp in (None, 0) and not gain and not (low or high):
        return None, None, None, None
    return gmp, gain, low, high


def parse_listing_result(text, issue_price=None):
    """
    Pulls the realised listing result out of the name cell.

    The cell reads like 'Gaja Alternative Asset Management IPO L@185.00 (15.62%)'
    once an issue lists. Returns (listing_price, listing_pct).

    The scraped percentage is trusted, but when the issue price is known the
    percentage is recomputed as a sanity check — the site renders losses in red
    and occasionally drops the minus sign, so a sign disagreement means the
    computed value wins.
    """
    if not text:
        return None, None

    clean = str(text).replace(",", "")
    hit = re.search(r"\bL\s*@\s*(" + _POS + r")", clean, re.I)
    if not hit:
        return None, None

    price = float(hit.group(1))
    if price <= 0:
        return None, None

    scraped = None
    pct = re.search(r"\((" + _NUM + r")\s*%\)", clean[hit.end():])
    if pct:
        scraped = float(pct.group(1))

    computed = None
    if issue_price:
        computed = round((price - issue_price) / issue_price * 100, 2)

    if scraped is None:
        return price, computed
    if computed is not None and (scraped >= 0) != (computed >= 0):
        return price, computed
    return price, scraped


def parse_date_cell(text):
    if not text:
        return None, None
    clean = re.sub(r"\s+", " ", str(text)).strip()

    day_gmp = None
    tagged = re.search(r"GMP\s*[:\-]?\s*(" + _NUM + r")", clean, re.I)
    if tagged:
        day_gmp = float(tagged.group(1))

    stamp = re.match(r"(\d{1,2}[-\s]" + _MON + r"(?:[-\s]\d{2,4})?)", clean, re.I)
    label = stamp.group(1).strip() if stamp else re.split(r"GMP", clean, flags=re.I)[0].strip()
    return (label or None), day_gmp


def to_iso(label):
    if not label:
        return None
    match = re.match(r"(\d{1,2})[-\s](" + _MON + r")(?:[-\s](\d{2,4}))?", label.strip(), re.I)
    if not match:
        return None
    day = int(match.group(1))
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None

    if match.group(3):
        year = int(match.group(3))
        year += 2000 if year < 100 else 0
    else:
        today = date.today()
        year = today.year
        try:
            guess = date(year, month, day)
        except ValueError:
            return None
        if guess < today - timedelta(days=180):
            year += 1
        elif guess > today + timedelta(days=270):
            year -= 1

    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_subscription(text):
    if not text:
        return None
    if not re.search(r"\d", str(text)):
        return None
    return _f(str(text).replace("x", " "))


def parse_price(text):
    if not text:
        return None, None
    nums = re.findall(_POS, str(text).replace(",", ""))
    nums = [float(n) for n in nums if float(n) > 0]
    if not nums:
        return None, None
    if len(nums) == 1:
        return None, nums[0]
    return min(nums[:2]), max(nums[:2])


def clean_name(text):
    name = re.sub(r"\s+", " ", str(text or "")).strip()
    name = re.sub(r"\b(NSE|BSE)\s*SME\b", " SME ", name, flags=re.I)
    board = "SME" if re.search(r"\bSME\b", name, re.I) else "Mainboard"
    name = re.sub(r"\bSME\b", " ", name, flags=re.I)
    name = re.sub(r"\bIPO\b.*$", "", name, flags=re.I)
    name = re.sub(r"[\u2b50\U0001f525\u26a0\ufe0f]+", " ", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" -–|,")
    return (name or "Unnamed issue"), board


def key_of(name):
    key = str(name or "").lower()
    key = re.sub(r"\b(ipo|limited|ltd|private|pvt|india|the|and|co)\b", " ", key)
    return re.sub(r"[^a-z0-9]", "", key)


def stage_for(open_iso, close_iso, listing_iso):
    today = date.today().isoformat()
    if listing_iso and today >= listing_iso:
        return "listed"
    if close_iso and today > close_iso:
        return "closed"
    if open_iso and today >= open_iso:
        return "open"
    return "upcoming"


# ----------------------------------------------------------------------------
# html extraction & parsing maps
# ----------------------------------------------------------------------------
COLUMN_PATTERNS = [
    ("name",    r"\b(ipo|company|name)\b"),
    ("gmp",     r"\bgmp|premium\b"),
    ("price",   r"\bprice|band\b"),
    ("size",    r"\b(ipo\s*)?size\b"),
    ("sub",     r"\b(sub|times|bid|total)\b"),
    ("open",    r"\bopen"),
    ("close",   r"\bclose|\bend"),
    ("boa",     r"\bboa|allot"),
    ("listing", r"\blist.*(date|dt|day)|\blisting\b"),
]

SUB_PATTERNS = [
    ("name",  r"\b(ipo|company|name)\b"),
    ("qib",   r"qib|institution"),
    ("shni",  r"s\s*-?\s*hni|small\s*hni|shni"),
    ("bhni",  r"b\s*-?\s*hni|big\s*hni|large\s*hni|bhni"),
    ("nii",   r"\bnii\b|non.?institution|\bhni\b"),
    ("rii",   r"\brii\b|retail"),
    ("emp",   r"employee|\bemp\b"),
    ("total", r"total|overall"),
]


def _map_by(header_cells, patterns):
    mapping = {}
    for idx, cell in enumerate(header_cells):
        label = re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).lower()
        if not label:
            continue
        for field, pattern in patterns:
            if field in mapping:
                continue
            if re.search(pattern, label):
                if field == "listing" and "est" in label:
                    continue
                mapping[field] = idx
                break
    return mapping


def map_columns(header_cells):
    return _map_by(header_cells, COLUMN_PATTERNS)


def cell_text(cols, mapping, field):
    idx = mapping.get(field)
    if idx is None or idx >= len(cols):
        return ""
    return cols[idx].get_text(" ", strip=True)


# ----------------------------------------------------------------------------
# web drivers & data fetching
# ----------------------------------------------------------------------------
def build_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)


def fetch_htmls():
    """Fetches the GMP page, Subscription page, and Allotment status page.

    Returns (gmp_html, sub_html, alm_html, symbol_map). The same driver is
    reused for the NSE symbol lookup so its cookies are already warm.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    driver = build_driver()
    try:
        pages = []
        for url in (URL_GMP, URL_SUB, URL_ALM):
            driver.get(url)
            try:
                WebDriverWait(driver, PAGE_WAIT).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "table"))
                )
            except Exception:
                pass
            time.sleep(1.2)
            pages.append(driver.page_source)
        symbols = load_symbol_map(driver)
        return pages[0], pages[1], pages[2], symbols
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# NSE symbol lookup
# ----------------------------------------------------------------------------
def _nse_session(driver):
    """A requests session carrying the cookies NSE hands the browser."""
    import requests

    ses = requests.Session()
    ses.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
        "Referer": "https://www.nseindia.com/",
        "Accept-Language": "en-US,en;q=0.9",
    })
    for cookie in driver.get_cookies():
        try:
            ses.cookies.set(cookie["name"], cookie["value"])
        except Exception:
            pass
    return ses


def fetch_listed_symbols(session):
    """Every NSE-listed symbol from EQUITY_L.csv, keyed by normalised name."""
    out = {}
    try:
        body = session.get(URL_EQ, timeout=25).text
        for row in csv.DictReader(io.StringIO(body)):
            name = (row.get("NAME OF COMPANY") or "").strip()
            symbol = (row.get("SYMBOL") or "").strip()
            if name and symbol:
                out[key_of(clean_name(name)[0])] = symbol.upper()
        print(f"  EQUITY_L.csv gave {len(out)} listed symbols")
    except Exception as exc:
        print(f"  EQUITY_L.csv failed: {exc}")
    return out


def fetch_pipeline_symbols(driver):
    """Symbols for open and upcoming issues, which aren't in EQUITY_L yet."""
    from selenium.webdriver.common.by import By

    out = {}
    for url in (URL_NSE_CUR, URL_NSE_UP):
        try:
            driver.get(url)
            time.sleep(1.2)
            payload = json.loads(driver.find_element(By.TAG_NAME, "pre").text)
            rows = payload.get("data") if isinstance(payload, dict) else payload
            for row in rows or []:
                name = row.get("companyName") or row.get("issuerName") or row.get("company")
                symbol = row.get("symbol")
                if name and symbol:
                    out[key_of(clean_name(str(name))[0])] = str(symbol).strip().upper()
        except Exception as exc:
            print(f"  {url.rsplit('/', 1)[-1]} failed: {exc}")
    return out


def load_symbol_map(driver):
    """
    Layers three sources over a growing on-disk cache.

    EQUITY_L covers everything already trading, the NSE IPO endpoints cover
    open and upcoming issues, and SYMBOL_FIXES gets the final word. The cache
    matters because a symbol resolved while an issue was open stays resolved
    after it lists, so coverage only ever grows.
    """
    merged = {}
    if os.path.exists(SYMBOL_CACHE):
        try:
            with open(SYMBOL_CACHE, encoding="utf-8") as fh:
                merged.update(json.load(fh))
            print(f"  symbol cache loaded ({len(merged)} entries)")
        except Exception as exc:
            print(f"  symbol cache unreadable: {exc}")

    try:
        driver.get("https://www.nseindia.com")
        time.sleep(2)
        merged.update(fetch_listed_symbols(_nse_session(driver)))
        merged.update(fetch_pipeline_symbols(driver))
    except Exception as exc:
        print(f"  symbol lookup failed, using cache only: {exc}")

    merged.update(SYMBOL_FIXES)

    try:
        with open(SYMBOL_CACHE, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=1, sort_keys=True)
    except Exception as exc:
        print(f"  could not write symbol cache: {exc}")

    return merged


def pick_table(soup, is_sub=False):
    tables = soup.find_all("table")
    if not tables:
        return None
    for table in tables:
        head = table.find("tr")
        if head:
            text = head.get_text(" ", strip=True).upper()
            if is_sub:
                if "QIB" in text or "NII" in text or "RII" in text:
                    return table
            else:
                if "GMP" in text and ("IPO" in text or "NAME" in text or "COMPANY" in text):
                    return table
    return max(tables, key=lambda t: len(t.find_all("tr")))


def parse_sub_data(html):
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    table = pick_table(soup, is_sub=True)
    if not table:
        return {}
    rows = table.find_all("tr")
    if len(rows) < 2:
        return {}

    mapping = _map_by(rows[0].find_all(["th", "td"]), SUB_PATTERNS)
    if "name" not in mapping:
        mapping["name"] = 0

    data = {}
    for row in rows[1:]:
        cols = row.find_all(["td", "th"])
        if len(cols) < 3:
            continue
        raw_name = cell_text(cols, mapping, "name")
        if not raw_name:
            continue
        name, _ = clean_name(raw_name)
        key = key_of(name)
        if not key:
            continue
        data[key] = {
            "qib":   parse_subscription(cell_text(cols, mapping, "qib")),
            "shni":  parse_subscription(cell_text(cols, mapping, "shni")),
            "bhni":  parse_subscription(cell_text(cols, mapping, "bhni")),
            "nii":   parse_subscription(cell_text(cols, mapping, "nii")),
            "rii":   parse_subscription(cell_text(cols, mapping, "rii")),
            "emp":   parse_subscription(cell_text(cols, mapping, "emp")),
            "total": parse_subscription(cell_text(cols, mapping, "total")),
        }
    return data


def parse_alm_data(html):
    """Extracts direct allotment status registrar links from the last column."""
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    data = {}
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cols = row.find_all(["td", "th"])
            if len(cols) >= 4:
                name_raw = cols[0].get_text(" ", strip=True)
                if not name_raw or "IPO" in name_raw:
                    continue
                name, _ = clean_name(name_raw)
                key = key_of(name)
                if not key:
                    continue

                # Target the last column (Allotment Status)
                link_tag = cols[-1].find("a", href=True)
                if link_tag:
                    data[key] = {
                        "url": link_tag["href"],
                        "registrar": link_tag.get_text(strip=True) or "Check Allotment"
                    }
    return data


def _shared_prefix(a, b):
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n


def match_fuzzy(mapped_dict, name):
    """
    Name matching for the small per-issue tables.

    Deliberately no longer accepts a bare substring in either direction: that
    rule matched 'MPS Limited' (key 'mps') against 'tempsensinstruments'.
    A prefix relationship, or a long shared prefix, is the safe version.
    """
    key = key_of(name)
    if not key:
        return None
    if key in mapped_dict:
        return mapped_dict[key]
    for other, values in mapped_dict.items():
        if min(len(key), len(other)) < 8:
            continue
        if key.startswith(other) or other.startswith(key):
            return values
    for other, values in mapped_dict.items():
        if _shared_prefix(key, other) >= 12:
            return values
    return None


def match_symbol(sym_map, name):
    """
    Strict lookup for tickers — a wrong symbol is worse than a missing one,
    because it silently sends you to another company's allotment page.

    Exact normalised match, or a prefix relationship between two long keys,
    and nothing else. Two different candidates means we refuse to guess.
    """
    key = key_of(name)
    if not key:
        return None
    if key in sym_map:
        return sym_map[key]

    found = None
    for other, symbol in sym_map.items():
        if min(len(key), len(other)) < 10:
            continue
        if key.startswith(other) or other.startswith(key):
            if found is not None and found != symbol:
                return None          # ambiguous
            found = symbol
    return found


STAGE_ORDER = {"open": 0, "upcoming": 1, "closed": 2, "listed": 3}


def parse_pages(gmp_html, sub_html, alm_html, sym_map=None):
    sub_map = parse_sub_data(sub_html)
    alm_map = parse_alm_data(alm_html)
    sym_map = sym_map or {}

    soup = BeautifulSoup(gmp_html, "html.parser")
    table = pick_table(soup, is_sub=False)
    if table is None:
        raise RuntimeError("No data table found — the Cloudflare check may still be active.")

    rows = table.find_all("tr")
    if not rows:
        raise RuntimeError("Table found but it has no rows.")

    mapping = map_columns(rows[0].find_all(["th", "td"]))
    records = []
    matched = 0
    listed_seen = 0
    sym_hits = 0
    unresolved = []

    for row in rows[1:]:
        cols = row.find_all(["td", "th"])
        if len(cols) < 4:
            continue

        raw_name = cell_text(cols, mapping, "name")
        if not raw_name or raw_name.strip().upper() in {"NAME", "IPO", "COMPANY"}:
            continue

        name, board = clean_name(raw_name)
        if MAINBOARD_ONLY and board == "SME":
            continue

        gmp, gain, low, high = parse_gmp(cell_text(cols, mapping, "gmp"))
        price_lo, price_hi = parse_price(cell_text(cols, mapping, "price"))
        open_label, open_gmp = parse_date_cell(cell_text(cols, mapping, "open"))
        close_label, close_gmp = parse_date_cell(cell_text(cols, mapping, "close"))
        boa_label, _ = parse_date_cell(cell_text(cols, mapping, "boa"))
        list_label, _ = parse_date_cell(cell_text(cols, mapping, "listing"))

        # realised listing result lives inside the name cell: "L@185.00 (15.62%)"
        listing_price, listing_pct = parse_listing_result(raw_name, price_hi)
        allotted = bool(re.search(r"\bALLOT(?:TED|MENT)\b", raw_name, re.I))

        open_iso = to_iso(open_label)
        close_iso = to_iso(close_label)
        boa_iso = to_iso(boa_label)
        list_iso = to_iso(list_label)

        breakdown = match_fuzzy(sub_map, name) or {}
        alm_info = match_fuzzy(alm_map, name)
        symbol = match_symbol(sym_map, name)
        narada_link = NARADA_TMPL.format(symbol=symbol) if symbol else None

        if breakdown:
            matched += 1
        if listing_pct is not None:
            listed_seen += 1
        if symbol:
            sym_hits += 1
        else:
            unresolved.append(name)

        total = breakdown.get("total")
        if total is None:
            total = parse_subscription(cell_text(cols, mapping, "sub"))

        stage = stage_for(open_iso, close_iso, list_iso)
        if listing_price is not None:
            stage = "listed"          # a printed listing price beats a stale date

        gmp_error = None
        if listing_pct is not None and gain is not None:
            gmp_error = round(listing_pct - gain, 2)

        records.append({
            "name": name,
            "board": board,
            "gmp": gmp,
            "gain_pct": gain,
            "gmp_low": low,
            "gmp_high": high,
            "price_low": price_lo,
            "price_high": price_hi,
            "size": _f(cell_text(cols, mapping, "size")),
            "sub": total,
            "qib": breakdown.get("qib"),
            "shni": breakdown.get("shni"),
            "bhni": breakdown.get("bhni"),
            "nii": breakdown.get("nii"),
            "rii": breakdown.get("rii"),
            "emp": breakdown.get("emp"),
            "open_label": open_label,
            "close_label": close_label,
            "boa_label": boa_label,
            "listing_label": list_label,
            "open_date": open_iso,
            "close_date": close_iso,
            "boa_date": boa_iso,
            "listing_date": list_iso,
            "open_gmp": open_gmp,
            "close_gmp": close_gmp,
            "listing_price": listing_price,
            "listing_pct": listing_pct,
            "gmp_error": gmp_error,
            "allotted": allotted,
            "stage": stage,
            "symbol": symbol,
            "narada_link": narada_link,
            "allotment_link": alm_info.get("url") if alm_info else None,
            "registrar": alm_info.get("registrar") if alm_info else None,
        })

    records.sort(key=lambda r: (STAGE_ORDER.get(r["stage"], 9),
                                r["close_date"] or "9999-12-31",
                                r["name"]))
    print(f"  subscription breakdown matched for {matched}/{len(records)} issues")
    print(f"  realised listing gain found for {listed_seen}/{len(records)} issues")
    print(f"  NSE symbol resolved for {sym_hits}/{len(records)} issues")
    if unresolved:
        print("  no symbol yet for: " + ", ".join(unresolved))
        print("  add to SYMBOL_FIXES as: " +
              ", ".join(f'"{key_of(n)}": "SYMBOL"' for n in unresolved[:3]))
    return records


# ----------------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------------
def main():
    """Scrape, then write docs/ipos.json. Never overwrites good data with bad."""
    payload = {"ipos": [], "fetched_at": time.time(), "error": None, "count": 0}

    try:
        gmp_html, sub_html, alm_html, sym_map = fetch_htmls()
        records = parse_pages(gmp_html, sub_html, alm_html, sym_map)
        if not records:
            raise RuntimeError("Table parsed but no issue rows survived cleaning.")
        payload["ipos"] = records
        payload["count"] = len(records)
        print(f"  parsed {len(records)} issues")
    except Exception as exc:
        print(f"  scrape failed: {exc}")
        payload["error"] = str(exc)
        # keep whatever the last good run produced, just mark it stale
        if os.path.exists(OUT_JSON):
            try:
                with open(OUT_JSON, encoding="utf-8") as fh:
                    old = json.load(fh)
                payload["ipos"] = old.get("ipos", [])
                payload["count"] = len(payload["ipos"])
                payload["fetched_at"] = old.get("fetched_at") or payload["fetched_at"]
                print(f"  kept previous snapshot of {payload['count']} issues")
            except Exception:
                pass

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    print(f"  wrote {OUT_JSON}")

    # a failed scrape that salvaged nothing should fail the workflow loudly
    return 0 if payload["ipos"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
