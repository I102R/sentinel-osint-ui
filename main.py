"""
SENTINEL OSINT Agent - Backend Server
Runs on Render.com (Python 3.10+)
"""

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import subprocess, threading, json, os, time, queue, socket
from datetime import datetime

app = Flask(__name__)
CORS(app)

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

    if "," in target:
        name_part = target.split(",")[0].strip()
        location_part = target.split(",")[1].strip()
    else:
        parts = target.split()
        name_part = " ".join(parts[:2]) if len(parts) >= 2 else target
        location_part = " ".join(parts[2:]) if len(parts) > 2 else ""

    wp_first = name_part.split()[0] if name_part else ""
    wp_last = name_part.split()[-1] if len(name_part.split()) > 1 else ""
    wp_city = location_part.split()[0] if location_part else ""
    wp_state = location_part.split()[-1] if location_part else ""
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
        ("TRUEPEOPLESEARCH", f"https://www.truepeoplesearch.com/results?name={name_plus}&citystatezip={loc_plus}"),
        ("FASTPEOPLESEARCH", f"https://www.fastpeoplesearch.com/name/{name_url}"),
        ("WHITEPAGES",       f"https://www.whitepages.com/name/{wp_first}-{wp_last}/{wp_city}-{wp_state}"),
        ("SPOKEO",           f"https://www.spokeo.com/{name_part.replace(' ','-')}"),
        ("BEENVERIFIED",     f"https://www.beenverified.com/people/{name_url}/"),
        ("INTELIUS",         f"https://intelius.com/people-search/results/?firstName={wp_first}&lastName={wp_last}&city={wp_city}&state={wp_state}"),
        ("PEOPLEFINDERS",    f"https://www.peoplefinders.com/people/{name_url}"),
        ("USPHONEBOOK",      f"https://www.usphonebook.com/{name_part.replace(' ','-')}"),
        ("411.COM",          f"https://www.411.com/name/{wp_first}-{wp_last}/{wp_city}-{wp_state}"),
        ("PEOPLELOOKER",     f"https://www.peoplelooker.com/results/people?firstName={wp_first}&lastName={wp_last}&city={wp_city}&state={wp_state}"),
        ("RADARIS",          f"https://radaris.com/p/{wp_first}-{wp_last}/"),
        ("MYLIFE",           f"https://www.mylife.com/people-search/searchPeople.pubview?firstName={wp_first}&lastName={wp_last}&city={wp_city}&state={wp_state}"),
        ("VOTERRECORDS",     f"https://voterrecords.com/voters/{name_url}/1"),
        ("CHECKPEOPLE",      f"https://checkpeople.com/search?firstName={wp_first}&lastName={wp_last}&state={wp_state}"),
        ("CLUBSET",          f"https://www.clubset.com/profile/name/{wp_first}+{wp_last}"),
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



def module_social_media(target, job_id):
    emit(job_id, "module_start", {"module": "social_media"})
    
    name_plus = target.replace(" ", "+").replace(",", "")
    name_quoted = target.replace(",", "").strip()
    parts = name_quoted.split()
    first = parts[0] if parts else target
    last = parts[-1] if len(parts) > 1 else ""
    
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
        ("Snapchat",           f"https://www.snapchat.com/add/{first.lower()}{last.lower()}"),
        ("Pinterest",          f"https://www.pinterest.com/search/people/?q={name_plus}"),
        ("Tumblr",             f"https://www.tumblr.com/search/{name_plus}"),
        ("Nextdoor",           f"https://nextdoor.com/find-neighbors/"),
        ("Meetup",             f"https://www.meetup.com/find/?keywords={name_plus}"),
        ("Venmo",              f"https://venmo.com/{first.lower()}{last.lower()}"),
        ("Cash App",           f"https://cash.app/${first.lower()}{last.lower()}"),
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
            f"curl -s 'https://emailrep.io/{target}' -H 'User-Agent: sentinel-osint' 2>/dev/null",
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
        f"curl -s 'https://emailrep.io/{target}' -H 'User-Agent: sentinel-osint' 2>/dev/null"
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
            f"-H 'hibp-api-key: {api_key}' -H 'User-Agent: sentinel-osint' 2>/dev/null"
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

# ── Module registry ───────────────────────────────────────────────────────────
MODULE_MAP = {
    "people":       module_people_search,
    "social_media":  module_social_media,
    "email_investigate": module_email_investigate,
    "phone":        module_phone,
    "whois":        module_whois,
    "dns":          module_dns,
    "subdomains":   module_subdomains,
    "nmap":         module_nmap,
    "geoip":        module_geoip,
    "theharvester": module_theharvester,
    "sherlock":     module_sherlock,
    "shodan":       module_shodan,
    "virustotal":   module_virustotal,
    "emailrep":     module_emailrep,
    "hibp":         module_haveibeenpwned,
    "metadata":     module_metadata,
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

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/api/investigate", methods=["POST"])
def investigate():
    data = request.json
    target = data.get("target", "").strip()
    target_type = data.get("type", "DOMAIN")
    selected_modules = data.get("modules", list(MODULE_MAP.keys()))
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
    return "SENTINEL OSINT Backend running. Connect your frontend."

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
