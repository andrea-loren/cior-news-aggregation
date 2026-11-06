import datetime
import re
import time
import urllib.parse
import gspread
from ddgs import DDGS
from yt_dlp import YoutubeDL

# ==========================================
# STEP 1: CONNECT TO GOOGLE SHEETS
# ==========================================
print("Connecting to Google Sheets via OAuth...")
gc = gspread.oauth(
    credentials_filename="credentials.json",
    authorized_user_filename="authorized_user.json"
)
doc = gc.open("CIOR News Tracker")
sheet = doc.sheet1

# Helper: Unwrap tracking links and strip URL query tracking parameters (keeping sub-pages)
def clean_url(raw_url):
    if not raw_url:
        return ""
    if "r.search.yahoo.com" in raw_url and "/RU=" in raw_url:
        try:
            raw_url = urllib.parse.unquote(raw_url.split("/RU=")[1].split("/RK=")[0])
        except Exception:
            pass
    
    # Standardize YouTube URLs to watch?v= format
    if "youtube.com/watch" in raw_url or "youtu.be/" in raw_url:
        video_id_match = re.search(r'(?:v=|\/)([a-zA-Z0-9_-]{11})', raw_url)
        if video_id_match:
            return f"https://www.youtube.com/watch?v={video_id_match.group(1)}"

    parsed = urllib.parse.urlparse(raw_url)
    
    # Filter out common tracking parameters but keep distinct page paths
    query_params = urllib.parse.parse_qs(parsed.query)
    filtered_params = {k: v for k, v in query_params.items() if not k.startswith(('utm_', 'nskip', 'fbclid', 'ref'))}
    new_query = urllib.parse.urlencode(filtered_params, doseq=True)
    
    clean = urllib.parse.urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        new_query,
        ""  # Strip fragment
    ))
    return clean.rstrip('/')

# Helper: Normalize Title for duplicate headline detection
def normalize_title(title):
    return re.sub(r'[^a-z0-9]', '', title.lower())

# Fetch existing URLs and Titles from main sheet
existing_urls = set(clean_url(url) for url in sheet.col_values(3) if url.strip())
existing_titles = set(normalize_title(t) for t in sheet.col_values(2) if t.strip())

# Fetch permanently excluded URLs and Titles from 'Excluded' tab
try:
    excluded_sheet = doc.worksheet("Excluded")
    excluded_urls = set(clean_url(url) for url in excluded_sheet.col_values(1) if url.strip())
    existing_urls.update(excluded_urls)
    
    excluded_titles = set(normalize_title(t) for t in excluded_sheet.col_values(2) if t.strip())
    existing_titles.update(excluded_titles)
    print(f"Loaded {len(excluded_urls)} excluded URLs and {len(excluded_titles)} excluded titles.")
except Exception:
    print("Notice: 'Excluded' tab not found. Processing active sheet only.")

def determine_type(url, title):
    url_lower = url.lower()
    title_lower = title.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "YouTube video"
    elif any(p in url_lower for p in ["twitter.com", "x.com", "instagram.com", "linkedin.com", "facebook.com"]):
        return "Social media post"
    elif "newsletter" in title_lower or "substack" in url_lower:
        return "Newsletter"
    elif "press release" in title_lower or "pr distribution" in url_lower:
        return "Press release"
    return "Article"

def extract_date(item, clean_snippet):
    for date_key in ["date", "published", "pubdate", "time", "upload_date"]:
        if item.get(date_key):
            val = str(item.get(date_key))
            if len(val) == 8 and val.isdigit():  # Converts YYYYMMDD
                return f"{val[:4]}-{val[4:6]}-{val[6:]}"
            return val[:10]
    match = re.search(r'\b(2019|202[0-6])[-/]\d{1,2}[-/]\d{1,2}\b', clean_snippet)
    return match.group(0) if match else "N/A"

def is_allowed_language(text):
    return re.search(r'[\u0400-\u04FF\u4E00-\u9FFF\u3040-\u30FF]', text) is None

search_results = []
seen_hrefs = set()

# ==========================================
# STEP 2A: YOUTUBE DIRECT SEARCH (ENGINES + YT-DLP)
# ==========================================
print("\nSearching YouTube directly (Web Index + YT-DLP engine)...")

yt_search_queries = [
    'site:youtube.com "City Island Oyster Reef"',
    'site:youtube.com "City Island" "oyster reef"',
    'site:youtube.com "CIOR" "City Island"'
]

# 1. Search web index for YouTube links
for yt_q in yt_search_queries:
    print(f"➜ Querying web index for YouTube videos: {yt_q}")
    try:
        results = list(DDGS().text(yt_q, region="us-en", max_results=30))
        for item in results:
            raw_url = item.get("href", "")
            if "youtube.com/watch" in raw_url or "youtu.be/" in raw_url:
                c_url = clean_url(raw_url)
                if c_url and c_url not in seen_hrefs:
                    seen_hrefs.add(c_url)
                    search_results.append({
                        "title": item.get("title", "").replace(" - YouTube", ""),
                        "href": c_url,
                        "body": item.get("body", ""),
                        "date": item.get("date", "")
                    })
    except Exception as e:
        print(f"Notice during YouTube index search: {e}")

# 2. Search YouTube internal engine directly via yt-dlp (catches description-only mentions)
yt_direct_terms = [
    "ytsearch15:City Island Oyster Reef",
    "ytsearch15:CIOR City Island oyster"
]

ydl_opts = {
    'quiet': True,
    'skip_download': True,
    'ignoreerrors': True,
    'no_warnings': True,
    'extract_flat': True
}

print("➜ Querying YouTube internal search for description-based mentions...")
for yt_term in yt_direct_terms:
    try:
        with YoutubeDL(ydl_opts) as ydl:
            res = ydl.extract_info(yt_term, download=False)
            if res and 'entries' in res:
                for entry in res['entries']:
                    if entry and entry.get('id'):
                        v_url = f"https://www.youtube.com/watch?v={entry['id']}"
                        c_url = clean_url(v_url)
                        if c_url and c_url not in seen_hrefs:
                            seen_hrefs.add(c_url)
                            search_results.append({
                                "title": entry.get("title", "YouTube Video"),
                                "href": c_url,
                                "body": entry.get("description", ""),
                                "date": entry.get("upload_date", "")
                            })
    except Exception as e:
        print(f"Notice during direct YouTube search: {e}")

# 3. Enrich collected YouTube entries
if search_results:
    print(f"Enriching metadata for {len(search_results)} found YouTube videos via yt-dlp...")
    for item in search_results:
        if "youtube.com" in item["href"]:
            try:
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(item["href"], download=False)
                    if info:
                        item["title"] = info.get("title", item["title"])
                        item["body"] = info.get("description", item["body"])
                        item["date"] = info.get("upload_date", item["date"])
            except Exception:
                pass

print(f"✓ Total YouTube videos collected: {len(search_results)}")

# ==========================================
# STEP 2B: TARGETED WEB & NEWS SEARCH
# ==========================================
print("\nSearching web and news for City Island Oyster Reef...")

queries = [
    '"City Island Oyster Reef"',
    'CIOR "City Island"',
    'site:bxtimes.com "City Island" "oyster"',
    'site:riverdalepress.com "City Island" "oyster"',
    'site:tv.cuny.edu "City Island" "oyster"',
    'site:cityislandchamber.org "City Island Oyster Reef"',
    'site:cityandstateny.com "City Island Oyster Reef"'
]

for q in queries:
    print(f"➜ Running web query: {q}")
    try:
        web_res = list(DDGS().text(q, region="us-en", max_results=50))
        for item in web_res:
            href = clean_url(item.get("href", ""))
            if href and href not in seen_hrefs:
                seen_hrefs.add(href)
                item["href"] = href
                search_results.append(item)
                
        news_res = list(DDGS().news(q, region="us-en", max_results=30))
        for item in news_res:
            href = clean_url(item.get("url") or item.get("href", ""))
            if href and href not in seen_hrefs:
                seen_hrefs.add(href)
                search_results.append({
                    "title": item.get("title", ""),
                    "href": href,
                    "body": item.get("body", "") or item.get("excerpt", ""),
                    "date": item.get("date", "")
                })

        time.sleep(1)
    except Exception as e:
        print(f"Notice during search for '{q}': {e}")

print(f"\nCollected {len(search_results)} total raw results across YouTube and web. Filtering...")

# ==========================================
# STEP 3: FILTER & BATCH WRITE TO GOOGLE SHEET
# ==========================================
rows_to_append = []
current_date = datetime.date.today().strftime("%Y-%m-%d")

# Banned domains (Yahoo, map sites, tax records, standard social media)
BANNED_DOMAINS = [
    "yahoo.com", "guidestar.org", "mapquest.com", "charitynavigator.org", 
    "propublica.org", "yelp.com", "tripadvisor.com", "instagram.com", 
    "facebook.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com"
]

# FIRST-PARTY DOMAINS (Blocked as requested)
CIOR_OWNED_DOMAINS = [
    "cior.org", "cityislandoysterreef.org", "cioysterreef.org"
]

# Strict noise keyword blacklist
IRRELEVANT_KEYWORDS = [
    "tybee island", "maritime academy", "cafeteria", "carvel", "deep glades", 
    "artisanal", "newburyport", "letters to the editor", "fried oysters", 
    "carton", "takeout", "menu", "restaurant", "dining", "city island 6", 
    "apk", "movie review", "andy garcia", "egypt", "cairo", "everglades",
    "drivers license", "olivia rodrigo"
]

for item in search_results:
    title = item.get("title", "No Title").strip()
    url = item.get("href", "").strip()
    body = item.get("body", "").strip()

    if not url or not title:
        continue

    clean_u = clean_url(url)
    norm_t = normalize_title(title)
    url_lower = clean_u.lower()
    clean_snippet = re.sub('<[^<]+?>', '', body)
    combined_text = (title + " " + clean_snippet).lower()
    is_youtube = "youtube.com" in url_lower or "youtu.be" in url_lower

    # Filter 1: Block PDFs
    if url_lower.endswith(".pdf") or ".pdf?" in url_lower:
        print(f"✗ Filtered out (PDF/Document): {title}")
        continue

    # Filter 2: Block banned non-YouTube domains
    if not is_youtube and any(domain in url_lower for domain in BANNED_DOMAINS):
        print(f"✗ Filtered out (Banned Domain): {title}")
        continue

    # Filter 3: Block CIOR first-party websites
    if any(domain in url_lower for domain in CIOR_OWNED_DOMAINS):
        print(f"✗ Filtered out (CIOR First-Party Content): {title}")
        continue

    # Filter 4: Duplicate URL check
    if clean_u in existing_urls:
        print(f"➜ Skipping duplicate URL: {title}")
        continue

    # Filter 5: Duplicate Headline Title check
    if norm_t in existing_titles:
        print(f"➜ Skipping duplicate headline: {title}")
        continue

    # Filter 6: Non-English language screen
    if not is_allowed_language(title + " " + clean_snippet):
        print(f"✗ Filtered out (Foreign Script): {title}")
        continue

    # Filter 7: Strict Keyword Blacklist Check
    if any(k in combined_text for k in IRRELEVANT_KEYWORDS):
        print(f"✗ Filtered out (Blacklisted Noise Keyword): {title}")
        continue

    # Filter 8: Mandatory Entity Match Safeguard
    # Permits local site query matches even if the snippet only has partial terms
    is_local_outlet = any(d in url_lower for d in ["riverdalepress.com", "bxtimes.com", "tv.cuny.edu", "cityislandchamber.org"])
    if not is_local_outlet and ("city island oyster reef" not in combined_text and "cior" not in combined_text):
        print(f"✗ Filtered out (Does not mention City Island Oyster Reef directly): {title}")
        continue

    # Filter 9: Exclude Pre-2019 Historical Content
    if any(year in combined_text for year in ["2015", "2016", "2017", "2018"]):
        print(f"✗ Filtered out (Pre-2019 Content): {title}")
        continue

    media_type = determine_type(clean_u, title)
    pub_date = extract_date(item, clean_snippet)

    print(f"✓ Valid mention found ({media_type}): {title}")
    row_data = [
        current_date,     # Col A: Date Found
        title,            # Col B: Title
        clean_u,          # Col C: Cleaned URL
        media_type,       # Col D: Media Type
        clean_snippet,    # Col E: Summary Snippet
        pub_date,         # Col F: Date Published
        "Pending"         # Col G: Human Review Status
    ]
    
    rows_to_append.append(row_data)
    existing_urls.add(clean_u)
    existing_titles.add(norm_t)

# Write all collected rows in a SINGLE API call
if rows_to_append:
    print(f"\nWriting {len(rows_to_append)} new rows to Google Sheets...")
    sheet.append_rows(rows_to_append, table_range="A1")
    print("✓ Batch write successful!")
else:
    print("\nNo new relevant mentions found.")

print("Processing complete! Check your Google Sheet.")
