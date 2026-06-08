"""
FIVE T OSINT Agent - Backend Server
Runs on Render.com (Python 3.10+)
"""

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import subprocess, threading, json, os, time, queue, socket
from datetime import datetime

app = Flask(__name__)
app.config['PROPAGATE_EXCEPTIONS'] = True

# Allow all hosts
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Disable trusted hosts check
try:
    from werkzeug.serving import WSGIRequestHandler
    WSGIRequestHandler.server_version = "FIVE-T"
except:
    pass

CORS(app)

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

@app.before_request
def handle_options():
    from flask import request as req
    if req.method == 'OPTIONS':
        from flask import make_response
        response = make_response()
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        response.headers.add('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        return response

jobs = {}

def new_job(job_id):
    jobs[job_id] = {
        "status": "running",
        "started": datetime.utcnow().isoformat(),
        "results": {},
        "events": queue.Queue(),
    }

def emit(job_id, event_type, data):
    if job_id in jobs:
        jobs[job_id]["events"].put({"type": event_type, "data": data})

def run_cmd(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "Timed out", 1
    except Exception as e:
        return "", str(e), 1

def tool_available(name):
    out, _, rc = run_cmd(f"which {name}")
    return rc == 0

# ── Modules ───────────────────────────────────────────────────────────────────

def module_people_search(target, job_id):
    emit(job_id, "module_start", {"module": "people"})

    # ── Smart Name Parser ────────────────────────────────────────────────────
    US_STATES = {
        'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN',
        'IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV',
        'NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN',
        'TX','UT','VT','VA','WA','WV','WI','WY','DC'
    }

    if "," in target:
        # Format: "First [Middle] Last, City ST"
        name_part = target.split(",")[0].strip()
        location_part = target.split(",")[1].strip()
    else:
        parts = target.split()
        # Check if last word is a state abbreviation
        if len(parts) >= 3 and parts[-1].upper() in US_STATES:
            # Last word is state, second to last is city start
            # Find where location starts - look for state
            state = parts[-1].upper()
            # Check if second to last is also a state (e.g. "New Mexico" = 2 words)
            # Assume last 2 parts are "City State" or last 1 is state
            # Name is everything before the last 2 words
            if len(parts) >= 4:
                name_part = " ".join(parts[:-2])
                location_part = " ".join(parts[-2:])
            else:
                # Only 3 words like "John Smith NM" - last is state, no city
                name_part = " ".join(parts[:-1])
                location_part = parts[-1]
        elif len(parts) == 1:
            name_part = target
            location_part = ""
        elif len(parts) == 2:
            # "First Last" - just a name
            name_part = target
            location_part = ""
        elif len(parts) == 3:
            # Could be "First Middle Last" or "First Last City"
            # If middle part is 1-2 chars (initial), treat as name
            if len(parts[1].replace('.','')) <= 2:
                name_part = target  # "Patricia L Annis"
                location_part = ""
            else:
                # "First Last City" - ambiguous, treat all as name
                name_part = target
                location_part = ""
        else:
            # 4+ words without state - first 2-3 as name, rest as location
            # Check if part[2] looks like a middle initial
            if len(parts[1].replace('.','')) <= 2:
                # "First MI Last City..." format
                name_part = " ".join(parts[:3])
                location_part = " ".join(parts[3:])
            else:
                name_part = " ".join(parts[:2])
                location_part = " ".join(parts[2:])

    # Parse name components
    name_words = name_part.split()
    wp_first = name_words[0] if name_words else ""
    wp_last = name_words[-1] if len(name_words) > 1 else ""
    wp_middle = name_words[1] if len(name_words) == 3 else ""

    # Parse location components  
    loc_words = location_part.split() if location_part else []
    wp_state = loc_words[-1].upper() if loc_words else ""
    wp_city = " ".join(loc_words[:-1]) if len(loc_words) > 1 else loc_words[0] if loc_words else ""

    name_plus = name_part.replace(" ", "+")
    loc_plus = location_part.replace(" ", "+")
    name_url = name_part.replace(" ", "-").lower()

    lines = []
    lines.append(f"TARGET:   {name_part}")
    if location_part:
        lines.append(f"LOCATION: {location_part}")
    lines.append("")

    lines.append("=" * 50)
    lines.append("PEOPLE FINDER SITES")
    lines.append("=" * 50)
    lines.append("")

    # Only free sources that return real data without paywalls
    sites = [
        ("TRUEPEOPLESEARCH",  f"https://www.truepeoplesearch.com/results?name={name_plus}&citystatezip={loc_plus}"),
        ("FASTPEOPLESEARCH",  f"https://www.fastpeoplesearch.com/name/{name_url}"),
        ("WHITEPAGES (free)", f"https://www.whitepages.com/name/{wp_first}-{wp_last}/{wp_city}-{wp_state}"),
        ("USPHONEBOOK",       f"https://www.usphonebook.com/{wp_first}-{wp_last}"),
        ("CHECKPEOPLE",       f"https://checkpeople.com/search?firstName={wp_first}&lastName={wp_last}&state={wp_state}"),
        ("RADARIS",           f"https://radaris.com/p/{wp_first}-{wp_last}/"),
        ("VOTERRECORDS",      f"https://voterrecords.com/voters/{name_url}/1"),
        ("CLUSTRMAPS",        f"https://clustrmaps.com/person/{wp_last}-{wp_first}/"),
        ("PUBLICRECORDS.ONLINE", f"https://publicrecords.online/search/?first_name={wp_first}&last_name={wp_last}&state={wp_state}"),
        ("SEARCHPEOPLEFREE",  f"https://www.searchpeoplefree.com/find/{wp_first}-{wp_last}"),
        ("NUWBER",            f"https://nuwber.com/search?firstName={wp_first}&lastName={wp_last}&city={wp_city}&state={wp_state}"),
        ("ADDRESSES.COM",     f"https://www.addresses.com/people/{wp_first}+{wp_last}/{wp_state}/"),
    ]

    for name, url in sites:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("SOCIAL MEDIA")
    lines.append("=" * 50)
    lines.append("")

    social = [
        ("LinkedIn",  f"https://www.linkedin.com/search/results/people/?keywords={name_plus}"),
        ("Facebook",  f"https://www.facebook.com/search/people/?q={name_plus}"),
        ("Twitter/X", f"https://twitter.com/search?q=%22{name_plus}%22&f=user"),
        ("Instagram", f"https://www.instagram.com/explore/search/keyword/?q={name_plus}"),
        ("TikTok",    f"https://www.tiktok.com/search?q={name_plus}"),
        ("YouTube",   f"https://www.youtube.com/results?search_query={name_plus}"),
        ("Reddit",    f"https://www.reddit.com/search/?q=%22{name_part}%22&type=user"),
    ]
    for platform, url in social:
        lines.append(f"[{platform}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("COURT & PUBLIC RECORDS")
    lines.append("=" * 50)
    lines.append("")

    courts = [
        ("PACER Federal Courts",    "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("OpenSanctions Watchlist", f"https://www.opensanctions.org/search/?q={name_plus}"),
        ("NM Courts (CourtLook)",   f"https://caselookup.nmcourts.gov/caselookup/app"),
        ("VINE Offender Search",    f"https://vinelink.vineapps.com/search/NM/Person"),
    ]
    for name, url in courts:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("GOOGLE DORKS")
    lines.append("=" * 50)
    lines.append("")

    dorks = [
        f'"{name_part}" "{location_part}"',
        f'"{name_part}" address phone',
        f'"{name_part}" site:whitepages.com',
        f'"{name_part}" site:spokeo.com',
        f'"{name_part}" site:linkedin.com',
        f'"{name_part}" arrest OR mugshot',
        f'"{name_part}" court OR lawsuit OR case',
        f'"{name_part}" resume OR CV',
        f'"{name_part}" obituary',
        f'"{name_part}" email OR contact',
    ]
    for dork in dorks:
        encoded = dork.replace(" ", "+").replace('"', '%22')
        lines.append(f"  {dork}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    # Live sanctions check
    try:
        san_out, _, _ = run_cmd(
            f"curl -s 'https://api.opensanctions.org/search/default?q={name_plus}&schema=Person' 2>/dev/null",
            timeout=10
        )
        san_data = json.loads(san_out)
        results = san_data.get("results", [])
        lines.append("=" * 50)
        lines.append("LIVE SANCTIONS / WATCHLIST CHECK")
        lines.append("=" * 50)
        lines.append("")
        if results:
            lines.append(f"⚠ WARNING: {len(results)} MATCH(ES) FOUND ON WATCHLISTS")
            for r in results[:5]:
                lines.append(f"  • {r.get('caption','?')} — {r.get('schema','?')} — Score: {r.get('score','?')}")
        else:
            lines.append("✓ No matches found on sanctions/watchlists")
        lines.append("")
    except:
        pass

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "people", "result": result})
    return result

def module_whois(target, job_id):
    emit(job_id, "module_start", {"module": "whois"})
    api_key = os.environ.get("WHOIS_API_KEY", "at_free")
    try:
        out, _, _ = run_cmd(
            f"curl -s 'https://www.whoisxmlapi.com/whoisserver/WhoisService?apiKey={api_key}&domainName={target}&outputFormat=JSON' 2>/dev/null"
        )
        data = json.loads(out)
        record = data.get("WhoisRecord", {})
        registrant = record.get("registrant", {})
        lines = [
            f"Domain:      {record.get('domainName', target)}",
            f"Registrar:   {record.get('registrarName', 'N/A')}",
            f"Created:     {record.get('createdDate', 'N/A')}",
            f"Expires:     {record.get('expiresDate', 'N/A')}",
            f"Updated:     {record.get('updatedDate', 'N/A')}",
            f"Status:      {record.get('status', 'N/A')}",
            f"Registrant:  {registrant.get('organization', registrant.get('name', 'N/A'))}",
            f"Country:     {registrant.get('country', 'N/A')}",
        ]
        nameservers = record.get("nameServers", {}).get("hostNames", [])
        if nameservers:
            lines.append(f"Nameservers: {', '.join(nameservers[:4])}")
        result = "\n".join(lines)
    except:
        out, err, _ = run_cmd(f"whois {target} 2>/dev/null | head -40")
        result = out if out else f"WHOIS lookup failed for {target}"
    emit(job_id, "module_done", {"module": "whois", "result": result})
    return result

def module_dns(target, job_id):
    emit(job_id, "module_start", {"module": "dns"})
    lines = []
    for rtype in ["A", "AAAA", "MX", "NS", "TXT", "CNAME"]:
        out, _, _ = run_cmd(f"dig +short {rtype} {target} 2>/dev/null")
        if out:
            lines.append(f"[{rtype}] {out}")
    result = "\n".join(lines) if lines else "No DNS records found."
    emit(job_id, "module_done", {"module": "dns", "result": result})
    return result

def module_nmap(target, job_id):
    emit(job_id, "module_start", {"module": "nmap"})
    common_ports = {
        21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS",
        80: "HTTP", 110: "POP3", 143: "IMAP", 443: "HTTPS", 445: "SMB",
        3306: "MySQL", 3389: "RDP", 5432: "PostgreSQL", 6379: "Redis",
        8080: "HTTP-Alt", 8443: "HTTPS-Alt", 27017: "MongoDB"
    }
    try:
        ip = socket.gethostbyname(target)
        open_ports = []
        for port, service in common_ports.items():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                if sock.connect_ex((ip, port)) == 0:
                    open_ports.append(f"  {port}/tcp  OPEN  {service}")
                sock.close()
            except:
                pass
        result = f"Host: {target} ({ip})\n\n" + ("\n".join(open_ports) if open_ports else "No common ports open.")
    except Exception as e:
        result = f"Port scan failed: {str(e)}"
    emit(job_id, "module_done", {"module": "nmap", "result": result})
    return result

def module_phone(target, job_id):
    emit(job_id, "module_start", {"module": "phone"})
    # Clean phone number - remove formatting
    clean = target.replace("-","").replace("(","").replace(")","").replace(" ","").replace("+1","").strip()
    formatted = f"({clean[:3]}) {clean[3:6]}-{clean[6:]}" if len(clean) == 10 else target
    phone_plus1 = f"+1{clean}" if len(clean) == 10 else target

    lines = []
    lines.append(f"TARGET:    {formatted}")
    lines.append(f"CLEANED:   {clean}")
    lines.append("")

    # Live carrier/spam lookup via IPQualityScore (free, no key needed for basic)
    try:
        out, _, _ = run_cmd(
            f"curl -s 'https://www.ipqualityscore.com/api/json/phone/YOUR_KEY/{clean}' 2>/dev/null",
            timeout=10
        )
        # Even without API key, try numverify free tier
        nv_out, _, _ = run_cmd(
            f"curl -s 'http://apilayer.net/api/validate?access_key=free&number={clean}&country_code=US&format=1' 2>/dev/null",
            timeout=10
        )
        try:
            nv_data = json.loads(nv_out)
            if nv_data.get("valid"):
                lines.append("=== CARRIER INTELLIGENCE ===")
                lines.append(f"Valid:        {nv_data.get('valid', 'N/A')}")
                lines.append(f"Line Type:    {nv_data.get('line_type', 'N/A')}")
                lines.append(f"Carrier:      {nv_data.get('carrier', 'N/A')}")
                lines.append(f"Location:     {nv_data.get('location', 'N/A')}")
                lines.append(f"Country:      {nv_data.get('country_name', 'N/A')}")
                lines.append("")
        except:
            pass
    except:
        pass

    lines.append("=" * 50)
    lines.append("REVERSE LOOKUP SITES — CLICK TO SEARCH")
    lines.append("=" * 50)
    lines.append("")

    sites = [
        ("TRUEPEOPLESEARCH", f"https://www.truepeoplesearch.com/results?phoneno={clean}"),
        ("WHITEPAGES",       f"https://www.whitepages.com/phone/{formatted.replace(' ','-').replace('(','').replace(')','').replace(' ','-')}"),
        ("SPOKEO",           f"https://www.spokeo.com/phone-search/{clean}"),
        ("BEENVERIFIED",     f"https://www.beenverified.com/phone/{clean}/"),
        ("INTELIUS",         f"https://intelius.com/phone-lookup/{clean}/"),
        ("PEOPLEFINDERS",    f"https://www.peoplefinders.com/phone/{clean}"),
        ("USPHONEBOOK",      f"https://www.usphonebook.com/{clean}"),
        ("411.COM",          f"https://www.411.com/phone/{clean}"),
        ("CALLERMART",       f"https://www.callermart.com/phone/{clean}"),
        ("ZLOOKUP",          f"https://www.zlookup.com/"),
    ]
    for name, url in sites:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("SPAM & REPORT DATABASES")
    lines.append("=" * 50)
    lines.append("")

    spam_sites = [
        ("800NOTES",      f"https://800notes.com/Phone.aspx/{clean}"),
        ("CALLERCENTER",  f"https://callercenter.com/{clean}"),
        ("WHOCALLEDUS",   f"https://whocalledus.com/calls/{clean}/"),
        ("CALLERCOMMENTS",f"https://callercomments.com/calls/{clean}/"),
        ("NOMOROBO",      f"https://www.nomorobo.com/lookup/{clean}"),
        ("SPAMCALLS",     f"https://spamcalls.net/en/search?n={clean}"),
    ]
    for name, url in spam_sites:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("SOCIAL MEDIA PHONE SEARCH")
    lines.append("=" * 50)
    lines.append("")

    social = [
        ("Facebook",  f"https://www.facebook.com/search/people/?q={clean}"),
        ("TrueCaller", f"https://www.truecaller.com/search/us/{clean}"),
        ("Telegram",  f"https://t.me/+1{clean}"),
    ]
    for name, url in social:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("GOOGLE DORKS")
    lines.append("=" * 50)
    lines.append("")

    dorks = [
        f'"{formatted}"',
        f'"{clean}"',
        f'"{phone_plus1}"',
        f'"{formatted}" name address',
        f'"{clean}" site:facebook.com',
        f'"{clean}" site:linkedin.com',
        f'"{formatted}" site:whitepages.com',
        f'"{clean}" spam OR scam OR fraud',
    ]
    for dork in dorks:
        encoded = dork.replace(" ", "+").replace('"', '%22')
        lines.append(f"  {dork}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "phone", "result": result})
    return result






def module_business(target, job_id):
    emit(job_id, "module_start", {"module": "business"})
    
    name_plus = target.replace(" ", "+")
    name_url = target.replace(" ", "-").lower()
    
    lines = []
    lines.append(f"TARGET: {target}")
    lines.append("")

    # ── Live OpenCorporates API (free) ────────────────────────────────────────
    try:
        out, _, _ = run_cmd(
            f"curl -s 'https://api.opencorporates.com/v0.4/companies/search?q={name_plus}&jurisdiction_code=us_nm&format=json' 2>/dev/null",
            timeout=10
        )
        data = json.loads(out)
        companies = data.get("results", {}).get("companies", [])
        if companies:
            lines.append("=" * 50)
            lines.append("OPENCORPORATES — NEW MEXICO RESULTS")
            lines.append("=" * 50)
            lines.append("")
            for c in companies[:5]:
                co = c.get("company", {})
                lines.append(f"  Name:       {co.get('name', 'N/A')}")
                lines.append(f"  Status:     {co.get('current_status', 'N/A')}")
                lines.append(f"  Type:       {co.get('company_type', 'N/A')}")
                lines.append(f"  Registered: {co.get('incorporation_date', 'N/A')}")
                lines.append(f"  Number:     {co.get('company_number', 'N/A')}")
                lines.append(f"  URL:        {co.get('opencorporates_url', 'N/A')}")
                lines.append("")
        else:
            lines.append("No NM results from OpenCorporates.")
            lines.append("")
    except Exception as e:
        lines.append(f"OpenCorporates: {str(e)}")
        lines.append("")

    # ── Secretary of State Lookups ────────────────────────────────────────────
    lines.append("=" * 50)
    lines.append("SECRETARY OF STATE — DIRECT SEARCHES")
    lines.append("=" * 50)
    lines.append("")

    sos_sites = [
        ("NM SOS Business Search",      f"https://portal.sos.state.nm.us/BFS/online/CorporationFormation/SearchBusinesses?SearchCriteria={name_plus}"),
        ("NM SOS (alternate)",          f"https://businessportal.sos.nm.gov/"),
        ("TX SOS Business Search",      f"https://mycpa.cpa.state.tx.us/coa/Index.html"),
        ("AZ SOS Business Search",      f"https://ecorp.azcc.gov/BusinessSearch/BusinessSearch?SearchTerm={name_plus}"),
        ("CO SOS Business Search",      f"https://www.sos.state.co.us/biz/BusinessEntityCriteriaExt.do?nameTyp=ENT&masterFileId=&entityName={name_plus}"),
        ("CA SOS Business Search",      f"https://bizfileonline.sos.ca.gov/search/business"),
        ("FL SOS Business Search",      f"https://search.sunbiz.org/Inquiry/CorporationSearch/SearchResults?searchNameOrder={name_plus}"),
        ("NY SOS Business Search",      f"https://apps.dos.ny.gov/publicInquiry/EntitySearch"),
    ]
    for name, url in sos_sites:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    # ── Federal Business Databases ────────────────────────────────────────────
    lines.append("=" * 50)
    lines.append("FEDERAL BUSINESS DATABASES")
    lines.append("=" * 50)
    lines.append("")

    federal = [
        ("SAM.gov (Federal Contractors)",   f"https://sam.gov/search/?keywords={name_plus}&sort=relevanceScore&index=ei&is_active=true&page=1"),
        ("USASpending.gov",                  f"https://www.usaspending.gov/search/?hash="),
        ("SEC EDGAR (Public Companies)",     f"https://www.sec.gov/cgi-bin/browse-edgar?company={name_plus}&CIK=&type=&dateb=&owner=include&count=40&search_text=&action=getcompany"),
        ("FCC License Search",               f"https://wireless2.fcc.gov/UlsApp/UlsSearch/searchLicense.jsp"),
        ("PACER Business Search",            f"https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("BetterBusiness Bureau",            f"https://www.bbb.org/search?find_text={name_plus}&find_loc=Albuquerque%2C+NM"),
        ("OpenCorporates All States",        f"https://opencorporates.com/companies?q={name_plus}&jurisdiction_code=us&utf8=✓"),
    ]
    for name, url in federal:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    # ── Business Intelligence Sites ───────────────────────────────────────────
    lines.append("=" * 50)
    lines.append("BUSINESS INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")

    intel = [
        ("LinkedIn Company",        f"https://www.linkedin.com/search/results/companies/?keywords={name_plus}"),
        ("Dun & Bradstreet",        f"https://www.dnb.com/business-directory/company-search.html#{name_plus}"),
        ("Manta",                   f"https://www.manta.com/mb_{name_url}"),
        ("Yelp Business",           f"https://www.yelp.com/search?find_desc={name_plus}&find_loc=Albuquerque%2C+NM"),
        ("Google Business",         f"https://www.google.com/search?q={name_plus}+Albuquerque+NM+business"),
        ("Glassdoor",               f"https://www.glassdoor.com/Search/results.htm?keyword={name_plus}"),
        ("Indeed Company",          f"https://www.indeed.com/cmp/{name_url}"),
        ("Bizapedia",               f"https://www.bizapedia.com/nm/"),
        ("Corporationwiki",         f"https://www.corporationwiki.com/search/results?term={name_plus}"),
    ]
    for name, url in intel:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    # ── Registered Agent & Officer Search ────────────────────────────────────
    lines.append("=" * 50)
    lines.append("REGISTERED AGENT & OFFICER SEARCH")
    lines.append("=" * 50)
    lines.append("")
    lines.append("To find who owns or runs this business:")
    lines.append("")
    
    officer_searches = [
        ("OpenCorporates Officers",  f"https://opencorporates.com/officers?q={name_plus}&utf8=✓"),
        ("Corporationwiki Network",  f"https://www.corporationwiki.com/search/results?term={name_plus}"),
        ("NM SOS Officer Search",   f"https://portal.sos.state.nm.us/BFS/online/CorporationFormation/SearchBusinesses?SearchCriteria={name_plus}"),
    ]
    for name, url in officer_searches:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    # ── Google Dorks ──────────────────────────────────────────────────────────
    lines.append("=" * 50)
    lines.append("GOOGLE DORKS")
    lines.append("=" * 50)
    lines.append("")

    dorks = [
        f'"{target}" owner OR CEO OR president',
        f'"{target}" registered agent New Mexico',
        f'"{target}" lawsuit OR lawsuit OR litigation',
        f'"{target}" BBB complaint',
        f'"{target}" fraud OR scam OR complaint',
        f'"{target}" annual report',
        f'"{target}" site:linkedin.com',
        f'"{target}" New Mexico corporation',
    ]
    for dork in dorks:
        encoded = dork.replace(" ", "+").replace('"', '%22')
        lines.append(f"  {dork}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    # ── Live DuckDuckGo ───────────────────────────────────────────────────────
    try:
        ddg_out, _, _ = run_cmd(
            f"curl -s 'https://api.duckduckgo.com/?q={name_plus}+company&format=json&no_html=1' 2>/dev/null",
            timeout=10
        )
        ddg_data = json.loads(ddg_out)
        if ddg_data.get("Abstract"):
            lines.append("=" * 50)
            lines.append("PUBLIC BUSINESS SUMMARY")
            lines.append("=" * 50)
            lines.append("")
            lines.append(ddg_data["Abstract"])
            if ddg_data.get("AbstractURL"):
                lines.append(f"Source: {ddg_data['AbstractURL']}")
            lines.append("")
    except:
        pass

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "business", "result": result})
    return result

def module_plate_lookup(target, job_id):
    emit(job_id, "module_start", {"module": "plate_lookup"})
    
    # Clean and parse plate - preserve state if included
    target_clean = target.upper().strip()
    parts = target_clean.split()
    
    # Check if last part is a state abbreviation
    states = ["NM","TX","AZ","CO","CA","FL","NY","IL","OH","GA","NC","MI","PA","WA","OR"]
    if len(parts) >= 2 and parts[-1] in states:
        plate = parts[0].replace("-", "")
        state = parts[-1]
    else:
        plate = target_clean.replace(" ", "").replace("-", "")
        state = "NM"  # default to NM
    
    lines = []
    lines.append(f"TARGET PLATE: {plate}")
    lines.append(f"STATE:        {state}")
    lines.append(f"NOTE: Vehicle record access requires permissible purpose under DPPA.")
    lines.append(f"Law firms qualify for litigation, process serving, and investigations.")
    lines.append("")

    lines.append("=" * 50)
    lines.append("COMMERCIAL DATABASE LOOKUPS")
    lines.append("=" * 50)
    lines.append("")
    
    commercial = [
        ("CLEAR (Thomson Reuters)", "https://clear.thomsonreuters.com/"),
        ("TLO (TransUnion)",        "https://www.tlo.com/"),
        ("IRB Search",              "https://www.irbsearch.com/"),
        ("Tracers",                 "https://www.tracers.com/"),
        ("NMVTIS (vehiclehistory)", "https://www.vehiclehistory.gov/"),
        ("AutoCheck",               f"https://www.autocheck.com/vehiclehistory/search/go?stype=plate&plate={plate}&state={state}"),
        ("VehicleHistory.com",      f"https://www.vehiclehistory.com/license-plate-search?plate={plate}&state={state}"),
    ]
    for name, url in commercial:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("NEW MEXICO SPECIFIC RESOURCES")
    lines.append("=" * 50)
    lines.append("")
    
    nm_resources = [
        ("NM MVD Records Request",     "https://www.mvd.newmexico.gov/"),
        ("NM Courts - Vehicle Cases",  "https://caselookup.nmcourts.gov/caselookup/app"),
        ("NM Public Records Request",  "https://www.nmag.gov/public-records-requests.aspx"),
        ("NM Taxation & Revenue MVD",  "https://www.mvd.newmexico.gov/"),
    ]
    for name, url in nm_resources:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("FREE PUBLIC VEHICLE RESOURCES")
    lines.append("=" * 50)
    lines.append("")

    free_resources = [
        ("VINCheck (NICB stolen)",     f"https://www.nicb.org/vincheck"),
        ("NHTSA VIN Decoder",          f"https://vpic.nhtsa.dot.gov/decoder/"),
        ("NMVTIS Vehicle History",     f"https://www.vehiclehistory.gov/"),
        ("RecallsByVIN",               f"https://www.nhtsa.gov/recalls"),
        ("Plate Search (freecarvin)",  f"https://www.freecarvin.com/"),
        ("VehicleHistory",             f"https://www.vehiclehistory.com/license-plate-search?plate={plate}&state=NM"),
    ]
    for name, url in free_resources:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("GOOGLE DORKS FOR PLATE")
    lines.append("=" * 50)
    lines.append("")

    dorks = [
        f'"{plate}" New Mexico vehicle',
        f'"{plate}" NM license plate',
        f'"{plate}" site:facebook.com',
        f'"{plate}" site:instagram.com',
        f'"{plate}" accident OR crash OR incident',
        f'"{plate}" arrest OR citation OR ticket',
    ]
    for dork in dorks:
        encoded = dork.replace(" ", "+").replace('"', '%22')
        lines.append(f"  {dork}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("SOCIAL MEDIA PLATE SEARCH")
    lines.append("=" * 50)
    lines.append("")
    lines.append("People sometimes post photos showing their plates on social media.")
    lines.append("")
    
    social = [
        ("Facebook",   f"https://www.facebook.com/search/posts/?q={plate}"),
        ("Instagram",  f"https://www.instagram.com/explore/search/keyword/?q={plate}"),
        ("Twitter/X",  f"https://twitter.com/search?q=%22{plate}%22&f=live"),
        ("Reddit",     f"https://www.reddit.com/search/?q=%22{plate}%22"),
    ]
    for name, url in social:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("DPPA PERMISSIBLE PURPOSES (Law Firm)")
    lines.append("=" * 50)
    lines.append("")
    lines.append("Your firm qualifies under DPPA for:")
    lines.append("  • Use in litigation or investigation in anticipation of litigation")
    lines.append("  • Service of process")
    lines.append("  • Research by licensed private investigators")
    lines.append("  • Insurance claims investigation")
    lines.append("  • Locating missing persons or witnesses")
    lines.append("")
    lines.append("When requesting records, cite: 18 U.S.C. § 2721(b)")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "plate_lookup", "result": result})
    return result

def module_image_metadata(target, job_id):
    """
    Image metadata extraction - target should be a URL to an image
    or the module is triggered with image data from frontend
    """
    emit(job_id, "module_start", {"module": "image_metadata"})
    lines = []
    lines.append(f"TARGET: {target}")
    lines.append("")

    # Try to fetch and analyze image from URL
    if target.startswith("http"):
        try:
            # Download image to temp file
            out, err, rc = run_cmd(
                f"curl -s -L -o /tmp/sentinel_img.jpg '{target}' 2>/dev/null",
                timeout=15
            )
            
            # Extract EXIF with exiftool if available
            exif_out, _, rc = run_cmd("exiftool /tmp/sentinel_img.jpg 2>/dev/null", timeout=10)
            
            if exif_out and rc == 0:
                lines.append("=" * 50)
                lines.append("EXIF METADATA EXTRACTED")
                lines.append("=" * 50)
                lines.append("")
                
                # Parse key fields
                important_fields = [
                    "GPS Latitude", "GPS Longitude", "GPS Position",
                    "GPS Altitude", "GPS Date/Time",
                    "Create Date", "Date/Time Original", "Modify Date",
                    "Make", "Camera Model Name", "Software",
                    "Image Width", "Image Height",
                    "File Name", "File Size", "File Type",
                    "Author", "Copyright", "Artist",
                    "City", "State", "Country", "Location",
                    "Subject", "Description", "Comment",
                    "Serial Number", "Lens ID",
                ]
                
                found_gps = False
                gps_lat = ""
                gps_lon = ""
                
                for line in exif_out.split("\n"):
                    for field in important_fields:
                        if field.lower() in line.lower():
                            lines.append(f"  {line.strip()}")
                            if "GPS Latitude" in line and "Ref" not in line:
                                gps_lat = line.split(":")[-1].strip()
                                found_gps = True
                            if "GPS Longitude" in line and "Ref" not in line:
                                gps_lon = line.split(":")[-1].strip()
                
                lines.append("")
                
                if found_gps and gps_lat and gps_lon:
                    lines.append("=" * 50)
                    lines.append("⚠ GPS COORDINATES FOUND!")
                    lines.append("=" * 50)
                    lines.append("")
                    lines.append(f"Latitude:  {gps_lat}")
                    lines.append(f"Longitude: {gps_lon}")
                    lines.append("")
                    lines.append("View on maps:")
                    lines.append(f"  Google Maps: https://www.google.com/maps?q={gps_lat},{gps_lon}")
                    lines.append(f"  Google Street View: https://www.google.com/maps?q=&layer=c&cbll={gps_lat},{gps_lon}")
                    lines.append("")
            else:
                # Try strings command to extract any text metadata
                str_out, _, _ = run_cmd("strings /tmp/sentinel_img.jpg | grep -i 'gps\|location\|date\|camera\|make\|model' 2>/dev/null | head -20")
                if str_out:
                    lines.append("=" * 50)
                    lines.append("TEXT STRINGS FOUND IN IMAGE")
                    lines.append("=" * 50)
                    lines.append(str_out)
                    lines.append("")
                else:
                    lines.append("No metadata found in image.")
                    lines.append("The image may have had metadata stripped.")
                    lines.append("")
                    
        except Exception as e:
            lines.append(f"Image fetch failed: {str(e)}")
            lines.append("")
    
    # Always provide manual analysis tools
    lines.append("=" * 50)
    lines.append("MANUAL IMAGE ANALYSIS TOOLS")
    lines.append("=" * 50)
    lines.append("")
    
    manual_tools = [
        ("Jeffrey EXIF Viewer",     "http://exif.regex.info/exif.cgi"),
        ("ExifTool Online",         "https://exiftool.org/"),
        ("Metadata2Go",             "https://www.metadata2go.com/"),
        ("Google Reverse Image",    f"https://images.google.com/searchbyimage?image_url={target}" if target.startswith("http") else "https://images.google.com/"),
        ("TinEye Reverse Image",    f"https://tineye.com/search?url={target}" if target.startswith("http") else "https://tineye.com/"),
        ("Yandex Reverse Image",    f"https://yandex.com/images/search?url={target}&rpt=imageview" if target.startswith("http") else "https://yandex.com/images/"),
        ("Bing Visual Search",      "https://www.bing.com/images/search?view=detailv2&iss=sbi"),
        ("FotoForensics",           f"https://fotoforensics.com/analysis.php?id=" ),
        ("InVID/WeVerify",          "https://www.invid-project.eu/tools-and-services/invid-verification-plugin/"),
    ]
    for name, url in manual_tools:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("REVERSE IMAGE SEARCH LINKS")
    lines.append("=" * 50)
    lines.append("")
    lines.append("To find where else this image appears online:")
    lines.append("")
    
    if target.startswith("http"):
        rev_searches = [
            ("Google Images",   f"https://images.google.com/searchbyimage?image_url={target}"),
            ("TinEye",          f"https://tineye.com/search?url={target}"),
            ("Yandex",          f"https://yandex.com/images/search?url={target}&rpt=imageview"),
        ]
        for name, url in rev_searches:
            lines.append(f"[{name}]")
            lines.append(f"  {url}")
            lines.append("")
    else:
        lines.append("Paste an image URL in the search box to enable reverse image search links.")
        lines.append("")
        lines.append("Or drag and drop an image at:")
        lines.append("  https://images.google.com/")
        lines.append("  https://tineye.com/")
        lines.append("  https://yandex.com/images/")
        lines.append("")

    lines.append("=" * 50)
    lines.append("HOW TO USE FOR FACEBOOK PHOTOS")
    lines.append("=" * 50)
    lines.append("")
    lines.append("1. Right-click any Facebook photo")
    lines.append("2. Select 'Copy image address'")
    lines.append("3. Paste the URL as your target and re-investigate")
    lines.append("4. OR download the photo and upload to Jeffrey EXIF Viewer")
    lines.append("")
    lines.append("Note: Facebook strips GPS from uploaded photos")
    lines.append("but timestamps and device info often remain.")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "image_metadata", "result": result})
    return result

def module_social_media(target, job_id):
    emit(job_id, "module_start", {"module": "social_media"})
    
    # Parse name and location for social media module
    if "," in target:
        name_quoted = target.split(",")[0].strip()
    else:
        name_quoted = target.strip()
    name_plus = name_quoted.replace(" ", "+")
    parts = name_quoted.split()
    first = parts[0] if parts else target
    last = parts[-1] if len(parts) > 1 else ""
    wp_first_s = first
    wp_last_s = last""
    
    lines = []
    lines.append(f"TARGET: {name_quoted}")
    lines.append("")

    # ── Facebook ─────────────────────────────────────────────────────────────
    lines.append("=" * 50)
    lines.append("FACEBOOK INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    fb_searches = [
        ("People Search",      f"https://www.facebook.com/search/people/?q={name_plus}"),
        ("Posts mentioning",   f"https://www.facebook.com/search/posts/?q={name_plus}"),
        ("Photos tagged",      f"https://www.facebook.com/search/photos/?q={name_plus}"),
        ("Check-ins",          f"https://www.facebook.com/search/places/?q={name_plus}"),
        ("Groups",             f"https://www.facebook.com/search/groups/?q={name_plus}"),
        ("Events",             f"https://www.facebook.com/search/events/?q={name_plus}"),
        ("Marketplace",        f"https://www.facebook.com/marketplace/search/?query={name_plus}"),
    ]
    for label, url in fb_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    # ── Instagram ─────────────────────────────────────────────────────────────
    lines.append("=" * 50)
    lines.append("INSTAGRAM INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    ig_searches = [
        ("Profile Search",     f"https://www.instagram.com/explore/search/keyword/?q={name_plus}"),
        ("Hashtag Search",     f"https://www.instagram.com/explore/tags/{name_plus.replace('+','')}/"),
        ("Google IG Search",   f"https://www.google.com/search?q=site:instagram.com+%22{name_plus}%22"),
    ]
    for label, url in ig_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    # ── Twitter/X ─────────────────────────────────────────────────────────────
    lines.append("=" * 50)
    lines.append("TWITTER/X INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    tw_searches = [
        ("People Search",      f"https://twitter.com/search?q=%22{name_plus}%22&f=user"),
        ("Recent Posts",       f"https://twitter.com/search?q=%22{name_plus}%22&f=live"),
        ("Top Posts",          f"https://twitter.com/search?q=%22{name_plus}%22&f=top"),
        ("With Location",      f"https://twitter.com/search?q=%22{name_plus}%22+near%3A%22Albuquerque%22"),
    ]
    for label, url in tw_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    # ── LinkedIn ──────────────────────────────────────────────────────────────
    lines.append("=" * 50)
    lines.append("LINKEDIN INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    li_searches = [
        ("People Search",      f"https://www.linkedin.com/search/results/people/?keywords={name_plus}"),
        ("Posts Search",       f"https://www.linkedin.com/search/results/content/?keywords={name_plus}"),
        ("Google LI Search",   f"https://www.google.com/search?q=site:linkedin.com/in+%22{name_plus}%22"),
    ]
    for label, url in li_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    # ── TikTok ────────────────────────────────────────────────────────────────
    lines.append("=" * 50)
    lines.append("TIKTOK INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    tt_searches = [
        ("User Search",        f"https://www.tiktok.com/search/user?q={name_plus}"),
        ("Video Search",       f"https://www.tiktok.com/search?q={name_plus}"),
        ("Google TT Search",   f"https://www.google.com/search?q=site:tiktok.com+%22{name_plus}%22"),
    ]
    for label, url in tt_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    # ── YouTube ───────────────────────────────────────────────────────────────
    lines.append("=" * 50)
    lines.append("YOUTUBE INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    yt_searches = [
        ("Channel Search",     f"https://www.youtube.com/results?search_query={name_plus}&sp=EgIQAg%253D%253D"),
        ("Video Search",       f"https://www.youtube.com/results?search_query={name_plus}"),
    ]
    for label, url in yt_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    # ── Reddit ────────────────────────────────────────────────────────────────
    lines.append("=" * 50)
    lines.append("REDDIT INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    rd_searches = [
        ("User Search",        f"https://www.reddit.com/search/?q=%22{name_quoted}%22&type=user"),
        ("Posts Search",       f"https://www.reddit.com/search/?q=%22{name_quoted}%22"),
    ]
    for label, url in rd_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    # ── Snapchat ──────────────────────────────────────────────────────────────
    lines.append("=" * 50)
    lines.append("SNAPCHAT / OTHER PLATFORMS")
    lines.append("=" * 50)
    lines.append("")
    other_searches = [
        ("Snapchat",           f"https://www.snapchat.com/add/{wp_first_s.lower()}{wp_last_s.lower()}"),
        ("Pinterest",          f"https://www.pinterest.com/search/people/?q={name_plus}"),
        ("Tumblr",             f"https://www.tumblr.com/search/{name_plus}"),
        ("Nextdoor",           f"https://nextdoor.com/find-neighbors/"),
        ("Meetup",             f"https://www.meetup.com/find/?keywords={name_plus}"),
        ("Venmo",              f"https://venmo.com/{wp_first_s.lower()}{wp_last_s.lower()}"),
        ("Cash App",           f"https://cash.app/${wp_first_s.lower()}{wp_last_s.lower()}"),
    ]
    for label, url in other_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    # ── Location-Specific Social Searches ────────────────────────────────────
    lines.append("=" * 50)
    lines.append("LOCATION INDICATOR SEARCHES")
    lines.append("=" * 50)
    lines.append("")
    location_searches = [
        ("FB Check-ins NM",    f"https://www.facebook.com/search/places/?q={name_plus}+new+mexico"),
        ("Twitter Near ABQ",   f"https://twitter.com/search?q=%22{name_plus}%22+near%3AAlbuquerque&f=live"),
        ("Google Maps",        f"https://www.google.com/maps/search/{name_plus}"),
        ("Nextdoor NM",        f"https://nextdoor.com/find-neighbors/"),
        ("Yelp Reviews",       f"https://www.yelp.com/search?find_desc={name_plus}"),
    ]
    for label, url in location_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    # ── Live DuckDuckGo check ─────────────────────────────────────────────────
    try:
        ddg_out, _, _ = run_cmd(
            f"curl -s 'https://api.duckduckgo.com/?q={name_plus}+social+media&format=json&no_html=1' 2>/dev/null",
            timeout=10
        )
        ddg_data = json.loads(ddg_out)
        if ddg_data.get("Abstract"):
            lines.append("=" * 50)
            lines.append("PUBLIC PROFILE SUMMARY")
            lines.append("=" * 50)
            lines.append("")
            lines.append(ddg_data["Abstract"])
            if ddg_data.get("AbstractURL"):
                lines.append(f"Source: {ddg_data['AbstractURL']}")
            lines.append("")
    except:
        pass

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "social_media", "result": result})
    return result

def module_email_investigate(target, job_id):
    emit(job_id, "module_start", {"module": "email_investigate"})
    lines = []
    lines.append(f"TARGET EMAIL: {target}")
    lines.append("")

    # Run Holehe if installed
    out, err, rc = run_cmd(f"python3 -m holehe {target} --only-used 2>/dev/null", timeout=120)
    if out and "holehe" not in out.lower() and "error" not in out.lower():
        lines.append("=" * 50)
        lines.append("HOLEHE — ACCOUNT DETECTION (120+ SITES)")
        lines.append("=" * 50)
        lines.append("")
        lines.append(out)
        lines.append("")
    
    # EmailRep live check
    try:
        rep_out, _, _ = run_cmd(
            f"curl -s 'https://emailrep.io/{target}' -H 'User-Agent: fivet-osint' 2>/dev/null",
            timeout=10
        )
        data = json.loads(rep_out)
        details = data.get("details", {})
        lines.append("=" * 50)
        lines.append("EMAIL REPUTATION INTELLIGENCE")
        lines.append("=" * 50)
        lines.append("")
        lines.append(f"Reputation:    {data.get('reputation', 'N/A')}")
        lines.append(f"Suspicious:    {data.get('suspicious', False)}")
        lines.append(f"Blacklisted:   {details.get('blacklisted', False)}")
        lines.append(f"Data Breach:   {details.get('data_breach', False)}")
        lines.append(f"Malicious:     {details.get('malicious_activity', False)}")
        lines.append(f"Disposable:    {details.get('disposable', False)}")
        lines.append(f"Free Provider: {details.get('free_provider', False)}")
        lines.append(f"Profiles:      {', '.join(details.get('profiles', [])) or 'None detected'}")
        lines.append(f"References:    {data.get('references', 0)}")
        lines.append("")
    except:
        pass

    # Extract domain from email for WHOIS
    try:
        domain = target.split("@")[1]
        lines.append("=" * 50)
        lines.append(f"EMAIL DOMAIN INTEL: {domain}")
        lines.append("=" * 50)
        lines.append("")
        
        # DNS for domain
        dns_out, _, _ = run_cmd(f"dig +short A {domain} 2>/dev/null")
        mx_out, _, _ = run_cmd(f"dig +short MX {domain} 2>/dev/null")
        if dns_out:
            lines.append(f"Domain IP:   {dns_out.split()[0] if dns_out else 'N/A'}")
        if mx_out:
            lines.append(f"Mail Server: {mx_out}")
        lines.append("")
    except:
        pass

    lines.append("=" * 50)
    lines.append("DIRECT SEARCH LINKS")
    lines.append("=" * 50)
    lines.append("")

    sites = [
        ("TRUEPEOPLESEARCH", f"https://www.truepeoplesearch.com/results?emailaddress={target}"),
        ("SPOKEO",           f"https://www.spokeo.com/email-search/{target}"),
        ("BEENVERIFIED",     f"https://www.beenverified.com/email/{target}/"),
        ("INTELIUS",         f"https://intelius.com/email-lookup/{target}/"),
        ("EMAILREP",         f"https://emailrep.io/{target}"),
        ("HUNTER.IO",        f"https://hunter.io/email-verifier/{target}"),
        ("HAVEIBEENPWNED",   f"https://haveibeenpwned.com/account/{target}"),
        ("DEHASHED",         f"https://www.dehashed.com/search?query={target}"),
        ("EPIEOS",           f"https://epieos.com/?q={target}&t=email"),
        ("GMAIL LOOKUP",     f"https://www.google.com/search?q=%22{target}%22"),
    ]
    for name, url in sites:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("SOCIAL MEDIA EMAIL SEARCH")
    lines.append("=" * 50)
    lines.append("")

    social = [
        ("Facebook",   f"https://www.facebook.com/search/people/?q={target}"),
        ("LinkedIn",   f"https://www.linkedin.com/search/results/people/?keywords={target}"),
        ("Twitter/X",  f"https://twitter.com/search?q={target}&f=user"),
        ("Gravatar",   f"https://en.gravatar.com/{target}"),
    ]
    for name, url in social:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("GOOGLE DORKS")
    lines.append("=" * 50)
    lines.append("")

    dorks = [
        f'"{target}"',
        f'"{target}" name address phone',
        f'"{target}" site:linkedin.com',
        f'"{target}" site:facebook.com',
        f'"{target}" resume OR CV',
        f'"{target}" site:pastebin.com',
        f'"{target}" leaked OR breach OR hacked',
    ]
    for dork in dorks:
        encoded = dork.replace(" ", "+").replace('"', '%22')
        lines.append(f"  {dork}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "email_investigate", "result": result})
    return result

def module_theharvester(target, job_id):
    emit(job_id, "module_start", {"module": "theharvester"})
    out, err, rc = run_cmd(
        f"python3 -m theHarvester -d {target} -b google,bing,duckduckgo -l 50 2>/dev/null",
        timeout=120
    )
    result = out if out else f"theHarvester: {err or 'No results'}"
    emit(job_id, "module_done", {"module": "theharvester", "result": result})
    return result

def module_sherlock(target, job_id):
    emit(job_id, "module_start", {"module": "sherlock"})
    out, err, rc = run_cmd(
        f"python3 -m sherlock {target} --timeout 8 2>/dev/null",
        timeout=120
    )
    result = out if out else f"Sherlock: {err or 'No results'}"
    emit(job_id, "module_done", {"module": "sherlock", "result": result})
    return result

def module_shodan(target, job_id):
    emit(job_id, "module_start", {"module": "shodan"})
    api_key = os.environ.get("SHODAN_API_KEY", "")
    if not api_key:
        result = "Add SHODAN_API_KEY to Render Environment Variables."
    else:
        try:
            import urllib.request
            ip = socket.gethostbyname(target)
            url = f"https://api.shodan.io/shodan/host/{ip}?key={api_key}"
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read().decode())
            if "error" in data:
                result = f"Shodan: {data['error']}"
            else:
                ports = data.get("ports", [])
                lines = [
                    f"IP:      {ip}",
                    f"Org:     {data.get('org', 'N/A')}",
                    f"ISP:     {data.get('isp', 'N/A')}",
                    f"Country: {data.get('country_name', 'N/A')}",
                    f"City:    {data.get('city', 'N/A')}",
                    f"Ports:   {', '.join(map(str, ports)) or 'None'}",
                    f"Vulns:   {', '.join(data.get('vulns', {}).keys()) or 'None'}",
                ]
                result = "\n".join(lines)
        except Exception as e:
            result = f"Shodan: {str(e)}"
    emit(job_id, "module_done", {"module": "shodan", "result": result})
    return result

def module_subdomains(target, job_id):
    emit(job_id, "module_start", {"module": "subdomains"})
    out, _, _ = run_cmd(
        f"curl -s 'https://crt.sh/?q=%.{target}&output=json' 2>/dev/null | "
        f"python3 -c \"import sys,json; data=json.load(sys.stdin); "
        f"[print(e['name_value']) for e in data]\" 2>/dev/null | sort -u | head -40"
    )
    result = out if out else "No subdomains found via crt.sh."
    emit(job_id, "module_done", {"module": "subdomains", "result": result})
    return result

def module_geoip(target, job_id):
    emit(job_id, "module_start", {"module": "geoip"})
    try:
        ip = socket.gethostbyname(target)
        out, _, _ = run_cmd(f"curl -s 'https://ipapi.co/{ip}/json/' 2>/dev/null")
        data = json.loads(out)
        lines = [
            f"IP:       {data.get('ip', ip)}",
            f"City:     {data.get('city', 'N/A')}",
            f"Region:   {data.get('region', 'N/A')}",
            f"Country:  {data.get('country_name', 'N/A')}",
            f"Org/ISP:  {data.get('org', 'N/A')}",
            f"Timezone: {data.get('timezone', 'N/A')}",
            f"Lat/Lon:  {data.get('latitude', 'N/A')}, {data.get('longitude', 'N/A')}",
        ]
        result = "\n".join(lines)
    except Exception as e:
        result = f"GeoIP lookup failed: {str(e)}"
    emit(job_id, "module_done", {"module": "geoip", "result": result})
    return result

def module_virustotal(target, job_id):
    emit(job_id, "module_start", {"module": "virustotal"})
    api_key = os.environ.get("VT_API_KEY", "")
    if not api_key:
        result = "Add VT_API_KEY to Render Environment Variables."
    else:
        out, _, _ = run_cmd(
            f"curl -s --request GET "
            f"--url 'https://www.virustotal.com/api/v3/domains/{target}' "
            f"--header 'x-apikey: {api_key}' 2>/dev/null"
        )
        try:
            data = json.loads(out)
            attrs = data.get("data", {}).get("attributes", {})
            stats = attrs.get("last_analysis_stats", {})
            cats = attrs.get("categories", {})
            result = (
                f"Malicious:   {stats.get('malicious', 0)}\n"
                f"Suspicious:  {stats.get('suspicious', 0)}\n"
                f"Harmless:    {stats.get('harmless', 0)}\n"
                f"Undetected:  {stats.get('undetected', 0)}\n"
                f"Reputation:  {attrs.get('reputation', 'N/A')}\n"
                f"Categories:  {', '.join(set(cats.values())) if cats else 'N/A'}"
            )
        except:
            result = out[:500] if out else "VirusTotal lookup failed."
    emit(job_id, "module_done", {"module": "virustotal", "result": result})
    return result

def module_emailrep(target, job_id):
    emit(job_id, "module_start", {"module": "emailrep"})
    out, _, _ = run_cmd(
        f"curl -s 'https://emailrep.io/{target}' -H 'User-Agent: fivet-osint' 2>/dev/null"
    )
    try:
        data = json.loads(out)
        details = data.get("details", {})
        lines = [
            f"Email:         {data.get('email', target)}",
            f"Reputation:    {data.get('reputation', 'N/A')}",
            f"Suspicious:    {data.get('suspicious', 'N/A')}",
            f"References:    {data.get('references', 'N/A')}",
            f"Blacklisted:   {details.get('blacklisted', False)}",
            f"Data breach:   {details.get('data_breach', False)}",
            f"Disposable:    {details.get('disposable', False)}",
            f"Free provider: {details.get('free_provider', False)}",
            f"Profiles:      {', '.join(details.get('profiles', [])) or 'None found'}",
        ]
        result = "\n".join(lines)
    except:
        result = out[:500] if out else "EmailRep lookup failed."
    emit(job_id, "module_done", {"module": "emailrep", "result": result})
    return result

def module_haveibeenpwned(target, job_id):
    emit(job_id, "module_start", {"module": "hibp"})
    api_key = os.environ.get("HIBP_API_KEY", "")
    if not api_key:
        result = "Add HIBP_API_KEY to Render Environment Variables.\nKey at https://haveibeenpwned.com/API/Key"
    else:
        out, _, _ = run_cmd(
            f"curl -s 'https://haveibeenpwned.com/api/v3/breachedaccount/{target}' "
            f"-H 'hibp-api-key: {api_key}' -H 'User-Agent: fivet-osint' 2>/dev/null"
        )
        try:
            data = json.loads(out)
            if isinstance(data, list):
                result = f"Found in {len(data)} breach(es):\n"
                result += "\n".join(f"  - {b.get('Name','?')} ({b.get('BreachDate','?')})" for b in data[:20])
            else:
                result = "No breaches found."
        except:
            result = "No breaches found or API error."
    emit(job_id, "module_done", {"module": "hibp", "result": result})
    return result

def module_metadata(target, job_id):
    emit(job_id, "module_start", {"module": "metadata"})
    query = target.replace(" ", "+")
    out, _, _ = run_cmd(
        f"curl -s 'https://api.duckduckgo.com/?q={query}&format=json&no_html=1' 2>/dev/null"
    )
    try:
        data = json.loads(out)
        lines = []
        if data.get("Abstract"):
            lines.append(f"Summary: {data['Abstract']}")
        if data.get("AbstractSource"):
            lines.append(f"Source: {data['AbstractSource']} — {data.get('AbstractURL','')}")
        for r in data.get("RelatedTopics", [])[:8]:
            if isinstance(r, dict) and r.get("Text"):
                lines.append(f"• {r['Text'][:120]}")
        result = "\n".join(lines) if lines else "No public metadata found."
    except:
        result = "Metadata lookup failed."
    emit(job_id, "module_done", {"module": "metadata", "result": result})
    return result

def module_google_dorks(target, job_id):
    emit(job_id, "module_start", {"module": "dorks"})
    dorks = [
        f'site:linkedin.com "{target}"',
        f'site:twitter.com "{target}"',
        f'site:facebook.com "{target}"',
        f'"{target}" filetype:pdf',
        f'"{target}" inurl:cv OR inurl:resume',
        f'"{target}" site:pastebin.com',
        f'"{target}" site:github.com',
        f'"{target}" site:reddit.com',
        f'"{target}" site:instagram.com',
        f'"{target}" site:youtube.com',
    ]
    result = "Google Dork Queries (copy into Google):\n\n"
    result += "\n".join(f"  {d}" for d in dorks)
    result += "\n\nDirect links:\n"
    for d in dorks:
        encoded = d.replace(" ", "+").replace('"', '%22')
        result += f"  https://www.google.com/search?q={encoded}\n"
    emit(job_id, "module_done", {"module": "dorks", "result": result})
    return result


def module_property(target, job_id):
    emit(job_id, "module_start", {"module": "property"})
    
    if "," in target:
        name_part = target.split(",")[0].strip()
        location_part = target.split(",")[1].strip()
    else:
        parts = target.split()
        name_part = " ".join(parts[:2]) if len(parts) >= 2 else target
        location_part = " ".join(parts[2:]) if len(parts) > 2 else ""
    
    name_plus = name_part.replace(" ", "+")
    wp_first = name_part.split()[0] if name_part else ""
    wp_last = name_part.split()[-1] if len(name_part.split()) > 1 else ""

    lines = []
    lines.append(f"TARGET: {name_part}")
    if location_part:
        lines.append(f"LOCATION: {location_part}")
    lines.append("")

    lines.append("=" * 50)
    lines.append("NEW MEXICO PROPERTY RECORDS")
    lines.append("=" * 50)
    lines.append("")

    nm_counties = [
        ("Bernalillo County Assessor",  "https://assessor.bernco.gov/public.access/search/commonsearch.aspx?mode=owner"),
        ("Sandoval County Assessor",    "https://www.sandovalcountynm.gov/assessor/property-search/"),
        ("Santa Fe County Assessor",    "https://www.santafecountynm.gov/assessor"),
        ("Valencia County Assessor",    "https://www.co.valencia.nm.us/assessor"),
        ("Dona Ana County Assessor",    "https://assessor.donaanacounty.org/"),
        ("Chavez County Assessor",      "https://www.chaves.nm.us/departments/assessor"),
        ("NM All Counties",             "https://www.nmcourts.gov/"),
    ]
    for name, url in nm_counties:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("NATIONAL PROPERTY DATABASES")
    lines.append("=" * 50)
    lines.append("")

    national = [
        ("NETR Online (All 50 States)",  "https://publicrecords.netronline.com/"),
        ("PropWire (Free Owner Search)", f"https://propwire.com/search?q={name_plus}"),
        ("Zillow Owner Search",          f"https://www.zillow.com/homes/{name_plus}_rb/"),
        ("Realtor.com",                  f"https://www.realtor.com/realestateandhomes-search/{location_part.replace(' ','-')}/"),
        ("PropertyShark",               f"https://www.propertyshark.com/mason/property-search/us/"),
        ("Attom Data",                   "https://www.attomdata.com/"),
        ("County Office",               f"https://www.countyoffice.org/property-records-search/?q={name_plus}"),
        ("Black Knight (Parcel)",        "https://bkiconnect.com/"),
        ("FamilyTreeNow (Address Hist)", f"https://www.familytreenow.com/search/people/results?first={wp_first}&last={wp_last}"),
    ]
    for name, url in national:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("TAX & LIENS")
    lines.append("=" * 50)
    lines.append("")

    tax = [
        ("NM Taxation & Revenue",        "https://tap.state.nm.us/tap/_/"),
        ("Federal Tax Liens (PACER)",    "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("UCC Filings NM",               f"https://portal.sos.state.nm.us/BFS/online/UCCFilings/SearchUCC"),
        ("Bankruptcy Search",            f"https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
    ]
    for name, url in tax:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("GOOGLE DORKS FOR PROPERTY")
    lines.append("=" * 50)
    lines.append("")

    dorks = [
        f'"{name_part}" property owner New Mexico',
        f'"{name_part}" real estate deed',
        f'"{name_part}" assessor parcel',
        f'"{name_part}" foreclosure lien',
    ]
    for dork in dorks:
        encoded = dork.replace(" ", "+").replace('"', '%22')
        lines.append(f"  {dork}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "property", "result": result})
    return result


def module_photo_forensics(target, job_id):
    emit(job_id, "module_start", {"module": "photo_forensics"})
    lines = []
    lines.append(f"TARGET: {target}")
    lines.append("")

    is_url = target.startswith("http")

    lines.append("=" * 50)
    lines.append("REVERSE IMAGE SEARCH")
    lines.append("=" * 50)
    lines.append("")

    if is_url:
        rev = [
            ("Google Reverse Image",   f"https://images.google.com/searchbyimage?image_url={target}"),
            ("TinEye",                 f"https://tineye.com/search?url={target}"),
            ("Yandex (Best for faces)",f"https://yandex.com/images/search?url={target}&rpt=imageview"),
            ("Bing Visual Search",     f"https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:{target}"),
            ("Lenso.ai (Face Search)", f"https://lenso.ai/en?url={target}"),
            ("PimEyes (Face Search)",  f"https://pimeyes.com/en"),
        ]
    else:
        rev = [
            ("Google Reverse Image",   "https://images.google.com/"),
            ("TinEye",                 "https://tineye.com/"),
            ("Yandex (Best for faces)","https://yandex.com/images/"),
            ("Bing Visual Search",     "https://www.bing.com/images/search?view=detailv2&iss=sbi"),
            ("Lenso.ai (Face Search)", "https://lenso.ai/en"),
            ("PimEyes (Face Search)",  "https://pimeyes.com/en"),
        ]
    for name, url in rev:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("PHOTO METADATA EXTRACTION")
    lines.append("=" * 50)
    lines.append("")

    meta = [
        ("Jeffrey EXIF Viewer",     "http://exif.regex.info/exif.cgi"),
        ("ExifTool Online",         "https://exiftool.org/"),
        ("Metadata2Go",             "https://www.metadata2go.com/"),
        ("FotoForensics (manipulation)", "https://fotoforensics.com/"),
        ("Forensically (clone detection)", "https://29a.ch/photo-forensics/"),
        ("ImageEdited (edit detect)", "https://imageedited.com/"),
    ]
    for name, url in meta:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("VIDEO FORENSICS")
    lines.append("=" * 50)
    lines.append("")

    video = [
        ("InVID WeVerify",          "https://www.invid-project.eu/tools-and-services/invid-verification-plugin/"),
        ("YouTube DataViewer",      "https://citizenevidence.amnestyusa.org/"),
        ("TrueMedia.org",           "https://www.truemedia.org/"),
    ]
    for name, url in video:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("GEOLOCATION FROM PHOTOS")
    lines.append("=" * 50)
    lines.append("")

    geo = [
        ("SunCalc (shadow/time analysis)", "https://www.suncalc.org/"),
        ("SunEarthTools",                  "https://www.sunearthtools.com/dp/tools/pos_sun.php"),
        ("Google Maps Street View",        "https://www.google.com/maps"),
        ("Bing Maps Bird's Eye",           "https://www.bing.com/maps"),
        ("Google Earth Web",               "https://earth.google.com/web/"),
        ("Overpass Turbo (OpenStreetMap)", "https://overpass-turbo.eu/"),
        ("What3Words",                     "https://what3words.com/"),
        ("GeoHack",                        "https://geohack.toolforge.org/"),
    ]
    for name, url in geo:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    if is_url:
        lines.append("=" * 50)
        lines.append("AUTOMATED METADATA EXTRACTION")
        lines.append("=" * 50)
        lines.append("")
        try:
            out, _, _ = run_cmd(
                f"curl -s -L -o /tmp/fivet_img.jpg \"{target}\" 2>/dev/null && exiftool /tmp/fivet_img.jpg 2>/dev/null | head -40",
                timeout=15
            )
            if out:
                lines.append(out)
            else:
                lines.append("No metadata extracted — image may have metadata stripped.")
        except:
            lines.append("Could not fetch image for automated extraction.")
        lines.append("")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "photo_forensics", "result": result})
    return result


def module_geolocation(target, job_id):
    emit(job_id, "module_start", {"module": "geolocation"})
    lines = []
    lines.append(f"TARGET: {target}")
    lines.append("")

    loc_plus = target.replace(" ", "+")

    lines.append("=" * 50)
    lines.append("MAP INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")

    maps = [
        ("Google Maps",             f"https://www.google.com/maps/search/{loc_plus}"),
        ("Google Street View",      f"https://www.google.com/maps?q={loc_plus}&layer=c"),
        ("Google Earth Web",        f"https://earth.google.com/web/search/{loc_plus}"),
        ("Bing Maps",               f"https://www.bing.com/maps?q={loc_plus}"),
        ("OpenStreetMap",           f"https://www.openstreetmap.org/search?query={loc_plus}"),
        ("Apple Maps",              f"https://maps.apple.com/?q={loc_plus}"),
        ("Waze",                    f"https://www.waze.com/en/live-map/directions?to=ll.{loc_plus}"),
    ]
    for name, url in maps:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("SATELLITE & HISTORICAL IMAGERY")
    lines.append("=" * 50)
    lines.append("")

    satellite = [
        ("Google Earth Historical",  f"https://earth.google.com/web/search/{loc_plus}"),
        ("Sentinel Hub (Satellite)", "https://www.sentinel-hub.com/explore/eobrowser/"),
        ("USGS EarthExplorer",       "https://earthexplorer.usgs.gov/"),
        ("NASA Worldview",           "https://worldview.earthdata.nasa.gov/"),
        ("Bing Birds Eye",           f"https://www.bing.com/maps?q={loc_plus}&style=b"),
    ]
    for name, url in satellite:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("SPECIALIZED LOCATION TOOLS")
    lines.append("=" * 50)
    lines.append("")

    special = [
        ("Wigle.net (WiFi Networks)",    f"https://wigle.net/search#fullSearch"),
        ("Overpass Turbo",               f"https://overpass-turbo.eu/"),
        ("What3Words",                   f"https://what3words.com/{loc_plus.replace('+','.')}"),
        ("FlightAware (Aircraft)",       f"https://flightaware.com/live/airport/{loc_plus}"),
        ("MarineTraffic (Ships)",        f"https://www.marinetraffic.com/en/ais/home/centerx:-87/centery:20/zoom:4"),
        ("SunCalc (Sun Position)",       f"https://www.suncalc.org/"),
        ("CalcMaps (Distance/Area)",     f"https://www.calcmaps.com/map-distance/"),
    ]
    for name, url in special:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    # Live GeoIP if it looks like an IP
    import re
    if re.match(r'^\d+\.\d+\.\d+\.\d+$', target):
        try:
            out, _, _ = run_cmd(f"curl -s 'https://ipapi.co/{target}/json/' 2>/dev/null")
            data = json.loads(out)
            lines.append("=" * 50)
            lines.append("IP GEOLOCATION (LIVE)")
            lines.append("=" * 50)
            lines.append("")
            lines.append(f"IP:       {data.get('ip', target)}")
            lines.append(f"City:     {data.get('city', 'N/A')}")
            lines.append(f"Region:   {data.get('region', 'N/A')}")
            lines.append(f"Country:  {data.get('country_name', 'N/A')}")
            lines.append(f"Org/ISP:  {data.get('org', 'N/A')}")
            lines.append(f"Lat/Lon:  {data.get('latitude', 'N/A')}, {data.get('longitude', 'N/A')}")
            lat = data.get('latitude','')
            lon = data.get('longitude','')
            if lat and lon:
                lines.append(f"Maps:     https://www.google.com/maps?q={lat},{lon}")
            lines.append("")
        except:
            pass

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "geolocation", "result": result})
    return result


def module_username_search(target, job_id):
    emit(job_id, "module_start", {"module": "username_search"})
    lines = []
    lines.append(f"TARGET USERNAME: {target}")
    lines.append("")

    lines.append("=" * 50)
    lines.append("AUTOMATED SCANNER")
    lines.append("=" * 50)
    lines.append("")

    # Run Sherlock
    out, _, _ = run_cmd(f"python3 -m sherlock {target} --timeout 8 2>/dev/null", timeout=120)
    if out and "not found" not in out.lower():
        lines.append("[SHERLOCK — 300+ PLATFORMS]")
        lines.append(out)
        lines.append("")

    # Run Maigret if available
    out2, _, rc2 = run_cmd(f"python3 -m maigret {target} --top-sites 50 2>/dev/null", timeout=120)
    if out2 and rc2 == 0:
        lines.append("[MAIGRET — FULL DOSSIER]")
        lines.append(out2[:2000])
        lines.append("")

    lines.append("=" * 50)
    lines.append("MANUAL USERNAME SEARCH SITES")
    lines.append("=" * 50)
    lines.append("")

    sites = [
        ("WhatsMyName",         f"https://whatsmyname.app/?q={target}"),
        ("Namechk",             f"https://namechk.com/{target}"),
        ("UserSearch.org",      f"https://usersearch.org/results_normal.php?q={target}"),
        ("CheckUsernames",      f"https://checkusernames.com/?q={target}"),
        ("KnowEm",              f"https://knowem.com/checkusernames.php?u={target}"),
        ("Instant Username",    f"https://instantusername.com/#/{target}"),
        ("Sherlock Web",        f"https://sherlock-project.github.io/"),
    ]
    for name, url in sites:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("PLATFORM DIRECT CHECKS")
    lines.append("=" * 50)
    lines.append("")

    platforms = [
        ("Twitter/X",      f"https://twitter.com/{target}"),
        ("Instagram",      f"https://www.instagram.com/{target}/"),
        ("TikTok",         f"https://www.tiktok.com/@{target}"),
        ("YouTube",        f"https://www.youtube.com/@{target}"),
        ("Reddit",         f"https://www.reddit.com/user/{target}"),
        ("GitHub",         f"https://github.com/{target}"),
        ("LinkedIn",       f"https://www.linkedin.com/in/{target}"),
        ("Pinterest",      f"https://www.pinterest.com/{target}/"),
        ("Tumblr",         f"https://www.tumblr.com/{target}"),
        ("Twitch",         f"https://www.twitch.tv/{target}"),
        ("Steam",          f"https://steamcommunity.com/id/{target}"),
        ("Snapchat",       f"https://www.snapchat.com/add/{target}"),
        ("Venmo",          f"https://venmo.com/{target}"),
        ("Cash App",       f"https://cash.app/${target}"),
        ("Telegram",       f"https://t.me/{target}"),
        ("Discord Lookup", f"https://discord.id/"),
        ("Roblox",         f"https://www.roblox.com/user.aspx?username={target}"),
        ("Patreon",        f"https://www.patreon.com/{target}"),
        ("OnlyFans",       f"https://onlyfans.com/{target}"),
        ("Linktree",       f"https://linktr.ee/{target}"),
    ]
    for name, url in platforms:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("GOOGLE DORKS")
    lines.append("=" * 50)
    lines.append("")

    dorks = [
        f'"{target}" site:twitter.com',
        f'"{target}" site:instagram.com',
        f'"{target}" site:reddit.com',
        f'"{target}" site:facebook.com',
        f'"{target}" profile OR account',
        f'inurl:{target} profile',
    ]
    for dork in dorks:
        encoded = dork.replace(" ", "+").replace('"', '%22')
        lines.append(f"  {dork}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "username_search", "result": result})
    return result


def module_public_records(target, job_id):
    emit(job_id, "module_start", {"module": "public_records"})

    if "," in target:
        name_part = target.split(",")[0].strip()
        location_part = target.split(",")[1].strip()
    else:
        name_part = target
        location_part = ""

    name_plus = name_part.replace(" ", "+")
    wp_first = name_part.split()[0] if name_part else ""
    wp_last = name_part.split()[-1] if len(name_part.split()) > 1 else ""

    lines = []
    lines.append(f"TARGET: {name_part}")
    lines.append("")

    lines.append("=" * 50)
    lines.append("FREE PEOPLE & ADDRESS RECORDS")
    lines.append("=" * 50)
    lines.append("")

    free_people = [
        ("FamilyTreeNow (FREE — best)",  f"https://www.familytreenow.com/search/people/results?first={wp_first}&last={wp_last}"),
        ("TruePeopleSearch",             f"https://www.truepeoplesearch.com/results?name={name_plus}"),
        ("FastPeopleSearch",             f"https://www.fastpeoplesearch.com/name/{name_part.replace(' ','-').lower()}"),
        ("ClustrMaps",                   f"https://clustrmaps.com/person/{wp_last}-{wp_first}/"),
        ("SearchPeopleFree",             f"https://www.searchpeoplefree.com/find/{wp_first}-{wp_last}"),
        ("Nuwber",                       f"https://nuwber.com/search?firstName={wp_first}&lastName={wp_last}"),
        ("PublicRecords.Online",         f"https://publicrecords.online/search/?first_name={wp_first}&last_name={wp_last}"),
        ("PublicRecordsNow",             f"https://www.publicrecordsnow.com/"),
    ]
    for name, url in free_people:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("ARREST & CRIMINAL RECORDS")
    lines.append("=" * 50)
    lines.append("")

    criminal = [
        ("NM Courts (CourtLook)",        f"https://caselookup.nmcourts.gov/caselookup/app"),
        ("PACER Federal Courts",         f"https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("CourtListener (Free Federal)", f"https://www.courtlistener.com/?q={name_plus}&type=p"),
        ("VINE Offender Search NM",      f"https://vinelink.vineapps.com/search/NM/Person"),
        ("NM Corrections Inmate",        f"https://www.cd.nm.gov/divisions/oid/offender-search/"),
        ("ArrestFacts",                  f"https://arrestfacts.com/search?name={name_plus}"),
        ("BustedMugshots",               f"https://bustedmugshots.com/search?name={name_plus}"),
        ("MugshotSearch",               f"https://www.mugshots.com/search?q={name_plus}"),
        ("OpenSanctions Watchlist",      f"https://www.opensanctions.org/search/?q={name_plus}"),
        ("Sex Offender Registry NM",     f"https://www.nmsexoffender.dps.nm.gov/"),
        ("Sex Offender Registry National", f"https://www.nsopw.gov/Search/Results?firstName={wp_first}&lastName={wp_last}"),
    ]
    for name, url in criminal:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("VITAL RECORDS & GENEALOGY")
    lines.append("=" * 50)
    lines.append("")

    vital = [
        ("FamilySearch (Free)",       f"https://www.familysearch.org/search/record/results?q.givenName={wp_first}&q.surname={wp_last}"),
        ("Ancestry (limited free)",   f"https://www.ancestry.com/search/?name={wp_first}_{wp_last}"),
        ("FindAGrave",                f"https://www.findagrave.com/memorial/search?firstname={wp_first}&lastname={wp_last}"),
        ("BillionGraves",             f"https://billiongraves.com/search/results/#firstname={wp_first}&lastname={wp_last}"),
        ("Legacy.com Obituaries",     f"https://www.legacy.com/obituaries/search?keyword={name_plus}"),
        ("NamUs Missing Persons",     f"https://www.namus.gov/MissingPersons/Search#/results"),
    ]
    for name, url in vital:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("PROFESSIONAL LICENSES")
    lines.append("=" * 50)
    lines.append("")

    licenses = [
        ("NM License Lookup",         f"https://www.rld.nm.gov/licensing-and-regulation/"),
        ("NM Medical Board",          f"https://www.nmmb.state.nm.us/"),
        ("NM Bar Association",        f"https://www.nmbar.org/"),
        ("License.IQVIA (National)",  f"https://www.iqvia.com/"),
        ("NPPES (Medical NPI)",       f"https://npiregistry.cms.hhs.gov/search?search_type=ind&first_name={wp_first}&last_name={wp_last}"),
        ("BLS License Lookup",        f"https://www.careeronestop.org/Toolkit/Credentials/find-licenses.aspx"),
    ]
    for name, url in licenses:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    # Live CourtListener check
    try:
        out, _, _ = run_cmd(
            f"curl -s 'https://www.courtlistener.com/api/rest/v3/people/?name_last={wp_last}&name_first={wp_first}&format=json' 2>/dev/null",
            timeout=10
        )
        data = json.loads(out)
        count = data.get("count", 0)
        if count > 0:
            lines.append("=" * 50)
            lines.append(f"COURTLISTENER — {count} FEDERAL RECORD(S) FOUND")
            lines.append("=" * 50)
            lines.append("")
            for r in data.get("results", [])[:3]:
                lines.append(f"  Name: {r.get('name_full','N/A')}")
                lines.append(f"  URL:  https://www.courtlistener.com{r.get('absolute_url','')}")
                lines.append("")
    except:
        pass

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "public_records", "result": result})
    return result

# ── Module registry ───────────────────────────────────────────────────────────
MODULE_MAP = {
    "people":       module_people_search,
    "property":      module_property,
    "photo_forensics": module_photo_forensics,
    "geolocation":   module_geolocation,
    "username_search": module_username_search,
    "public_records": module_public_records,
    "social_media":  module_social_media,
    "business":      module_business,
    "plate_lookup":   module_plate_lookup,
    "image_metadata": module_image_metadata,
    "email_investigate": module_email_investigate,
    "phone":        module_phone,
    "whois":        module_whois,
    "dns":          module_dns,
        "nmap":         module_nmap,
    "geoip":        module_geoip,
        "sherlock":     module_sherlock,
    "shodan":       module_shodan,
    "virustotal":   module_virustotal,
                "dorks":        module_google_dorks,
}

def run_investigation(job_id, target, target_type, selected_modules):
    try:
        threads = []
        for mod_id in selected_modules:
            fn = MODULE_MAP.get(mod_id)
            if fn:
                t = threading.Thread(target=fn, args=(target, job_id), daemon=True)
                threads.append(t)
                t.start()
        for t in threads:
            t.join(timeout=130)
        jobs[job_id]["status"] = "complete"
        emit(job_id, "done", {"message": f"Complete: {target}"})
    except Exception as e:
        jobs[job_id]["status"] = "error"
        emit(job_id, "error", {"message": str(e)})


import hashlib
import secrets

# ── Secure Authentication ─────────────────────────────────────────────────────
# Users stored as environment variables in Render - never in source code
# Format in Render env vars:
# USER_RANGLADA=PLFAdmin2026!:admin:R Anglada
# USER_TLOPEZ=PLFInvest2026!:investigator:T Lopez
# USER_CMCPHERSON=PLFInvest2026!:investigator:C McPherson

active_sessions = {}  # token -> { username, role, name, created }

def get_users():
    """Load users from Render environment variables."""
    users = {}
    for key, val in os.environ.items():
        if key.startswith('USER_'):
            username = key[5:].lower()
            parts = val.split(':')
            if len(parts) >= 3:
                users[username] = {
                    'password': parts[0],
                    'role': parts[1],
                    'name': parts[2]
                }
    return users

def log_auth_event(username, action, detail, ip="unknown"):
    """Log authentication events."""
    print(f"[AUTH] {action} | user={username} | {detail} | ip={ip} | time={datetime.utcnow().isoformat()}")

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.json
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)

    users = get_users()
    user = users.get(username)

    if user and user["password"] == password:
        token = secrets.token_hex(32)
        active_sessions[token] = {
            "username": username,
            "role": user["role"],
            "name": user["name"],
            "created": datetime.utcnow().isoformat(),
            "ip": ip
        }
        log_auth_event(username, "LOGIN_SUCCESS", f"User {user['name']} authenticated", ip)
        return jsonify({
            "success": True,
            "token": token,
            "username": username,
            "role": user["role"],
            "name": user["name"]
        })
    else:
        log_auth_event(username, "LOGIN_FAILED", "Invalid credentials", ip)
        return jsonify({"success": False, "error": "Invalid credentials"}), 401

@app.route("/api/auth/verify", methods=["POST"])
def verify_token():
    data = request.json
    token = data.get("token", "")
    session = active_sessions.get(token)
    if session:
        return jsonify({"valid": True, "username": session["username"], "role": session["role"], "name": session["name"]})
    return jsonify({"valid": False}), 401

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    data = request.json
    token = data.get("token", "")
    session = active_sessions.pop(token, None)
    if session:
        log_auth_event(session["username"], "LOGOUT", "User logged out")
    return jsonify({"success": True})

@app.route("/api/auth/audit", methods=["POST"])
def get_audit():
    """Return audit log - admin only."""
    data = request.json
    token = data.get("token", "")
    session = active_sessions.get(token)
    if not session or session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    # Return last 500 server log entries
    return jsonify({"message": "Audit log is written to Render server logs. Check Render dashboard → Logs."})

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/api/investigate", methods=["GET", "POST"])
def investigate():
    if request.method == "POST":
        data = request.json or {}
    else:
        data = request.args
    target = data.get("target", "").strip()
    target_type = data.get("type", "DOMAIN")
    modules_param = data.get("modules", "")
    if isinstance(modules_param, str) and modules_param:
        selected_modules = modules_param.split(",")
    elif isinstance(modules_param, list):
        selected_modules = modules_param
    else:
        selected_modules = list(MODULE_MAP.keys())
    if not target:
        return jsonify({"error": "No target provided"}), 400
    job_id = f"job_{int(time.time()*1000)}"
    new_job(job_id)
    threading.Thread(
        target=run_investigation,
        args=(job_id, target, target_type, selected_modules),
        daemon=True
    ).start()
    return jsonify({"job_id": job_id})

@app.route("/api/stream/<job_id>")
def stream(job_id):
    def generate():
        if job_id not in jobs:
            yield f"data: {json.dumps({'type':'error','data':{'message':'Job not found'}})}\n\n"
            return
        while True:
            try:
                event = jobs[job_id]["events"].get(timeout=30)
                yield f"data: {json.dumps(event)}\n\n"
                if event["type"] in ("done", "error"):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'type':'ping','data':{}})}\n\n"
                if jobs[job_id]["status"] in ("complete", "error"):
                    break
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/health")
def health():
    tools = {t: tool_available(t) for t in ["whois", "dig", "curl"]}
    return jsonify({"status": "ok", "tools": tools})

@app.route("/")
def index():
    return "FIVE T OSINT Backend running. Connect your frontend."

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
