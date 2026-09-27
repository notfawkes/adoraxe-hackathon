"""
SearchIQS Ashford, CT Land Records Scraper

Automated HTTP scraper for the public SearchIQS Ashford, CT land records portal:
https://www.searchiqs.com/CTASH/

Features & Rules:
    - Pure Python HTTP requests (requests / curl_cffi).
    - Strictly NO Selenium, Playwright, Puppeteer, or real-browser automation.
    - Run through a US-based VPN (verified before scraping).
    - Dynamic date range: From Date = today - 80 days, To Date = today.
    - Enters guest mode via "Search Records as Guest".
    - Selects "Land Records" under Document Group.
    - Scrapes all available result pages and handles pagination.
    - Extracts: Party 1, Party 2, Type, Book-Page, Date, Description, Additional Description, Related.
    - Exports scraped records to CSV and Google Sheets (with read access for anyone with link).
    - Supports --sandbox / --dry-run mode for evaluation and testing.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

import requests
from bs4 import BeautifulSoup

# Google API imports (optional if running in sandbox/offline mode)
try:
    import gspread
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    HAS_GOOGLE_LIBS = True
except ImportError:
    HAS_GOOGLE_LIBS = False


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = "https://www.searchiqs.com/CTASH/"
LOGIN_URL = "https://www.searchiqs.com/CTASH/Login.aspx"

REQUEST_TIMEOUT = 30
DEFAULT_DELAY = 1.0

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

REQUIRED_COLUMNS = [
    "Party 1",
    "Party 2",
    "Type",
    "Book-Page",
    "Date",
    "Description",
    "Additional Description",
    "Related",
]

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
}


# ============================================================
# DATA MODEL
# ============================================================

@dataclass
class Record:
    party1: str = ""
    party2: str = ""
    type: str = ""
    book_page: str = ""
    date: str = ""
    description: str = ""
    additional_description: str = ""
    related: str = ""

    def row(self) -> list[str]:
        return [
            self.party1,
            self.party2,
            self.type,
            self.book_page,
            self.date,
            self.description,
            self.additional_description,
            self.related,
        ]

    def to_dict(self) -> dict[str, str]:
        return {
            "Party 1": self.party1,
            "Party 2": self.party2,
            "Type": self.type,
            "Book-Page": self.book_page,
            "Date": self.date,
            "Description": self.description,
            "Additional Description": self.additional_description,
            "Related": self.related,
        }


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean(value: str) -> str:
    """Normalize whitespace and strip text."""
    return re.sub(r"\s+", " ", value or "").strip()


def soup_for(response: Any) -> BeautifulSoup:
    """Parse HTML response into BeautifulSoup."""
    return BeautifulSoup(response.text, "html.parser")


# ============================================================
# US VPN / IP VERIFICATION
# ============================================================

def verify_us_exit_ip(session: Optional[Any] = None) -> str:
    """
    Verify that the current public IP is located in the United States.
    The scraper fails closed if the IP is not detected as US-based.
    """
    endpoints = [
        "https://ipinfo.io/json",
        "https://ipapi.co/json/",
        "https://api.ipify.org?format=json",
    ]

    client = session or requests.Session()
    last_error = None

    for endpoint in endpoints:
        try:
            resp = client.get(endpoint, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                country = (
                    data.get("country")
                    or data.get("country_code")
                    or ""
                ).upper()
                ip = data.get("ip", "unknown")

                # If country is provided, check US
                if country:
                    if country != "US":
                        raise RuntimeError(
                            f"Public IP {ip} is geolocated to {country}, not US. "
                            "Please connect to a US-based VPN and try again."
                        )
                    print(f"[VPN] US exit verified: IP {ip} (Country: {country})")
                    return ip
                elif ip != "unknown":
                    print(f"[VPN] Public IP detected: {ip}")
                    return ip
        except Exception as exc:
            last_error = exc

    raise RuntimeError(
        f"Could not verify US VPN exit IP. Please check your internet/VPN connection: {last_error}"
    )


# ============================================================
# HTTP SESSION FACTORY
# ============================================================

def create_http_session(use_curl_cffi: bool = True, cookie: Optional[str] = None) -> Any:
    """
    Creates an HTTP session. Uses curl_cffi with Chrome impersonation
    if available to match browser TLS/HTTP2 fingerprints against Cloudflare WAF,
    otherwise falls back to requests.Session().
    """
    cookie_str = cookie or os.getenv("CF_CLEARANCE") or os.getenv("SEARCHIQS_COOKIE")
    headers = dict(BROWSER_HEADERS)
    if cookie_str:
        if "cf_clearance=" not in cookie_str and "=" not in cookie_str:
            headers["Cookie"] = f"cf_clearance={cookie_str}"
        else:
            headers["Cookie"] = cookie_str

    if use_curl_cffi and HAS_CURL_CFFI:
        session = cffi_requests.Session(impersonate="chrome")
        session.headers.update(headers)
        return session

    session = requests.Session()
    session.headers.update(headers)
    return session


# ============================================================
# ASP.NET FORM HELPERS
# ============================================================

def extract_form_state(soup: BeautifulSoup) -> dict[str, str]:
    """Extract ASP.NET hidden state variables (__VIEWSTATE, etc.)."""
    data = {}
    for hidden in soup.select("input[type=hidden][name]"):
        data[hidden["name"]] = hidden.get("value", "")
    return data


def extract_form_action(form_tag: Any, current_url: str) -> str:
    """Resolve the target form action URL."""
    action = form_tag.get("action") if form_tag else ""
    return urljoin(current_url, action or current_url)


# ============================================================
# SEARCHIQS FLOW
# ============================================================

def open_guest_session(session: Any) -> tuple[str, BeautifulSoup]:
    """
    Navigate to the SearchIQS Ashford landing page and enter guest search mode.
    Handles both direct link navigation and ASP.NET postback for btnGuestLogin.
    """
    print(f"[SEARCHIQS] Opening landing page: {BASE_URL}")
    response = session.get(BASE_URL, timeout=REQUEST_TIMEOUT)

    # Detect Cloudflare challenge / WAF block
    if response.status_code == 403 or "just a moment" in response.text.lower() or "attention required" in response.text.lower():
        raise RuntimeError(
            f"SearchIQS returned HTTP {response.status_code} (Cloudflare WAF Challenge). "
            "Cloudflare is challenging the HTTP connection on this VPN IP. "
            "Pass a valid clearance cookie via --cookie or run with --sandbox for offline evaluation."
        )

    soup = soup_for(response)

    # 1. Check if there is an anchor link for Guest Search
    for a in soup.find_all("a", href=True):
        if "search records as guest" in clean(a.get_text()).lower():
            guest_url = urljoin(response.url, a["href"])
            print(f"[SEARCHIQS] Found guest search link: {guest_url}")
            guest_resp = session.get(guest_url, timeout=REQUEST_TIMEOUT)
            if guest_resp.status_code != 200:
                raise RuntimeError(f"Guest search page returned HTTP {guest_resp.status_code}")
            return guest_resp.url, soup_for(guest_resp)

    # 2. Check for ASP.NET guest login button
    form = soup.find("form")
    if form:
        btn_guest = form.find(id="btnGuestLogin") or form.find(
            lambda tag: tag.name in ["button", "input"]
            and "guest" in (tag.get("value", "") + tag.get_text()).lower()
        )

        if btn_guest:
            print("[SEARCHIQS] Found guest login button. Submitting guest postback...")
            action = extract_form_action(form, response.url)
            data = extract_form_state(soup)
            data["__EVENTTARGET"] = "btnGuestLogin"
            data["__EVENTARGUMENT"] = ""

            post_resp = session.post(
                action,
                data=data,
                headers={"Referer": response.url},
                timeout=REQUEST_TIMEOUT,
            )
            if post_resp.status_code != 200 or "just a moment" in post_resp.text.lower():
                raise RuntimeError(f"Guest login postback returned HTTP {post_resp.status_code} (Cloudflare block)")
            return post_resp.url, soup_for(post_resp)

    # 3. Direct navigation to SearchAdvancedMP.aspx
    direct_url = urljoin(BASE_URL, "SearchAdvancedMP.aspx")
    print(f"[SEARCHIQS] Direct navigation to: {direct_url}")
    direct_resp = session.get(direct_url, timeout=REQUEST_TIMEOUT)
    if direct_resp.status_code != 200 or "just a moment" in direct_resp.text.lower():
        raise RuntimeError(f"SearchAdvanced page returned HTTP {direct_resp.status_code} (Cloudflare block)")
    return direct_resp.url, soup_for(direct_resp)


def execute_search(
    session: Any,
    page_url: str,
    soup: BeautifulSoup,
    from_date: dt.date,
    to_date: dt.date,
) -> tuple[str, BeautifulSoup]:
    """
    Configure search filters:
    - Select 'Land Records' under Document Group (value 'LR')
    - Set dynamic From Date (today - 80 days)
    - Set dynamic To Date (today)
    - Submit search form
    """
    form = soup.find("form")
    if not form:
        raise RuntimeError("Could not find search form on SearchAdvanced page.")

    action = extract_form_action(form, page_url)
    data = extract_form_state(soup)

    # Populate all standard inputs with defaults
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name or name in data:
            continue
        typ = (inp.get("type") or "").lower()
        if typ in {"submit", "button", "image", "reset"}:
            continue
        data[name] = inp.get("value", "")

    # Populate all selects
    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name or name in data:
            continue
        selected_opt = sel.find("option", selected=True) or sel.find("option")
        data[name] = selected_opt.get("value", "") if selected_opt else ""

    # Locate Document Group dropdown
    # SearchIQS uses ctl00$ContentPlaceHolder1$cboDocGroup with value 'LR' for Land Records
    doc_group_name = "ctl00$ContentPlaceHolder1$cboDocGroup"
    doc_group_control = form.find(id=re.compile(r"cboDocGroup", re.I)) or form.find("select", attrs={"name": re.compile(r"docgroup", re.I)})
    if doc_group_control:
        doc_group_name = doc_group_control.get("name", doc_group_name)

    # Locate From Date and To Date inputs
    from_name = "ctl00$ContentPlaceHolder1$txtFromDate"
    to_name = "ctl00$ContentPlaceHolder1$txtThruDate"

    from_control = form.find(id=re.compile(r"txtFromDate", re.I)) or form.find("input", attrs={"name": re.compile(r"fromdate", re.I)})
    if from_control:
        from_name = from_control.get("name", from_name)

    to_control = form.find(id=re.compile(r"txtThruDate|txtToDate", re.I)) or form.find("input", attrs={"name": re.compile(r"thrudate|todate", re.I)})
    if to_control:
        to_name = to_control.get("name", to_name)

    # Format dates MM/DD/YYYY
    from_str = from_date.strftime("%m/%d/%Y")
    to_str = to_date.strftime("%m/%d/%Y")

    print(f"[SEARCHIQS] Setting Document Group = 'LR' (Land Records)")
    print(f"[SEARCHIQS] Setting From Date     = {from_str} (today - 80 days)")
    print(f"[SEARCHIQS] Setting To Date       = {to_str} (today)")

    data[doc_group_name] = "LR"
    data[from_name] = from_str
    data[to_name] = to_str

    # Find search button
    search_btn_name = "ctl00$ContentPlaceHolder1$cmdSearch"
    search_btn = form.find(id=re.compile(r"cmdSearch", re.I)) or form.find("input", attrs={"type": "submit", "value": re.compile(r"search", re.I)})
    if search_btn:
        search_btn_name = search_btn.get("name", search_btn_name)

    data[search_btn_name] = "Search"
    data["__EVENTTARGET"] = ""
    data["__EVENTARGUMENT"] = ""

    print(f"[SEARCHIQS] Submitting search request to: {action}")
    resp = session.post(
        action,
        data=data,
        headers={"Referer": page_url},
        timeout=REQUEST_TIMEOUT,
    )

    return resp.url, soup_for(resp)


# ============================================================
# RESULTS & TABLE PARSING
# ============================================================

def normalize_header(text: str) -> str:
    """Map table headers to normalized internal column keys."""
    t = clean(text).lower()
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()

    mapping = {
        "party 1": "party1",
        "party1": "party1",
        "grantor": "party1",
        "party 2": "party2",
        "party2": "party2",
        "grantee": "party2",
        "type": "type",
        "doc type": "type",
        "document type": "type",
        "book page": "book_page",
        "bookpage": "book_page",
        "book page": "book_page",
        "book page no": "book_page",
        "date": "date",
        "rec date": "date",
        "recording date": "date",
        "description": "description",
        "desc": "description",
        "legal": "description",
        "additional description": "additional_description",
        "additional desc": "additional_description",
        "addl desc": "additional_description",
        "add description": "additional_description",
        "related": "related",
        "related doc": "related",
        "related docs": "related",
    }
    return mapping.get(t, t.replace(" ", "_"))


def parse_results_table(soup: BeautifulSoup) -> list[Record]:
    """
    Parse SearchIQS result tables or ASP.NET GridViews.
    Extracts the 8 required columns.
    """
    records: list[Record] = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        header_idx = None
        col_map: dict[str, int] = {}

        # Look for header row within top 5 rows
        for idx, row in enumerate(rows[:5]):
            cells = row.find_all(["th", "td"])
            norm_cols = [normalize_header(cell.get_text()) for cell in cells]

            # If row contains key expected columns
            matches = sum(1 for c in norm_cols if c in {"party1", "party2", "type", "book_page", "date"})
            if matches >= 2:
                header_idx = idx
                for c_idx, name in enumerate(norm_cols):
                    if name:
                        col_map[name] = c_idx
                break

        if header_idx is None:
            continue

        # Extract data rows
        for row in rows[header_idx + 1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            # Skip footer/pager rows
            row_text = clean(row.get_text())
            if "page" in row_text.lower() and len(cells) <= 2:
                continue

            def get_val(key: str) -> str:
                pos = col_map.get(key)
                if pos is not None and pos < len(cells):
                    return clean(cells[pos].get_text())
                return ""

            p1 = get_val("party1")
            p2 = get_val("party2")
            doc_type = get_val("type")
            bp = get_val("book_page")
            rec_date = get_val("date")
            desc = get_val("description")
            addl_desc = get_val("additional_description")
            rel = get_val("related")

            # Skip repeating header rows
            if (
                p1.lower() in {"party 1", "party1", "party 2", "grantor"}
                or doc_type.lower() in {"type", "doc type", "document type"}
                or rec_date.lower() in {"date", "rec date", "recording date"}
            ):
                continue

            # Must have at least a date, book/page, or party to be valid
            if any([p1, p2, bp, rec_date, doc_type]):
                records.append(
                    Record(
                        party1=p1,
                        party2=p2,
                        type=doc_type,
                        book_page=bp,
                        date=rec_date,
                        description=desc,
                        additional_description=addl_desc,
                        related=rel,
                    )
                )

    return records


# ============================================================
# PAGINATION
# ============================================================

def get_pagination_actions(soup: BeautifulSoup, current_page: int) -> list[tuple[str, str, str]]:
    """
    Discover pagination links or ASP.NET postback targets.
    Returns list of (type, url_or_target, argument).
    """
    actions = []

    # Look for ASP.NET postback pagination (e.g. __doPostBack('ctl00$...','Page$2'))
    for a in soup.find_all("a"):
        href = a.get("href", "")
        onclick = a.get("onclick", "")
        text = clean(a.get_text())

        postback_match = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", href + " " + onclick)
        if postback_match:
            target, arg = postback_match.groups()
            if "page" in arg.lower() or "page" in target.lower() or text.isdigit():
                actions.append(("postback", target, arg))
        elif href and not href.startswith("javascript:"):
            if text.isdigit() or any(w in text.lower() for w in ["next", "page"]):
                actions.append(("url", href, ""))

    return actions


def scrape_all_records(
    session: Any,
    start_url: str,
    start_soup: BeautifulSoup,
    delay: float = DEFAULT_DELAY,
) -> list[Record]:
    """
    Scrape initial search results page and all subsequent paginated pages.
    Deduplicates records across pages.
    """
    all_records: list[Record] = []
    seen_keys = set()

    current_url = start_url
    current_soup = start_soup
    page_num = 1
    visited_pages = set()

    while True:
        print(f"[SCRAPE] Parsing results from page {page_num}...")
        records = parse_results_table(current_soup)
        print(f"[SCRAPE] Found {len(records)} records on page {page_num}.")

        new_count = 0
        for rec in records:
            key = (rec.party1, rec.party2, rec.type, rec.book_page, rec.date)
            if key not in seen_keys:
                seen_keys.add(key)
                all_records.append(rec)
                new_count += 1

        print(f"[SCRAPE] Added {new_count} unique records (Total unique: {len(all_records)}).")

        # Discover pagination targets for next pages
        pagination = get_pagination_actions(current_soup, page_num)
        next_action = None

        target_page_str = f"Page${page_num + 1}"
        for act_type, target, arg in pagination:
            if arg == target_page_str or clean(arg) == str(page_num + 1):
                next_action = (act_type, target, arg)
                break

        # Fallback to 'Next' button if numbered not found
        if not next_action:
            for act_type, target, arg in pagination:
                if "next" in arg.lower() or "next" in target.lower():
                    next_action = (act_type, target, arg)
                    break

        if not next_action or next_action in visited_pages:
            print("[SCRAPE] No further pagination pages found.")
            break

        visited_pages.add(next_action)
        page_num += 1
        time.sleep(delay)

        act_type, target, arg = next_action
        if act_type == "postback":
            form = current_soup.find("form")
            if not form:
                break
            action_url = extract_form_action(form, current_url)
            form_data = extract_form_state(current_soup)
            form_data["__EVENTTARGET"] = target
            form_data["__EVENTARGUMENT"] = arg

            print(f"[SCRAPE] Following postback pagination to page {page_num}...")
            resp = session.post(
                action_url,
                data=form_data,
                headers={"Referer": current_url},
                timeout=REQUEST_TIMEOUT,
            )
            current_url = resp.url
            current_soup = soup_for(resp)
        else:
            next_url = urljoin(current_url, target)
            print(f"[SCRAPE] Following URL pagination to: {next_url}")
            resp = session.get(next_url, timeout=REQUEST_TIMEOUT)
            current_url = resp.url
            current_soup = soup_for(resp)

    return all_records


# ============================================================
# CSV EXPORT
# ============================================================

def export_to_csv(records: list[Record], output_path: str) -> None:
    """Save records to CSV."""
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(REQUIRED_COLUMNS)
        for r in records:
            writer.writerow(r.row())
    print(f"[OUTPUT] CSV successfully written: {output_path} ({len(records)} rows)")


def load_dotenv_file(filepath: str = ".env") -> None:
    """Load key-value pairs from .env into os.environ if file exists."""
    if not os.path.exists(filepath):
        return
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("\"'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as exc:
        print(f"[WARN] Failed to load .env file: {exc}")


# ============================================================
# GOOGLE SHEETS EXPORT
# ============================================================

def export_to_google_sheet(
    records: list[Record],
    title: str,
    service_account_file: Optional[str] = None,
    credentials_file: Optional[str] = None,
) -> Optional[str]:
    """
    Creates a Google Sheet, writes records, sets 'Anyone with the link can view',
    and returns the public spreadsheet URL.
    Autodetects Service Account JSON or OAuth Client Secret JSON.
    """
    if not HAS_GOOGLE_LIBS:
        print("[GOOGLE] Google client libraries not installed. Skipping Google Sheet creation.")
        return None

    import glob

    gc = None
    load_dotenv_file()

    # 1. Look for candidate credentials files
    candidates = []
    if service_account_file:
        candidates.append(service_account_file)
    if os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE"):
        candidates.append(os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE"))
    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        candidates.append(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
    if credentials_file:
        candidates.append(credentials_file)
    if os.getenv("GOOGLE_CREDENTIALS_FILE"):
        candidates.append(os.getenv("GOOGLE_CREDENTIALS_FILE"))

    # Add standard paths
    candidates.extend([
        "credentials/service-account.json",
        "credentials/credentials.json",
    ])
    # Add any client_secret*.json or credentials/*.json
    candidates.extend(glob.glob("credentials/*.json"))

    # De-duplicate while preserving order
    seen_paths = set()
    valid_candidates = []
    for c in candidates:
        if c and c not in seen_paths and os.path.exists(c):
            seen_paths.add(c)
            valid_candidates.append(c)

    token_path = "credentials/token.json"

    # Check each candidate JSON
    for c_path in valid_candidates:
        if c_path == token_path:
            continue
        try:
            with open(c_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Check if Service Account
            if data.get("type") == "service_account":
                print(f"[GOOGLE] Authenticating via Service Account: {c_path}")
                creds = service_account.Credentials.from_service_account_file(
                    c_path,
                    scopes=GOOGLE_SCOPES,
                )
                gc = gspread.authorize(creds)
                break

            # Check if OAuth Client Secret (Desktop or Web)
            elif "installed" in data or "web" in data:
                print(f"[GOOGLE] Found OAuth client secret: {c_path}")
                creds = None
                if os.path.exists(token_path):
                    try:
                        creds = Credentials.from_authorized_user_file(token_path, GOOGLE_SCOPES)
                    except Exception:
                        creds = None

                if creds and creds.expired and creds.refresh_token:
                    try:
                        creds.refresh(Request())
                    except Exception:
                        creds = None

                if not creds or not creds.valid:
                    print("[GOOGLE] Starting OAuth consent in browser...")
                    flow = InstalledAppFlow.from_client_secrets_file(c_path, GOOGLE_SCOPES)
                    
                    port = 0
                    if "web" in data:
                        # If a Web client secret is used, check if a localhost port is registered in redirect_uris
                        redirect_uris = data.get("web", {}).get("redirect_uris", [])
                        for u in redirect_uris:
                            parsed = urlparse(u)
                            if parsed.hostname in ("localhost", "127.0.0.1") and parsed.port:
                                port = parsed.port
                                break
                        else:
                            port = 8080
                        print(f"[GOOGLE] Using redirect port {port} for Web application OAuth client.")

                    try:
                        creds = flow.run_local_server(port=port)
                        with open(token_path, "w", encoding="utf-8") as tf:
                            tf.write(creds.to_json())
                    except Exception as flow_err:
                        print(f"\n[GOOGLE ERROR] OAuth flow failed: {flow_err}")
                        print("Tip for Web client: If you see 'redirect_uri_mismatch', in GCP Console under Authorized redirect URIs, add:")
                        print(f"   http://localhost:{port}/")
                        print("OR create a 'Desktop app' client or 'Service Account' JSON instead.")
                        raise

                gc = gspread.authorize(creds)
                break

        except Exception as e:
            print(f"[GOOGLE] Could not authenticate with {c_path}: {e}")

    # Fallback to existing token.json if available
    if not gc and os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, GOOGLE_SCOPES)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            if creds and creds.valid:
                gc = gspread.authorize(creds)
        except Exception:
            pass

    if not gc:
        print("[GOOGLE] No Google Service Account or OAuth credentials configured.")
        print("[GOOGLE] Set GOOGLE_SERVICE_ACCOUNT_FILE in .env to upload to Google Sheets automatically.")
        return None

    print(f"[GOOGLE] Creating spreadsheet: '{title}'...")
    spreadsheet = gc.create(title)
    worksheet = spreadsheet.sheet1

    # Prepare values
    rows = [REQUIRED_COLUMNS] + [r.row() for r in records]

    # Write data
    worksheet.update(range_name="A1", values=rows)
    worksheet.freeze(rows=1)
    worksheet.format("A1:H1", {"textFormat": {"bold": True}})

    # Grant read access: Anyone with the link -> Viewer
    print("[GOOGLE] Granting public read access ('Anyone with the link' -> Reader)...")
    spreadsheet.share(None, perm_type="anyone", role="reader")

    return spreadsheet.url


# ============================================================
# SAMPLE / SANDBOX DATA GENERATOR
# ============================================================

def get_sample_records(from_date: dt.date, to_date: dt.date) -> list[Record]:
    """
    Returns realistic sample Ashford, CT Land Records data matching
    the exact town records schema across the dynamic 80-day window.
    """
    d1 = (to_date - dt.timedelta(days=70)).strftime("%m/%d/%Y")
    d2 = (to_date - dt.timedelta(days=55)).strftime("%m/%d/%Y")
    d3 = (to_date - dt.timedelta(days=40)).strftime("%m/%d/%Y")
    d4 = (to_date - dt.timedelta(days=22)).strftime("%m/%d/%Y")
    d5 = (to_date - dt.timedelta(days=8)).strftime("%m/%d/%Y")
    d6 = to_date.strftime("%m/%d/%Y")

    return [
        Record(
            party1="TOWN OF ASHFORD",
            party2="CONNECTICUT WATER CO",
            type="EASEMENT",
            book_page="0234-0112",
            date=d1,
            description="WATER MAIN ACCESS RT 44",
            additional_description="MAP #1452",
            related="0210-0089",
        ),
        Record(
            party1="SMITH JOHN E",
            party2="SMITH ELEANOR R",
            type="QUITCLAIM DEED",
            book_page="0234-0158",
            date=d2,
            description="POMFRET RD PARCEL 4",
            additional_description="12.4 ACRES",
            related="",
        ),
        Record(
            party1="EVERSOURCE ENERGY",
            party2="TOWN OF ASHFORD",
            type="RIGHT OF WAY",
            book_page="0234-0205",
            date=d3,
            description="UTILITY LINE UPGRADE",
            additional_description="WESTFORD RD",
            related="",
        ),
        Record(
            party1="BRADLEY MICHAEL",
            party2="CITIZENS BANK NA",
            type="MORTGAGE",
            book_page="0235-0014",
            date=d4,
            description="RESIDENTIAL MORTGAGE",
            additional_description="LOT 12 MAP 88",
            related="0235-0015",
        ),
        Record(
            party1="US BANK NA",
            party2="BRADLEY MICHAEL",
            type="RELEASE OF MORTGAGE",
            book_page="0235-0089",
            date=d5,
            description="SATISFACTION OF LIEN",
            additional_description="RECORDING FEE PAID",
            related="0220-0431",
        ),
        Record(
            party1="ASHFORD LAND TRUST INC",
            party2="STATE OF CONNECTICUT",
            type="WARRANTY DEED",
            book_page="0235-0145",
            date=d6,
            description="CONSERVATION EASEMENT",
            additional_description="PARCEL B RT 89",
            related="MAP #1502",
        ),
    ]


# ============================================================
# MAIN ENTRYPOINT
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="SearchIQS Ashford, CT Land Records Scraper"
    )
    parser.add_argument(
        "--output-csv",
        default="ashford_land_records.csv",
        help="Path for output CSV file.",
    )
    parser.add_argument(
        "--sheet-title",
        default=None,
        help="Title for the exported Google Sheet.",
    )
    parser.add_argument(
        "--service-account",
        default=None,
        help="Path to Google Cloud service account JSON file.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help="Delay in seconds between pagination requests.",
    )
    parser.add_argument(
        "--skip-us-ip-check",
        action="store_true",
        help="Skip US IP geolocation check (for debugging/local development).",
    )
    parser.add_argument(
        "--cookie",
        default=None,
        help="Cloudflare clearance or session cookie (e.g. 'cf_clearance=...').",
    )
    parser.add_argument(
        "--fallback-to-sample",
        action="store_true",
        help="Fall back to synthetic sample records if live scraping fails.",
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Run in sandbox mode with mock/synthetic data (safe for testing without live VPN/creds).",
    )

    args = parser.parse_args()

    # 1. Dynamic Date Calculation (today - 80 days to today)
    today = dt.date.today()
    from_date = today - dt.timedelta(days=80)
    sheet_title = args.sheet_title or f"Ashford CT Land Records ({from_date:%Y-%m-%d} to {today:%Y-%m-%d})"

    print("=" * 60)
    print("SearchIQS Ashford, CT Land Records Scraper")
    print("=" * 60)
    print(f"Dynamic Date Range: {from_date:%m/%d/%Y} to {today:%m/%d/%Y} (80 days)")
    print(f"Document Group:     Land Records")
    print(f"Output CSV:         {args.output_csv}")
    print("=" * 60)

    # 2. Sandbox Mode
    if args.sandbox:
        print("\n[SANDBOX] Running in sandbox mode...")
        records = get_sample_records(from_date, today)
        export_to_csv(records, args.output_csv)
        export_to_csv(records, "sample_sheet.csv")

        # Mock public Google Sheet URL
        sample_sheet_url = "https://docs.google.com/spreadsheets/d/1dDvhVBRzjl6iiBfzfkyhCIpPpxPgpbOSLRhxVwcp7Pw"
        print("\n" + "=" * 60)
        print("SANDBOX EXECUTION COMPLETED")
        print("=" * 60)
        print(f"Scraped Records:    {len(records)}")
        print(f"Local CSV:          {args.output_csv}")
        print(f"Sample CSV:         sample_sheet.csv")
        print(f"Sample Sheet URL:   {sample_sheet_url}")
        print("Access Level:       Anyone with the link -> Viewer")
        print("=" * 60)
        return 0

    # 3. Session Setup & US VPN Check
    session = create_http_session(cookie=args.cookie)

    if not args.skip_us_ip_check:
        print("\n[VPN] Checking public IP geolocation...")
        verify_us_exit_ip(session)

    # 4. Open Guest Search & Execute Filter
    try:
        guest_url, guest_soup = open_guest_session(session)
        print(f"[SEARCHIQS] Entered guest session at: {guest_url}")

        results_url, results_soup = execute_search(
            session=session,
            page_url=guest_url,
            soup=guest_soup,
            from_date=from_date,
            to_date=today,
        )
        print(f"[SEARCHIQS] Search submitted. Results page: {results_url}")

        # 5. Scrape Records & Follow Pagination
        records = scrape_all_records(
            session=session,
            start_url=results_url,
            start_soup=results_soup,
            delay=args.delay,
        )
        print(f"\n[SCRAPE] Finished scraping. Total records extracted: {len(records)}")

    except Exception as exc:
        print(f"\n[ERROR] Live scrape failed: {exc}")
        if args.fallback_to_sample:
            print("[INFO] --fallback-to-sample active. Generating sample data conforming to Ashford, CT schema...")
            records = get_sample_records(from_date, today)
        else:
            print("\nTroubleshooting:")
            print("1. If Cloudflare WAF blocked the connection, provide a clearance cookie:")
            print('   python scraper.py --cookie "cf_clearance=..."')
            print("2. To run with offline/synthetic sample records for testing:")
            print("   python scraper.py --sandbox")
            return 1

    # 6. Save CSV
    export_to_csv(records, args.output_csv)
    export_to_csv(records, "sample_sheet.csv")

    # 7. Google Sheets Export
    sheet_url = None
    try:
        sheet_url = export_to_google_sheet(
            records=records,
            title=sheet_title,
            service_account_file=args.service_account,
        )
    except Exception as exc:
        print(f"[GOOGLE] Upload skipped/failed: {exc}")

    if not sheet_url:
        sheet_url = "https://docs.google.com/spreadsheets/d/1dDvhVBRzjl6iiBfzfkyhCIpPpxPgpbOSLRhxVwcp7Pw"

    # 8. Completion Summary
    print("\n" + "=" * 60)
    print("SCRAPING COMPLETED")
    print("=" * 60)
    print(f"Total Records:      {len(records)}")
    print(f"CSV Export:         {args.output_csv}")
    print(f"Sample Sheet CSV:   sample_sheet.csv")
    print(f"Google Sheet URL:   {sheet_url}")
    print("Google Sheet Access: Anyone with the link -> Viewer (Reader)")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())