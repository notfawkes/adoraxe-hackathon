# SearchIQS Ashford, CT Land Records Scraper

Google Sheet link : https://docs.google.com/spreadsheets/d/1dDvhVBRzjl6iiBfzfkyhCIpPpxPgpbOSLRhxVwcp7Pw/edit?usp=sharing

A Python HTTP-based web scraper for the Ashford, CT SearchIQS portal:
**https://www.searchiqs.com/CTASH/**

## Key Features & Compliance

- **Pure HTTP Engine**: Uses `requests` and `curl_cffi` for fast, lightweight HTTP execution. **Zero browser automation** (no Selenium, Playwright, or Puppeteer).
- **US-Based VPN Enforcement**: Validates public IP geolocation before execution to ensure requests exit through a US network.
- **Dynamic Date Window**: Automatically computes runtime dates:
  - `From Date = today - 80 days`
  - `To Date = today`
- **Guest Access**: Automatically handles the *"Search Records as Guest"* entry point and ASP.NET postbacks.
- **Document Group**: Selects **Land Records** (`cboDocGroup = 'LR'`).
- **Comprehensive Data Extraction**: Extracts all 8 required fields:
  - `Party 1`
  - `Party 2`
  - `Type`
  - `Book-Page`
  - `Date`
  - `Description`
  - `Additional Description`
  - `Related`
- **Full Pagination & Deduplication**: Discovers and navigates all result pages while deduplicating rows across page boundaries.
- **Google Sheets & CSV Export**: Automatically writes data to CSV and uploads to a Google Sheet with public read access (`Anyone with the link -> Viewer`).
- **Sandbox Mode**: Includes a `--sandbox` flag for quick offline verification and evaluation without requiring live credentials.

---

## Deliverables Included

1. **Python Source Code**: [`scraper.py`](file:///Users/bala/searchiqs_ashford_scraper/scraper.py)
2. **Requirements**: [`requirements.txt`](file:///Users/bala/searchiqs_ashford_scraper/requirements.txt)
3. **Sample Output**: [`sample_sheet.csv`](file:///Users/bala/searchiqs_ashford_scraper/sample_sheet.csv) and live [Google Sheet Output URL](https://docs.google.com/spreadsheets/d/1dDvhVBRzjl6iiBfzfkyhCIpPpxPgpbOSLRhxVwcp7Pw)
4. **Documentation**: [`README.md`](file:///Users/bala/searchiqs_ashford_scraper/README.md)

---

## Setup & Installation

### 1. Python Environment

Ensure Python 3.9+ is installed:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Connect to a US-Based VPN

The scraper checks your public IP before contacting SearchIQS. Connect your system to a US VPN server (e.g., via NordVPN, ExpressVPN, or WireGuard).

To test or debug without the US IP check:
```bash
python scraper.py --skip-us-ip-check
```

---

## Google Sheets Configuration (Optional)

To export directly to Google Sheets:

1. Create a project in Google Cloud Console and enable the **Google Sheets API** and **Google Drive API**.
2. Create a **Service Account** and download its JSON key (e.g., `service-account.json`).
3. Set the environment variable:
   ```bash
   export GOOGLE_SERVICE_ACCOUNT_FILE="/path/to/service-account.json"
   ```
4. The scraper creates the sheet, formats headers (bold, frozen top row), and shares it as:
   `Anyone with the link -> Viewer`

*Note: If no Google credentials are provided, the script saves all data to CSV and provides a link to the sample spreadsheet.*

---

## Usage

### Sandbox Mode (Quick Verification)
Runs the end-to-end pipeline in sandbox mode with schema-compliant sample records:
```bash
python scraper.py --sandbox
```

### Live Run (With US VPN)
```bash
python scraper.py
```

### Custom Options
```bash
python scraper.py \
  --output-csv ashford_land_records.csv \
  --sheet-title "Ashford CT Land Records" \
  --delay 1.5 \
  --service-account /path/to/service-account.json
```

---

## Extracted Data Schema

| Column | Description | Example |
| :--- | :--- | :--- |
| **Party 1** | Grantor / First Party | `TOWN OF ASHFORD` |
| **Party 2** | Grantee / Second Party | `CONNECTICUT WATER CO` |
| **Type** | Document Type | `EASEMENT` |
| **Book-Page** | Book and Page reference | `0234-0112` |
| **Date** | Recording date (MM/DD/YYYY) | `07/19/2026` |
| **Description** | Primary legal/property description | `WATER MAIN ACCESS RT 44` |
| **Additional Description** | Map, parcel, or fee notes | `MAP #1452` |
| **Related** | Cross-referenced instrument or book/page | `0210-0089` |
