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

def module_whois(target, job_id):
    emit(job_id, "module_start", {"module": "whois"})
    out, err, rc = run_cmd(f"whois {target} 2>/dev/null | head -60")
    result = out if out else f"No WHOIS data found. {err}"
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
        result = "Add SHODAN_API_KEY to Render Environment Variables.\nFree key at https://shodan.io"
    else:
        out, err, _ = run_cmd(f"shodan host {target} 2>/dev/null")
        result = out if out else f"Shodan: {err}"
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
    out, _, _ = run_cmd(f"curl -s 'https://ipapi.co/{target}/json/' 2>/dev/null")
    try:
        data = json.loads(out)
        lines = [
            f"IP:       {data.get('ip', target)}",
            f"City:     {data.get('city', 'N/A')}",
            f"Region:   {data.get('region', 'N/A')}",
            f"Country:  {data.get('country_name', 'N/A')}",
            f"Org/ISP:  {data.get('org', 'N/A')}",
            f"Timezone: {data.get('timezone', 'N/A')}",
            f"Lat/Lon:  {data.get('latitude', 'N/A')}, {data.get('longitude', 'N/A')}",
        ]
        result = "\n".join(lines)
    except:
        result = out if out else "GeoIP lookup failed."
    emit(job_id, "module_done", {"module": "geoip", "result": result})
    return result

def module_virustotal(target, job_id):
    emit(job_id, "module_start", {"module": "virustotal"})
    api_key = os.environ.get("VT_API_KEY", "")
    if not api_key:
        result = "Add VT_API_KEY to Render Environment Variables.\nFree key at https://virustotal.com"
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
            result = (
                f"Malicious:  {stats.get('malicious', 0)}\n"
                f"Suspicious: {stats.get('suspicious', 0)}\n"
                f"Harmless:   {stats.get('harmless', 0)}\n"
                f"Reputation: {attrs.get('reputation', 'N/A')}"
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
            f"Email:       {data.get('email', target)}",
            f"Reputation:  {data.get('reputation', 'N/A')}",
            f"Suspicious:  {data.get('suspicious', 'N/A')}",
            f"References:  {data.get('references', 'N/A')}",
            f"Blacklisted: {details.get('blacklisted', False)}",
            f"Data breach: {details.get('data_breach', False)}",
            f"Disposable:  {details.get('disposable', False)}",
            f"Free provider: {details.get('free_provider', False)}",
            f"Profiles:    {', '.join(details.get('profiles', [])) or 'None found'}",
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
