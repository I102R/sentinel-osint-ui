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
