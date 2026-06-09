"""
FIVE T OSINT Agent - Backend Server
Runs on Render.com (Python 3.10+)
"""

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import subprocess, threading, json, os, time, queue, socket, re
from datetime import datetime

app = Flask(__name__)
app.config['PROPAGATE_EXCEPTIONS'] = True

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

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

    US_STATES = {
        'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN',
        'IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV',
        'NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN',
        'TX','UT','VT','VA','WA','WV','WI','WY','DC'
    }

    if "," in target:
        name_part = target.split(",")[0].strip()
        location_part = target.split(",")[1].strip()
    else:
        parts = target.split()
        if len(parts) >= 3 and parts[-1].upper() in US_STATES:
            state = parts[-1].upper()
            if len(parts) >= 4:
                name_part = " ".join(parts[:-2])
                location_part = " ".join(parts[-2:])
            else:
                name_part = " ".join(parts[:-1])
                location_part = parts[-1]
        elif len(parts) <= 2:
            name_part = target
            location_part = ""
        elif len(parts) == 3:
            if len(parts[1].replace('.','')) <= 2:
                name_part = target
                location_part = ""
            else:
                name_part = target
                location_part = ""
        else:
            if len(parts[1].replace('.','')) <= 2:
                name_part = " ".join(parts[:3])
                location_part = " ".join(parts[3:])
            else:
                name_part = " ".join(parts[:2])
                location_part = " ".join(parts[2:])

    name_words = name_part.split()
    wp_first = name_words[0] if name_words else ""
    wp_last = name_words[-1] if len(name_words) > 1 else ""

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

    sites = [
        ("TRUEPEOPLESEARCH",     f"https://www.truepeoplesearch.com/results?name={name_plus}&citystatezip={loc_plus}"),
        ("FASTPEOPLESEARCH",     f"https://www.fastpeoplesearch.com/name/{name_url}"),
        ("WHITEPAGES (free)",    f"https://www.whitepages.com/name/{wp_first}-{wp_last}/{wp_city}-{wp_state}"),
        ("USPHONEBOOK",          f"https://www.usphonebook.com/{wp_first}-{wp_last}"),
        ("CHECKPEOPLE",          f"https://checkpeople.com/search?firstName={wp_first}&lastName={wp_last}&state={wp_state}"),
        ("RADARIS",              f"https://radaris.com/p/{wp_first}-{wp_last}/"),
        ("VOTERRECORDS",         f"https://voterrecords.com/voters/{name_url}/1"),
        ("CLUSTRMAPS",           f"https://clustrmaps.com/person/{wp_last}-{wp_first}/"),
        ("PUBLICRECORDS.ONLINE", f"https://publicrecords.online/search/?first_name={wp_first}&last_name={wp_last}&state={wp_state}"),
        ("SEARCHPEOPLEFREE",     f"https://www.searchpeoplefree.com/find/{wp_first}-{wp_last}"),
        ("NUWBER",               f"https://nuwber.com/search?firstName={wp_first}&lastName={wp_last}&city={wp_city}&state={wp_state}"),
        ("ADDRESSES.COM",        f"https://www.addresses.com/people/{wp_first}+{wp_last}/{wp_state}/"),
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
        ("NM Courts (CourtLook)",   "https://caselookup.nmcourts.gov/caselookup/app"),
        ("VINE Offender Search",    "https://vinelink.vineapps.com/search/NM/Person"),
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
    clean = target.replace("-","").replace("(","").replace(")","").replace(" ","").replace("+1","").strip()
    formatted = f"({clean[:3]}) {clean[3:6]}-{clean[6:]}" if len(clean) == 10 else target
    phone_plus1 = f"+1{clean}" if len(clean) == 10 else target

    lines = []
    lines.append(f"TARGET:    {formatted}")
    lines.append(f"CLEANED:   {clean}")
    lines.append("")

    # ── Live carrier lookup — uses env vars, no hardcoded keys ───────────────
    ipqs_key = os.environ.get("IPQS_API_KEY", "")
    nv_key = os.environ.get("NUMVERIFY_API_KEY", "")

    if ipqs_key:
        try:
            ipqs_out, _, _ = run_cmd(
                f"curl -s 'https://www.ipqualityscore.com/api/json/phone/{ipqs_key}/{clean}' 2>/dev/null",
                timeout=10
            )
            ipqs_data = json.loads(ipqs_out)
            if ipqs_data.get("success"):
                lines.append("=== CARRIER INTELLIGENCE (IPQS) ===")
                lines.append(f"Valid:        {ipqs_data.get('valid', 'N/A')}")
                lines.append(f"Line Type:    {ipqs_data.get('line_type', 'N/A')}")
                lines.append(f"Carrier:      {ipqs_data.get('carrier', 'N/A')}")
                lines.append(f"Country:      {ipqs_data.get('country', 'N/A')}")
                lines.append(f"Risky:        {ipqs_data.get('risky', False)}")
                lines.append(f"Spam Score:   {ipqs_data.get('fraud_score', 'N/A')}")
                lines.append(f"VoIP:         {ipqs_data.get('VOIP', False)}")
                lines.append(f"Prepaid:      {ipqs_data.get('prepaid', False)}")
                lines.append("")
        except:
            pass

    if nv_key:
        try:
            nv_out, _, _ = run_cmd(
                f"curl -s 'http://apilayer.net/api/validate?access_key={nv_key}&number={clean}&country_code=US&format=1' 2>/dev/null",
                timeout=10
            )
            nv_data = json.loads(nv_out)
            if nv_data.get("valid"):
                lines.append("=== CARRIER INTELLIGENCE (NUMVERIFY) ===")
                lines.append(f"Valid:        {nv_data.get('valid', 'N/A')}")
                lines.append(f"Line Type:    {nv_data.get('line_type', 'N/A')}")
                lines.append(f"Carrier:      {nv_data.get('carrier', 'N/A')}")
                lines.append(f"Location:     {nv_data.get('location', 'N/A')}")
                lines.append(f"Country:      {nv_data.get('country_name', 'N/A')}")
                lines.append("")
        except:
            pass

    if not ipqs_key and not nv_key:
        lines.append("=== CARRIER INTELLIGENCE ===")
        lines.append("Add IPQS_API_KEY or NUMVERIFY_API_KEY to Render env vars for live carrier data.")
        lines.append("")

    lines.append("=" * 50)
    lines.append("REVERSE LOOKUP SITES — CLICK TO SEARCH")
    lines.append("=" * 50)
    lines.append("")

    sites = [
        ("TRUEPEOPLESEARCH", f"https://www.truepeoplesearch.com/results?phoneno={clean}"),
        ("WHITEPAGES",       f"https://www.whitepages.com/phone/{clean}"),
        ("SPOKEO",           f"https://www.spokeo.com/phone-search/{clean}"),
        ("BEENVERIFIED",     f"https://www.beenverified.com/phone/{clean}/"),
        ("INTELIUS",         f"https://intelius.com/phone-lookup/{clean}/"),
        ("PEOPLEFINDERS",    f"https://www.peoplefinders.com/phone/{clean}"),
        ("USPHONEBOOK",      f"https://www.usphonebook.com/{clean}"),
        ("411.COM",          f"https://www.411.com/phone/{clean}"),
        ("CALLERMART",       f"https://www.callermart.com/phone/{clean}"),
        ("ZLOOKUP",          "https://www.zlookup.com/"),
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
        ("800NOTES",       f"https://800notes.com/Phone.aspx/{clean}"),
        ("CALLERCENTER",   f"https://callercenter.com/{clean}"),
        ("WHOCALLEDUS",    f"https://whocalledus.com/calls/{clean}/"),
        ("CALLERCOMMENTS", f"https://callercomments.com/calls/{clean}/"),
        ("NOMOROBO",       f"https://www.nomorobo.com/lookup/{clean}"),
        ("SPAMCALLS",      f"https://spamcalls.net/en/search?n={clean}"),
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
        ("Facebook",   f"https://www.facebook.com/search/people/?q={clean}"),
        ("TrueCaller",  f"https://www.truecaller.com/search/us/{clean}"),
        ("Telegram",   f"https://t.me/+1{clean}"),
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

    lines.append("=" * 50)
    lines.append("SECRETARY OF STATE — DIRECT SEARCHES")
    lines.append("=" * 50)
    lines.append("")

    sos_sites = [
        ("NM SOS Business Search",  f"https://portal.sos.state.nm.us/BFS/online/CorporationFormation/SearchBusinesses?SearchCriteria={name_plus}"),
        ("NM SOS (alternate)",      "https://businessportal.sos.nm.gov/"),
        ("TX SOS Business Search",  "https://mycpa.cpa.state.tx.us/coa/Index.html"),
        ("AZ SOS Business Search",  f"https://ecorp.azcc.gov/BusinessSearch/BusinessSearch?SearchTerm={name_plus}"),
        ("CO SOS Business Search",  f"https://www.sos.state.co.us/biz/BusinessEntityCriteriaExt.do?nameTyp=ENT&masterFileId=&entityName={name_plus}"),
        ("CA SOS Business Search",  "https://bizfileonline.sos.ca.gov/search/business"),
        ("FL SOS Business Search",  f"https://search.sunbiz.org/Inquiry/CorporationSearch/SearchResults?searchNameOrder={name_plus}"),
        ("NY SOS Business Search",  "https://apps.dos.ny.gov/publicInquiry/EntitySearch"),
    ]
    for name, url in sos_sites:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("FEDERAL BUSINESS DATABASES")
    lines.append("=" * 50)
    lines.append("")

    federal = [
        ("SAM.gov (Federal Contractors)",  f"https://sam.gov/search/?keywords={name_plus}&sort=relevanceScore&index=ei&is_active=true&page=1"),
        ("SEC EDGAR (Public Companies)",    f"https://www.sec.gov/cgi-bin/browse-edgar?company={name_plus}&CIK=&type=&dateb=&owner=include&count=40&search_text=&action=getcompany"),
        ("FCC License Search",              "https://wireless2.fcc.gov/UlsApp/UlsSearch/searchLicense.jsp"),
        ("PACER Business Search",           "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("BetterBusiness Bureau",           f"https://www.bbb.org/search?find_text={name_plus}&find_loc=Albuquerque%2C+NM"),
        ("OpenCorporates All States",       f"https://opencorporates.com/companies?q={name_plus}&jurisdiction_code=us&utf8=✓"),
    ]
    for name, url in federal:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("BUSINESS INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")

    intel = [
        ("LinkedIn Company",   f"https://www.linkedin.com/search/results/companies/?keywords={name_plus}"),
        ("Dun & Bradstreet",   f"https://www.dnb.com/business-directory/company-search.html#{name_plus}"),
        ("Manta",              f"https://www.manta.com/mb_{name_url}"),
        ("Yelp Business",      f"https://www.yelp.com/search?find_desc={name_plus}&find_loc=Albuquerque%2C+NM"),
        ("Google Business",    f"https://www.google.com/search?q={name_plus}+Albuquerque+NM+business"),
        ("Glassdoor",          f"https://www.glassdoor.com/Search/results.htm?keyword={name_plus}"),
        ("Indeed Company",     f"https://www.indeed.com/cmp/{name_url}"),
        ("Bizapedia",          "https://www.bizapedia.com/nm/"),
        ("Corporationwiki",    f"https://www.corporationwiki.com/search/results?term={name_plus}"),
    ]
    for name, url in intel:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("REGISTERED AGENT & OFFICER SEARCH")
    lines.append("=" * 50)
    lines.append("")

    officer_searches = [
        ("OpenCorporates Officers", f"https://opencorporates.com/officers?q={name_plus}&utf8=✓"),
        ("Corporationwiki Network", f"https://www.corporationwiki.com/search/results?term={name_plus}"),
        ("NM SOS Officer Search",   f"https://portal.sos.state.nm.us/BFS/online/CorporationFormation/SearchBusinesses?SearchCriteria={name_plus}"),
    ]
    for name, url in officer_searches:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("GOOGLE DORKS")
    lines.append("=" * 50)
    lines.append("")

    dorks = [
        f'"{target}" owner OR CEO OR president',
        f'"{target}" registered agent New Mexico',
        f'"{target}" lawsuit OR litigation',
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
    target_clean = target.upper().strip()
    parts = target_clean.split()
    states = ["NM","TX","AZ","CO","CA","FL","NY","IL","OH","GA","NC","MI","PA","WA","OR"]
    if len(parts) >= 2 and parts[-1] in states:
        plate = parts[0].replace("-", "")
        state = parts[-1]
    else:
        plate = target_clean.replace(" ", "").replace("-", "")
        state = "NM"

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
        ("NM MVD Records Request",    "https://www.mvd.newmexico.gov/"),
        ("NM Courts - Vehicle Cases", "https://caselookup.nmcourts.gov/caselookup/app"),
        ("NM Public Records Request", "https://www.nmag.gov/public-records-requests.aspx"),
        ("NM Taxation & Revenue MVD", "https://www.mvd.newmexico.gov/"),
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
        ("VINCheck (NICB stolen)",    "https://www.nicb.org/vincheck"),
        ("NHTSA VIN Decoder",         "https://vpic.nhtsa.dot.gov/decoder/"),
        ("NMVTIS Vehicle History",    "https://www.vehiclehistory.gov/"),
        ("RecallsByVIN",              "https://www.nhtsa.gov/recalls"),
        ("Plate Search (freecarvin)", "https://www.freecarvin.com/"),
        ("VehicleHistory",            f"https://www.vehiclehistory.com/license-plate-search?plate={plate}&state=NM"),
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
        ("Facebook",  f"https://www.facebook.com/search/posts/?q={plate}"),
        ("Instagram", f"https://www.instagram.com/explore/search/keyword/?q={plate}"),
        ("Twitter/X", f"https://twitter.com/search?q=%22{plate}%22&f=live"),
        ("Reddit",    f"https://www.reddit.com/search/?q=%22{plate}%22"),
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


def module_vin_investigation(target, job_id):
    emit(job_id, "module_start", {"module": "vin_investigation"})

    vin = target.upper().strip().replace(" ", "").replace("-", "")
    lines = []

    # ── VIN Validation ────────────────────────────────────────────────────────
    lines.append(f"TARGET VIN: {vin}")
    lines.append(f"LENGTH:     {len(vin)} characters {'✓ VALID' if len(vin) == 17 else '✗ INVALID — must be 17 characters'}")
    lines.append("")

    if len(vin) != 17:
        lines.append("⚠ VIN must be exactly 17 characters.")
        lines.append("Common issues: spaces, dashes, letter O vs zero, letter I vs one.")
        result = "\n".join(lines)
        emit(job_id, "module_done", {"module": "vin_investigation", "result": result})
        return result

    # ── Check digit validation ────────────────────────────────────────────────
    def validate_check_digit(v):
        transliterate = {
            'A':1,'B':2,'C':3,'D':4,'E':5,'F':6,'G':7,'H':8,
            'J':1,'K':2,'L':3,'M':4,'N':5,'P':7,'R':9,
            'S':2,'T':3,'V':5,'W':6,'X':7,'Y':8,'Z':9
        }
        weights = [8,7,6,5,4,3,2,10,0,9,8,7,6,5,4,3,2]
        total = 0
        for i, ch in enumerate(v):
            val = int(ch) if ch.isdigit() else transliterate.get(ch, 0)
            total += val * weights[i]
        rem = total % 11
        expected = 'X' if rem == 10 else str(rem)
        return expected == v[8], expected, v[8]

    check_ok, expected_digit, actual_digit = validate_check_digit(vin)
    lines.append(f"CHECK DIGIT (pos 9): {actual_digit} — {'✓ VALID' if check_ok else f'✗ MISMATCH (expected {expected_digit}) — verify VIN accuracy'}")
    lines.append("")

    # ── STEP 1: NHTSA vPIC Decode (free, no key) ─────────────────────────────
    lines.append("=" * 50)
    lines.append("NHTSA vPIC VEHICLE DECODE (LIVE)")
    lines.append("=" * 50)
    lines.append("")

    nhtsa_make  = ""
    nhtsa_model = ""
    nhtsa_year  = ""

    try:
        nhtsa_out, _, rc = run_cmd(
            f"curl -s --max-time 15 'https://vpic.nhtsa.dot.gov/api/vehicles/decodevinvalues/{vin}?format=json' 2>/dev/null",
            timeout=20
        )
        if rc == 0 and nhtsa_out:
            r = json.loads(nhtsa_out).get("Results", [{}])[0]
            nhtsa_make    = r.get("Make", "")
            nhtsa_model   = r.get("Model", "")
            nhtsa_year    = r.get("ModelYear", "")
            series        = r.get("Series", "")
            trim          = r.get("Trim", "")
            body          = r.get("BodyClass", "N/A")
            drive         = r.get("DriveType", "N/A")
            fuel          = r.get("FuelTypePrimary", "N/A")
            engine_l      = r.get("DisplacementL", "")
            cylinders     = r.get("EngineCylinders", "")
            trans         = r.get("TransmissionStyle", "N/A")
            gvwr          = r.get("GVWR", "N/A")
            mfr           = r.get("Manufacturer", "N/A")
            plant_city    = r.get("PlantCity", "")
            plant_state   = r.get("PlantState", "")
            plant_country = r.get("PlantCountry", "")
            plant = ", ".join(filter(None, [plant_city, plant_state, plant_country])) or "N/A"
            err_code = r.get("ErrorCode", "")
            err_text = r.get("ErrorText", "")
            engine_str = (f"{engine_l}L " if engine_l else "") + (f"{cylinders}-cylinder" if cylinders else "")
            engine_str = engine_str.strip() or "N/A"

            lines.append(f"MAKE:           {nhtsa_make or 'N/A'}")
            lines.append(f"MODEL:          {nhtsa_model or 'N/A'}")
            lines.append(f"YEAR:           {nhtsa_year or 'N/A'}")
            if series:  lines.append(f"SERIES:         {series}")
            if trim:    lines.append(f"TRIM:           {trim}")
            lines.append(f"BODY CLASS:     {body}")
            lines.append(f"DRIVE TYPE:     {drive}")
            lines.append(f"FUEL TYPE:      {fuel}")
            lines.append(f"ENGINE:         {engine_str}")
            lines.append(f"TRANSMISSION:   {trans}")
            lines.append(f"GVWR:           {gvwr}")
            lines.append(f"MANUFACTURER:   {mfr}")
            lines.append(f"ASSEMBLY PLANT: {plant}")
            if err_code and err_code != "0":
                lines.append(f"DECODE NOTE:    {err_text[:120]}")
            lines.append("")
        else:
            lines.append("NHTSA vPIC API unreachable.")
            lines.append("")
    except Exception as e:
        lines.append(f"NHTSA decode error: {str(e)}")
        lines.append("")

    # ── VIN Character Breakdown ───────────────────────────────────────────────
    model_year_map = {
        'A':'1980/2010','B':'1981/2011','C':'1982/2012','D':'1983/2013',
        'E':'1984/2014','F':'1985/2015','G':'1986/2016','H':'1987/2017',
        'J':'1988/2018','K':'1989/2019','L':'2020','M':'2021','N':'2022',
        'P':'2023','R':'2024','S':'2025','T':'2026','V':'2027','W':'2028',
        'X':'2029','Y':'2030','1':'2001','2':'2002','3':'2003','4':'2004',
        '5':'2005','6':'2006','7':'2007','8':'2008','9':'2009',
    }
    vin_year_display = nhtsa_year if nhtsa_year else model_year_map.get(vin[9], "Unknown")

    lines.append("=" * 50)
    lines.append("VIN STRUCTURAL BREAKDOWN")
    lines.append("=" * 50)
    lines.append("")
    lines.append(f"  Pos 1-3   WMI (World Manufacturer Identifier): {vin[0:3]}")
    lines.append(f"  Pos 4-8   VDS (Vehicle Descriptor Section):    {vin[3:8]}")
    lines.append(f"  Pos 9     Check Digit:                          {vin[8]} {'✓' if check_ok else '✗'}")
    lines.append(f"  Pos 10    Model Year Code:                      {vin[9]} = {vin_year_display}")
    lines.append(f"  Pos 11    Assembly Plant Code:                  {vin[10]}")
    lines.append(f"  Pos 12-17 Production Serial:                   {vin[11:17]}")
    lines.append("")

    # ── STEP 2: NHTSA Recall Check (live by VIN) ─────────────────────────────
    lines.append("=" * 50)
    lines.append("NHTSA SAFETY RECALLS (LIVE)")
    lines.append("=" * 50)
    lines.append("")

    recall_count = 0
    try:
        # Primary: VIN-specific recall endpoint
        vin_recall_out, _, rc1 = run_cmd(
            f"curl -s --max-time 15 'https://api.nhtsa.gov/recalls/recallsByVehicle?make={nhtsa_make}&model={nhtsa_model}&modelYear={nhtsa_year}' 2>/dev/null",
            timeout=20
        )
        if rc1 == 0 and vin_recall_out:
            rdata = json.loads(vin_recall_out)
            recalls = rdata.get("results", rdata.get("Results", []))
            recall_count = len(recalls)
            if recalls:
                lines.append(f"⚠ {recall_count} RECALL(S) FOUND")
                lines.append("")
                for rec in recalls[:10]:
                    lines.append(f"  NHTSA #:    {rec.get('NHTSACampaignNumber', 'N/A')}")
                    lines.append(f"  Component:  {rec.get('Component', 'N/A')}")
                    lines.append(f"  Date:       {rec.get('ReportReceivedDate', 'N/A')}")
                    summary = rec.get('Summary', '')
                    consequence = rec.get('Consequence', '')
                    remedy = rec.get('Remedy', '')
                    if summary:
                        lines.append(f"  Summary:    {summary[:250]}")
                    if consequence:
                        lines.append(f"  Risk:       {consequence[:200]}")
                    if remedy:
                        lines.append(f"  Remedy:     {remedy[:200]}")
                    lines.append("")
                if recall_count > 10:
                    lines.append(f"  ... and {recall_count - 10} more. See full list at NHTSA link below.")
                    lines.append("")
            else:
                lines.append("✓ No open recalls found for this make/model/year.")
                lines.append("")
        else:
            lines.append("Recall API unreachable — verify at link below.")
            lines.append("")
    except Exception as e:
        lines.append(f"Recall lookup error: {str(e)}")
        lines.append("")

    lines.append(f"  Full recall check: https://www.nhtsa.gov/vehicle/{vin}/recalls")
    lines.append("")

    # ── STEP 3: NHTSA Consumer Complaints (live) ─────────────────────────────
    lines.append("=" * 50)
    lines.append("NHTSA CONSUMER COMPLAINTS (LIVE)")
    lines.append("=" * 50)
    lines.append("")

    try:
        comp_out, _, rc2 = run_cmd(
            f"curl -s --max-time 15 'https://api.nhtsa.gov/complaints/complaintsByVehicle?make={nhtsa_make}&model={nhtsa_model}&modelYear={nhtsa_year}' 2>/dev/null",
            timeout=20
        )
        if rc2 == 0 and comp_out:
            cdata = json.loads(comp_out)
            complaints = cdata.get("results", cdata.get("Results", []))
            total_comp = len(complaints)

            if complaints:
                # Tally summary stats
                crashes   = sum(1 for c in complaints if c.get('crash', False))
                fires     = sum(1 for c in complaints if c.get('fire', False))
                injuries  = sum(c.get('numberOfInjuries', 0) or 0 for c in complaints)
                deaths    = sum(c.get('numberOfDeaths', 0) or 0 for c in complaints)

                lines.append(f"TOTAL COMPLAINTS:  {total_comp}")
                lines.append(f"CRASHES REPORTED:  {crashes}")
                lines.append(f"FIRES REPORTED:    {fires}")
                lines.append(f"INJURIES REPORTED: {injuries}")
                lines.append(f"DEATHS REPORTED:   {deaths}")
                lines.append("")

                # Top complained-about components
                from collections import Counter
                comp_counter = Counter(
                    c.get('components', c.get('component', 'UNKNOWN'))
                    for c in complaints
                )
                top_comps = comp_counter.most_common(5)
                if top_comps:
                    lines.append("TOP COMPLAINT AREAS:")
                    for comp_name, count in top_comps:
                        lines.append(f"  • {comp_name}: {count} complaint(s)")
                    lines.append("")

                # Show the 3 most recent complaints with detail
                lines.append("MOST RECENT COMPLAINTS:")
                lines.append("")
                recent = sorted(
                    complaints,
                    key=lambda x: x.get('dateOfIncident', x.get('incidentDate', '')),
                    reverse=True
                )[:3]
                for c in recent:
                    date    = c.get('dateOfIncident', c.get('incidentDate', 'N/A'))
                    comp_nm = c.get('components', c.get('component', 'N/A'))
                    desc    = c.get('summary', c.get('description', ''))
                    crash   = c.get('crash', False)
                    fire    = c.get('fire', False)
                    inj     = c.get('numberOfInjuries', 0)
                    lines.append(f"  Date:      {date}")
                    lines.append(f"  Component: {comp_nm}")
                    if crash:   lines.append(f"  ⚠ CRASH INVOLVED")
                    if fire:    lines.append(f"  ⚠ FIRE INVOLVED")
                    if inj:     lines.append(f"  Injuries:  {inj}")
                    if desc:    lines.append(f"  Summary:   {desc[:300]}")
                    lines.append("")
            else:
                lines.append("✓ No consumer complaints on record for this vehicle.")
                lines.append("")
        else:
            lines.append("Complaints API unreachable.")
            lines.append("")
    except Exception as e:
        lines.append(f"Complaints lookup error: {str(e)}")
        lines.append("")

    # ── STEP 4: NHTSA Safety Ratings (live) ──────────────────────────────────
    lines.append("=" * 50)
    lines.append("NHTSA SAFETY RATINGS — NCAP CRASH TESTS (LIVE)")
    lines.append("=" * 50)
    lines.append("")

    try:
        # Get vehicle variants list first
        variants_out, _, rc3 = run_cmd(
            f"curl -s --max-time 15 'https://api.nhtsa.gov/SafetyRatings/modelyear/{nhtsa_year}/make/{nhtsa_make}/model/{nhtsa_model}?format=json' 2>/dev/null",
            timeout=20
        )
        if rc3 == 0 and variants_out:
            vdata = json.loads(variants_out)
            variants = vdata.get("Results", [])
            if variants:
                # Pull ratings for first variant
                vid = variants[0].get("VehicleId")
                if vid:
                    ratings_out, _, rc4 = run_cmd(
                        f"curl -s --max-time 15 'https://api.nhtsa.gov/SafetyRatings/VehicleId/{vid}?format=json' 2>/dev/null",
                        timeout=20
                    )
                    if rc4 == 0 and ratings_out:
                        rdata2 = json.loads(ratings_out)
                        rat = rdata2.get("Results", [{}])[0]
                        overall       = rat.get("OverallRating", "N/A")
                        frontal_dr    = rat.get("OverallFrontCrashRating", "N/A")
                        side          = rat.get("OverallSideCrashRating", "N/A")
                        rollover      = rat.get("RolloverRating", "N/A")
                        rollover_risk = rat.get("RolloverPossibility", "N/A")
                        description   = rat.get("VehicleDescription", "")

                        def star(val):
                            try:
                                n = int(val)
                                return "★" * n + "☆" * (5 - n) + f" ({n}/5)"
                            except:
                                return str(val)

                        if description:
                            lines.append(f"VEHICLE:          {description}")
                        lines.append(f"OVERALL RATING:   {star(overall)}")
                        lines.append(f"FRONTAL CRASH:    {star(frontal_dr)}")
                        lines.append(f"SIDE CRASH:       {star(side)}")
                        lines.append(f"ROLLOVER:         {star(rollover)}")
                        if rollover_risk and rollover_risk != "N/A":
                            lines.append(f"ROLLOVER RISK:    {rollover_risk}%")
                        if len(variants) > 1:
                            lines.append(f"NOTE: {len(variants)} variant(s) tested. Showing primary.")
                        lines.append("")
                    else:
                        lines.append("Ratings data unavailable for this vehicle.")
                        lines.append("")
            else:
                lines.append("No NCAP crash test data found for this vehicle.")
                lines.append("(Not all vehicles are tested — older, specialty, or low-volume vehicles may not have ratings.)")
                lines.append("")
        else:
            lines.append("Safety ratings API unreachable.")
            lines.append("")
    except Exception as e:
        lines.append(f"Safety ratings error: {str(e)}")
        lines.append("")

    # ── STEP 5: NICB Stolen / Salvage Check (live) ───────────────────────────
    lines.append("=" * 50)
    lines.append("NICB STOLEN / SALVAGE CHECK (LIVE)")
    lines.append("=" * 50)
    lines.append("")

    try:
        nicb_out, _, rc5 = run_cmd(
            f"curl -s --max-time 15 -X POST 'https://www.nicb.org/api/vincheck' "
            f"-H 'Content-Type: application/json' "
            f"-H 'Accept: application/json' "
            f"-H 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' "
            f"-H 'Origin: https://www.nicb.org' "
            f"-H 'Referer: https://www.nicb.org/vincheck' "
            f"--data '{{\"vin\":\"{vin}\"}}' 2>/dev/null",
            timeout=20
        )
        if rc5 == 0 and nicb_out and len(nicb_out) > 5:
            try:
                nicb_data = json.loads(nicb_out)
                stolen  = nicb_data.get("stolen", nicb_data.get("isStolen", nicb_data.get("theft", None)))
                salvage = nicb_data.get("salvage", nicb_data.get("isSalvage", nicb_data.get("totalLoss", None)))
                msg     = nicb_data.get("message", nicb_data.get("result", nicb_data.get("status", "")))

                if stolen is True or (isinstance(stolen, str) and stolen.lower() in ("yes","true","1")):
                    lines.append("⚠ ⚠ ⚠  VEHICLE REPORTED STOLEN — NOT RECOVERED  ⚠ ⚠ ⚠")
                elif stolen is False or (isinstance(stolen, str) and stolen.lower() in ("no","false","0")):
                    lines.append("✓ Not reported as stolen in NICB database.")
                else:
                    lines.append(f"Stolen status: {stolen if stolen is not None else 'See NICB directly'}")

                if salvage is True or (isinstance(salvage, str) and salvage.lower() in ("yes","true","1")):
                    lines.append("⚠ VEHICLE HAS SALVAGE / TOTAL LOSS RECORD")
                elif salvage is False or (isinstance(salvage, str) and salvage.lower() in ("no","false","0")):
                    lines.append("✓ No salvage/total loss record in NICB database.")
                else:
                    lines.append(f"Salvage status: {salvage if salvage is not None else 'See NICB directly'}")

                if msg:
                    lines.append(f"NICB message: {str(msg)[:200]}")
            except:
                # Non-JSON response — show raw
                clean_resp = nicb_out[:500].strip()
                if any(word in clean_resp.lower() for word in ["stolen","theft","salvage","total loss"]):
                    lines.append(f"NICB response: {clean_resp}")
                else:
                    lines.append("NICB returned non-standard response — verify directly.")
        else:
            lines.append("NICB API did not return data — verify directly at link below.")
        lines.append("")
        lines.append(f"  Verify: https://www.nicb.org/vincheck")
        lines.append(f"  Note: NICB limits to 5 searches/24hrs per IP address.")
        lines.append("")
    except Exception as e:
        lines.append(f"NICB check error: {str(e)}")
        lines.append(f"  Verify directly: https://www.nicb.org/vincheck")
        lines.append("")

    # ── STEP 6: Auction Search (Copart + IAAI live queries) ──────────────────
    lines.append("=" * 50)
    lines.append("AUCTION HISTORY — LIVE SEARCH")
    lines.append("=" * 50)
    lines.append("")

    # Copart API search
    try:
        copart_out, _, rc6 = run_cmd(
            f"curl -s --max-time 15 "
            f"-H 'Accept: application/json, text/plain, */*' "
            f"-H 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' "
            f"'https://www.copart.com/public/data/lotSearch/search?query={vin}&page=0&size=5&sort=lotCreateDate%2Cdesc' "
            f"2>/dev/null",
            timeout=20
        )
        if rc6 == 0 and copart_out and "{" in copart_out:
            try:
                cdata = json.loads(copart_out)
                # Try various response shapes Copart uses
                lots = (cdata.get("data", {}).get("results", {}).get("content", [])
                        or cdata.get("returnCode", {})
                        or [])
                if isinstance(lots, list) and lots:
                    lines.append(f"COPART: {len(lots)} lot(s) found")
                    lines.append("")
                    for lot in lots[:3]:
                        lines.append(f"  Lot #:     {lot.get('ln', lot.get('lotNumber', 'N/A'))}")
                        lines.append(f"  Title:     {lot.get('ld', lot.get('lotDescription', 'N/A'))}")
                        lines.append(f"  Sale Date: {lot.get('ld', lot.get('saleDate', 'N/A'))}")
                        lines.append(f"  Location:  {lot.get('yn', lot.get('yardName', 'N/A'))}")
                        lines.append(f"  Damage:    {lot.get('dd', lot.get('damageDescription', 'N/A'))}")
                        lines.append("")
                else:
                    lines.append("COPART: No auction records found for this VIN.")
                    lines.append("")
            except:
                lines.append("COPART: Response parsing error — check link below.")
                lines.append("")
        else:
            lines.append("COPART: Unable to query — check link below.")
            lines.append("")
    except Exception as e:
        lines.append(f"Copart search error: {str(e)}")
        lines.append("")

    lines.append(f"  Copart search:  https://www.copart.com/lot/search/#?q[]=%22{vin}%22")
    lines.append(f"  IAAI search:    https://www.iaai.com/Vehicles/Search?SearchType=4&SearchTerm={vin}")
    lines.append(f"  SalvageBid:     https://www.salvagebid.com/")
    lines.append(f"  AutoBidMaster:  https://www.autobidmaster.com/")
    lines.append("")

    # ── STEP 7: Title & Registration Tracing (links + DPPA context) ──────────
    lines.append("=" * 50)
    lines.append("TITLE & REGISTRATION TRACING (DPPA)")
    lines.append("=" * 50)
    lines.append("")
    lines.append("Law firm use qualifies under DPPA 18 U.S.C. § 2721(b)(4) — litigation.")
    lines.append("")

    title_resources = [
        ("NMVTIS — VinAudit (~$8, DPPA report)", f"https://www.vinaudit.com/get-vehicle-history-report?vin={vin}"),
        ("NMVTIS — VehicleHistory.gov",           "https://www.vehiclehistory.gov/"),
        ("AutoCheck by Experian",                  f"https://www.autocheck.com/vehiclehistory/?vin={vin}"),
        ("CarFax (title brands, accident history)", f"https://www.carfax.com/vehicle/{vin}"),
        ("NM MVD Title Request",                   "https://www.mvd.newmexico.gov/"),
        ("NHTSA Full Decode",                      f"https://vpic.nhtsa.dot.gov/decoder/Decoder?vin={vin}"),
        ("TLO (PLF tool — fastest owner lookup)",  "https://www.tlo.com/"),
        ("PeopleMap (PLF tool)",                   "https://www.peoplemaps.com/"),
        ("Tracers (PLF tool)",                     "https://www.tracers.com/"),
    ]
    for name, url in title_resources:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    # ── STEP 8: EDR / Crash Investigation Resources ───────────────────────────
    lines.append("=" * 50)
    lines.append("EDR / CRASH INVESTIGATION RESOURCES")
    lines.append("=" * 50)
    lines.append("")
    lines.append("For EDR data retrieval (Bosch CDR system):")
    lines.append("")

    edr_resources = [
        ("NHTSA EDR Regulations (49 CFR 563)",  "https://www.nhtsa.gov/document/49-cfr-part-563"),
        ("Bosch CDR Tool Info",                  "https://www.boschdiagnostics.com/cdr"),
        ("NHTSA Recall Check by VIN",            f"https://www.nhtsa.gov/vehicle/{vin}/recalls"),
        ("Driver Privacy Protection Act (DPPA)", "https://www.justice.gov/d9/2023-01/dppa_guidance.pdf"),
        ("NM IPRA Request Portal",               "https://www.nmag.gov/public-records-requests.aspx"),
        ("NM MVD Title/Registration Request",    "https://www.mvd.newmexico.gov/"),
    ]
    for name, url in edr_resources:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    # ── STEP 9: Google Dorks ──────────────────────────────────────────────────
    lines.append("=" * 50)
    lines.append("GOOGLE DORKS")
    lines.append("=" * 50)
    lines.append("")

    dorks = [
        f'"{vin}"',
        f'"{vin}" accident OR crash OR collision',
        f'"{vin}" title OR registration OR sold',
        f'"{vin}" auction OR salvage',
        f'"{vin}" stolen OR theft OR recovered',
        f'"{vin}" lawsuit OR litigation OR court',
        f'"{vin}" site:copart.com',
        f'"{vin}" site:iaai.com',
        f'"{vin}" site:facebook.com',
    ]
    for dork in dorks:
        encoded = dork.replace(" ", "+").replace('"', '%22')
        lines.append(f"  {dork}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    # ── STEP 10: Investigative Next Steps ────────────────────────────────────
    lines.append("=" * 50)
    lines.append("INVESTIGATIVE NEXT STEPS")
    lines.append("=" * 50)
    lines.append("")
    lines.append(f"01. Run VIN in TLO/PeopleMap/Tracers — fastest path to current registered owner.")
    lines.append(f"02. Pull NMVTIS report via VinAudit (~$8) to confirm title state and any brands.")
    lines.append(f"03. If still in NM: submit MVD title request citing DPPA §2721(b)(4), VIN {vin}.")
    lines.append(f"04. If vehicle crossed state lines: identify destination from NMVTIS, then")
    lines.append(f"    submit title request to that state DMV under DPPA.")
    lines.append(f"05. If sold at auction: serve preservation letter/subpoena on auction house")
    lines.append(f"    demanding buyer records referencing VIN {vin}.")
    lines.append(f"06. Once vehicle located: arrange certified CDR analyst download immediately.")
    lines.append(f"    Document ignition cycle count BEFORE vehicle is started again.")
    lines.append(f"07. Subpoena manufacturer telematics data (FordPass, Mercedes Me, OnStar, etc.)")
    lines.append(f"    directly from manufacturer — GPS track, trip history, crash notification.")
    lines.append(f"08. If any recalls or complaints returned above, note for expert witness prep.")
    lines.append("")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "vin_investigation", "result": result})
    return result



def module_image_metadata(target, job_id):
    emit(job_id, "module_start", {"module": "image_metadata"})
    lines = []
    lines.append(f"TARGET: {target}")
    lines.append("")

    if target.startswith("http"):
        try:
            run_cmd(f"curl -s -L -o /tmp/sentinel_img.jpg '{target}' 2>/dev/null", timeout=15)
            exif_out, _, rc = run_cmd("exiftool /tmp/sentinel_img.jpg 2>/dev/null", timeout=10)

            if exif_out and rc == 0:
                lines.append("=" * 50)
                lines.append("EXIF METADATA EXTRACTED")
                lines.append("=" * 50)
                lines.append("")

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
                    lines.append(f"  Google Maps:       https://www.google.com/maps?q={gps_lat},{gps_lon}")
                    lines.append(f"  Google Street View: https://www.google.com/maps?q=&layer=c&cbll={gps_lat},{gps_lon}")
                    lines.append("")
            else:
                str_out, _, _ = run_cmd("strings /tmp/sentinel_img.jpg | grep -i 'gps\\|location\\|date\\|camera\\|make\\|model' 2>/dev/null | head -20")
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

    lines.append("=" * 50)
    lines.append("MANUAL IMAGE ANALYSIS TOOLS")
    lines.append("=" * 50)
    lines.append("")

    manual_tools = [
        ("Jeffrey EXIF Viewer",            "http://exif.regex.info/exif.cgi"),
        ("ExifTool Online",                "https://exiftool.org/"),
        ("Metadata2Go",                    "https://www.metadata2go.com/"),
        ("Google Reverse Image",           f"https://images.google.com/searchbyimage?image_url={target}" if target.startswith("http") else "https://images.google.com/"),
        ("TinEye Reverse Image",           f"https://tineye.com/search?url={target}" if target.startswith("http") else "https://tineye.com/"),
        ("Yandex Reverse Image",           f"https://yandex.com/images/search?url={target}&rpt=imageview" if target.startswith("http") else "https://yandex.com/images/"),
        ("Bing Visual Search",             "https://www.bing.com/images/search?view=detailv2&iss=sbi"),
        ("FotoForensics",                  "https://fotoforensics.com/"),
        ("InVID/WeVerify",                 "https://www.invid-project.eu/tools-and-services/invid-verification-plugin/"),
    ]
    for name, url in manual_tools:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    if target.startswith("http"):
        lines.append("=" * 50)
        lines.append("REVERSE IMAGE SEARCH LINKS")
        lines.append("=" * 50)
        lines.append("")
        rev_searches = [
            ("Google Images", f"https://images.google.com/searchbyimage?image_url={target}"),
            ("TinEye",        f"https://tineye.com/search?url={target}"),
            ("Yandex",        f"https://yandex.com/images/search?url={target}&rpt=imageview"),
        ]
        for name, url in rev_searches:
            lines.append(f"[{name}]")
            lines.append(f"  {url}")
            lines.append("")
    else:
        lines.append("Paste an image URL as your target to enable reverse image search links.")
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

    if "," in target:
        name_quoted = target.split(",")[0].strip()
    else:
        name_quoted = target.strip()
    name_plus = name_quoted.replace(" ", "+")
    parts = name_quoted.split()
    first = parts[0] if parts else target
    last = parts[-1] if len(parts) > 1 else ""

    lines = []
    lines.append(f"TARGET: {name_quoted}")
    lines.append("")

    lines.append("=" * 50)
    lines.append("FACEBOOK INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    fb_searches = [
        ("People Search",    f"https://www.facebook.com/search/people/?q={name_plus}"),
        ("Posts mentioning", f"https://www.facebook.com/search/posts/?q={name_plus}"),
        ("Photos tagged",    f"https://www.facebook.com/search/photos/?q={name_plus}"),
        ("Check-ins",        f"https://www.facebook.com/search/places/?q={name_plus}"),
        ("Groups",           f"https://www.facebook.com/search/groups/?q={name_plus}"),
        ("Events",           f"https://www.facebook.com/search/events/?q={name_plus}"),
        ("Marketplace",      f"https://www.facebook.com/marketplace/search/?query={name_plus}"),
    ]
    for label, url in fb_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("INSTAGRAM INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    ig_searches = [
        ("Profile Search",   f"https://www.instagram.com/explore/search/keyword/?q={name_plus}"),
        ("Hashtag Search",   f"https://www.instagram.com/explore/tags/{name_plus.replace('+','')}/"),
        ("Google IG Search", f"https://www.google.com/search?q=site:instagram.com+%22{name_plus}%22"),
    ]
    for label, url in ig_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("TWITTER/X INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    tw_searches = [
        ("People Search",  f"https://twitter.com/search?q=%22{name_plus}%22&f=user"),
        ("Recent Posts",   f"https://twitter.com/search?q=%22{name_plus}%22&f=live"),
        ("Top Posts",      f"https://twitter.com/search?q=%22{name_plus}%22&f=top"),
        ("With Location",  f"https://twitter.com/search?q=%22{name_plus}%22+near%3A%22Albuquerque%22"),
    ]
    for label, url in tw_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("LINKEDIN INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    li_searches = [
        ("People Search",    f"https://www.linkedin.com/search/results/people/?keywords={name_plus}"),
        ("Posts Search",     f"https://www.linkedin.com/search/results/content/?keywords={name_plus}"),
        ("Google LI Search", f"https://www.google.com/search?q=site:linkedin.com/in+%22{name_plus}%22"),
    ]
    for label, url in li_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("TIKTOK INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    tt_searches = [
        ("User Search",      f"https://www.tiktok.com/search/user?q={name_plus}"),
        ("Video Search",     f"https://www.tiktok.com/search?q={name_plus}"),
        ("Google TT Search", f"https://www.google.com/search?q=site:tiktok.com+%22{name_plus}%22"),
    ]
    for label, url in tt_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("YOUTUBE INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    yt_searches = [
        ("Channel Search", f"https://www.youtube.com/results?search_query={name_plus}&sp=EgIQAg%253D%253D"),
        ("Video Search",   f"https://www.youtube.com/results?search_query={name_plus}"),
    ]
    for label, url in yt_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("REDDIT INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    rd_searches = [
        ("User Search",  f"https://www.reddit.com/search/?q=%22{name_quoted}%22&type=user"),
        ("Posts Search", f"https://www.reddit.com/search/?q=%22{name_quoted}%22"),
    ]
    for label, url in rd_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("SNAPCHAT / OTHER PLATFORMS")
    lines.append("=" * 50)
    lines.append("")
    other_searches = [
        ("Snapchat",  f"https://www.snapchat.com/add/{first.lower()}{last.lower()}"),
        ("Pinterest", f"https://www.pinterest.com/search/people/?q={name_plus}"),
        ("Tumblr",    f"https://www.tumblr.com/search/{name_plus}"),
        ("Nextdoor",  "https://nextdoor.com/find-neighbors/"),
        ("Meetup",    f"https://www.meetup.com/find/?keywords={name_plus}"),
        ("Venmo",     f"https://venmo.com/{first.lower()}{last.lower()}"),
        ("Cash App",  f"https://cash.app/${first.lower()}{last.lower()}"),
    ]
    for label, url in other_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("LOCATION INDICATOR SEARCHES")
    lines.append("=" * 50)
    lines.append("")
    location_searches = [
        ("FB Check-ins NM",  f"https://www.facebook.com/search/places/?q={name_plus}+new+mexico"),
        ("Twitter Near ABQ", f"https://twitter.com/search?q=%22{name_plus}%22+near%3AAlbuquerque&f=live"),
        ("Google Maps",      f"https://www.google.com/maps/search/{name_plus}"),
        ("Nextdoor NM",      "https://nextdoor.com/find-neighbors/"),
        ("Yelp Reviews",     f"https://www.yelp.com/search?find_desc={name_plus}"),
    ]
    for label, url in location_searches:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

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

    out, err, rc = run_cmd(f"python3 -m holehe {target} --only-used 2>/dev/null", timeout=120)
    if out and "holehe" not in out.lower() and "error" not in out.lower():
        lines.append("=" * 50)
        lines.append("HOLEHE — ACCOUNT DETECTION (120+ SITES)")
        lines.append("=" * 50)
        lines.append("")
        lines.append(out)
        lines.append("")

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

    try:
        domain = target.split("@")[1]
        lines.append("=" * 50)
        lines.append(f"EMAIL DOMAIN INTEL: {domain}")
        lines.append("=" * 50)
        lines.append("")
        dns_out, _, _ = run_cmd(f"dig +short A {domain} 2>/dev/null")
        mx_out, _, _ = run_cmd(f"dig +short MX {domain} 2>/dev/null")
        if dns_out:
            lines.append(f"Domain IP:   {dns_out.split()[0]}")
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
        ("Facebook",  f"https://www.facebook.com/search/people/?q={target}"),
        ("LinkedIn",  f"https://www.linkedin.com/search/results/people/?keywords={target}"),
        ("Twitter/X", f"https://twitter.com/search?q={target}&f=user"),
        ("Gravatar",  f"https://en.gravatar.com/{target}"),
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
        ("Bernalillo County Assessor", "https://assessor.bernco.gov/public.access/search/commonsearch.aspx?mode=owner"),
        ("Sandoval County Assessor",   "https://www.sandovalcountynm.gov/assessor/property-search/"),
        ("Santa Fe County Assessor",   "https://www.santafecountynm.gov/assessor"),
        ("Valencia County Assessor",   "https://www.co.valencia.nm.us/assessor"),
        ("Dona Ana County Assessor",   "https://assessor.donaanacounty.org/"),
        ("Chavez County Assessor",     "https://www.chaves.nm.us/departments/assessor"),
        ("NM All Counties",            "https://www.nmcourts.gov/"),
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
        ("PropertyShark",                "https://www.propertyshark.com/mason/property-search/us/"),
        ("Attom Data",                   "https://www.attomdata.com/"),
        ("County Office",                f"https://www.countyoffice.org/property-records-search/?q={name_plus}"),
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
        ("NM Taxation & Revenue",     "https://tap.state.nm.us/tap/_/"),
        ("Federal Tax Liens (PACER)", "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("UCC Filings NM",            "https://portal.sos.state.nm.us/BFS/online/UCCFilings/SearchUCC"),
        ("Bankruptcy Search",         "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
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
            ("Google Reverse Image",    f"https://images.google.com/searchbyimage?image_url={target}"),
            ("TinEye",                  f"https://tineye.com/search?url={target}"),
            ("Yandex (Best for faces)", f"https://yandex.com/images/search?url={target}&rpt=imageview"),
            ("Bing Visual Search",      f"https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:{target}"),
            ("Lenso.ai (Face Search)",  f"https://lenso.ai/en?url={target}"),
            ("PimEyes (Face Search)",   "https://pimeyes.com/en"),
        ]
    else:
        rev = [
            ("Google Reverse Image",    "https://images.google.com/"),
            ("TinEye",                  "https://tineye.com/"),
            ("Yandex (Best for faces)", "https://yandex.com/images/"),
            ("Bing Visual Search",      "https://www.bing.com/images/search?view=detailv2&iss=sbi"),
            ("Lenso.ai (Face Search)",  "https://lenso.ai/en"),
            ("PimEyes (Face Search)",   "https://pimeyes.com/en"),
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
        ("Jeffrey EXIF Viewer",            "http://exif.regex.info/exif.cgi"),
        ("ExifTool Online",                "https://exiftool.org/"),
        ("Metadata2Go",                    "https://www.metadata2go.com/"),
        ("FotoForensics (manipulation)",   "https://fotoforensics.com/"),
        ("Forensically (clone detection)", "https://29a.ch/photo-forensics/"),
        ("ImageEdited (edit detect)",      "https://imageedited.com/"),
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
        ("InVID WeVerify",   "https://www.invid-project.eu/tools-and-services/invid-verification-plugin/"),
        ("YouTube DataViewer", "https://citizenevidence.amnestyusa.org/"),
        ("TrueMedia.org",    "https://www.truemedia.org/"),
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
        ("Google Maps",        f"https://www.google.com/maps/search/{loc_plus}"),
        ("Google Street View", f"https://www.google.com/maps?q={loc_plus}&layer=c"),
        ("Google Earth Web",   f"https://earth.google.com/web/search/{loc_plus}"),
        ("Bing Maps",          f"https://www.bing.com/maps?q={loc_plus}"),
        ("OpenStreetMap",      f"https://www.openstreetmap.org/search?query={loc_plus}"),
        ("Apple Maps",         f"https://maps.apple.com/?q={loc_plus}"),
        ("Waze",               f"https://www.waze.com/en/live-map/directions?to=ll.{loc_plus}"),
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
        ("Wigle.net (WiFi Networks)", "https://wigle.net/search#fullSearch"),
        ("Overpass Turbo",            "https://overpass-turbo.eu/"),
        ("What3Words",                f"https://what3words.com/{loc_plus.replace('+','.')}"),
        ("FlightAware (Aircraft)",    f"https://flightaware.com/live/airport/{loc_plus}"),
        ("MarineTraffic (Ships)",     "https://www.marinetraffic.com/en/ais/home/centerx:-87/centery:20/zoom:4"),
        ("SunCalc (Sun Position)",    "https://www.suncalc.org/"),
        ("CalcMaps (Distance/Area)",  "https://www.calcmaps.com/map-distance/"),
    ]
    for name, url in special:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

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

    out, _, _ = run_cmd(f"python3 -m sherlock {target} --timeout 8 2>/dev/null", timeout=120)
    if out and "not found" not in out.lower():
        lines.append("[SHERLOCK — 300+ PLATFORMS]")
        lines.append(out)
        lines.append("")

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
        ("WhatsMyName",      f"https://whatsmyname.app/?q={target}"),
        ("Namechk",          f"https://namechk.com/{target}"),
        ("UserSearch.org",   f"https://usersearch.org/results_normal.php?q={target}"),
        ("CheckUsernames",   f"https://checkusernames.com/?q={target}"),
        ("KnowEm",           f"https://knowem.com/checkusernames.php?u={target}"),
        ("Instant Username", f"https://instantusername.com/#/{target}"),
        ("Sherlock Web",     "https://sherlock-project.github.io/"),
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
        ("Discord Lookup", "https://discord.id/"),
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
        ("FamilyTreeNow (FREE — best)", f"https://www.familytreenow.com/search/people/results?first={wp_first}&last={wp_last}"),
        ("TruePeopleSearch",            f"https://www.truepeoplesearch.com/results?name={name_plus}"),
        ("FastPeopleSearch",            f"https://www.fastpeoplesearch.com/name/{name_part.replace(' ','-').lower()}"),
        ("ClustrMaps",                  f"https://clustrmaps.com/person/{wp_last}-{wp_first}/"),
        ("SearchPeopleFree",            f"https://www.searchpeoplefree.com/find/{wp_first}-{wp_last}"),
        ("Nuwber",                      f"https://nuwber.com/search?firstName={wp_first}&lastName={wp_last}"),
        ("PublicRecords.Online",        f"https://publicrecords.online/search/?first_name={wp_first}&last_name={wp_last}"),
        ("PublicRecordsNow",            "https://www.publicrecordsnow.com/"),
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
        ("NM Courts (CourtLook)",          "https://caselookup.nmcourts.gov/caselookup/app"),
        ("PACER Federal Courts",           "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("CourtListener (Free Federal)",   f"https://www.courtlistener.com/?q={name_plus}&type=p"),
        ("VINE Offender Search NM",        "https://vinelink.vineapps.com/search/NM/Person"),
        ("NM Corrections Inmate",          "https://www.cd.nm.gov/divisions/oid/offender-search/"),
        ("ArrestFacts",                    f"https://arrestfacts.com/search?name={name_plus}"),
        ("BustedMugshots",                 f"https://bustedmugshots.com/search?name={name_plus}"),
        ("MugshotSearch",                  f"https://www.mugshots.com/search?q={name_plus}"),
        ("OpenSanctions Watchlist",        f"https://www.opensanctions.org/search/?q={name_plus}"),
        ("Sex Offender Registry NM",       "https://www.nmsexoffender.dps.nm.gov/"),
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
        ("FamilySearch (Free)",      f"https://www.familysearch.org/search/record/results?q.givenName={wp_first}&q.surname={wp_last}"),
        ("Ancestry (limited free)",  f"https://www.ancestry.com/search/?name={wp_first}_{wp_last}"),
        ("FindAGrave",               f"https://www.findagrave.com/memorial/search?firstname={wp_first}&lastname={wp_last}"),
        ("BillionGraves",            f"https://billiongraves.com/search/results/#firstname={wp_first}&lastname={wp_last}"),
        ("Legacy.com Obituaries",    f"https://www.legacy.com/obituaries/search?keyword={name_plus}"),
        ("NamUs Missing Persons",    "https://www.namus.gov/MissingPersons/Search#/results"),
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
        ("NM License Lookup",        "https://www.rld.nm.gov/licensing-and-regulation/"),
        ("NM Medical Board",         "https://www.nmmb.state.nm.us/"),
        ("NM Bar Association",       "https://www.nmbar.org/"),
        ("License.IQVIA (National)", "https://www.iqvia.com/"),
        ("NPPES (Medical NPI)",      f"https://npiregistry.cms.hhs.gov/search?search_type=ind&first_name={wp_first}&last_name={wp_last}"),
        ("BLS License Lookup",       "https://www.careeronestop.org/Toolkit/Credentials/find-licenses.aspx"),
    ]
    for name, url in licenses:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

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


# ── Module Registry ───────────────────────────────────────────────────────────
# NOTE: sherlock is intentionally NOT listed here as a standalone entry.
# It runs inside module_username_search to avoid double execution.

MODULE_MAP = {
    "people":              module_people_search,
    "property":            module_property,
    "photo_forensics":     module_photo_forensics,
    "geolocation":         module_geolocation,
    "username_search":     module_username_search,
    "public_records":      module_public_records,
    "social_media":        module_social_media,
    "business":            module_business,
    "plate_lookup":        module_plate_lookup,
    "vin_investigation":   module_vin_investigation,
    "image_metadata":      module_image_metadata,
    "email_investigate":   module_email_investigate,
    "phone":               module_phone,
    "whois":               module_whois,
    "dns":                 module_dns,
    "nmap":                module_nmap,
    "geoip":               module_geoip,
    "shodan":              module_shodan,
    "virustotal":          module_virustotal,
    "dorks":               module_google_dorks,
}

# Modules that only make sense for domain/IP targets
DOMAIN_IP_MODULES = {"whois", "dns", "nmap", "geoip", "shodan", "virustotal"}

# Modules that only make sense for person targets
PERSON_ONLY_MODULES = {"people", "public_records", "property", "plate_lookup"}


def run_investigation(job_id, target, target_type, selected_modules):
    try:
        # Prune stale jobs older than 1 hour to prevent memory leak
        cutoff = time.time() - 3600
        stale = [
            jid for jid, j in list(jobs.items())
            if datetime.fromisoformat(j["started"]).timestamp() < cutoff
        ]
        for jid in stale:
            del jobs[jid]

        # Gate modules by target type
        tt = (target_type or "PERSON").upper()

        if tt == "PERSON":
            selected_modules = [m for m in selected_modules if m not in DOMAIN_IP_MODULES]
        elif tt in ("DOMAIN", "IP"):
            selected_modules = [m for m in selected_modules if m not in PERSON_ONLY_MODULES]

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


# ── Authentication ────────────────────────────────────────────────────────────
import secrets

active_sessions = {}

def get_users():
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
    data = request.json
    token = data.get("token", "")
    session = active_sessions.get(token)
    if not session or session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    return jsonify({"message": "Audit log is written to Render server logs. Check Render dashboard → Logs."})


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/api/investigate", methods=["GET", "POST"])
def investigate():
    if request.method == "POST":
        data = request.json or {}
    else:
        data = request.args
    target = data.get("target", "").strip()
    target_type = data.get("type", "PERSON")
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
