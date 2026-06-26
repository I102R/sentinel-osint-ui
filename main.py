"""
FIVE T OSINT Agent - Backend Server
Runs on Render.com (Python 3.10+)
All modules audited June 2026 — paywall tools removed, free tools verified.
v2.1 — Full URL audit: parameterized where possible, MANUAL labels on form-entry tools,
        duplicates removed, no stone left unturned.
"""

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import subprocess, threading, json, os, time, queue, socket, re
from datetime import datetime
from urllib.parse import quote_plus

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

# ── Name Parser ───────────────────────────────────────────────────────────────
def parse_name_location(target):
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
    first = name_words[0] if name_words else ""
    last = name_words[-1] if len(name_words) > 1 else ""
    loc_words = location_part.split() if location_part else []
    state = loc_words[-1].upper() if loc_words else ""
    city = " ".join(loc_words[:-1]) if len(loc_words) > 1 else loc_words[0] if loc_words else ""
    return name_part, location_part, first, last, state, city

# ── Additional Identifiers Parser ─────────────────────────────────────────────
def parse_identifiers(data):
    ids = {
        "dob_month":  str(data.get("dob_month", "")).strip(),
        "dob_day":    str(data.get("dob_day", "")).strip(),
        "dob_year":   str(data.get("dob_year", "")).strip(),
        "ssn_type":   str(data.get("ssn_type", "")).strip().lower(),
        "ssn_value":  str(data.get("ssn_value", "")).strip(),
        "oln_number": str(data.get("oln_number", "")).strip(),
        "oln_state":  str(data.get("oln_state", "NM")).strip().upper(),
        "employer":   str(data.get("employer", "")).strip(),
        "street":     str(data.get("street", "")).strip(),
        "zip":        str(data.get("zip", "")).strip(),
    }
    raw_ssn = ids["ssn_value"]
    if raw_ssn:
        if ids["ssn_type"] == "last4" and len(raw_ssn) >= 4:
            ids["ssn_display"] = f"XXX-XX-{raw_ssn[-4:]}"
            ids["ssn_last4"] = raw_ssn[-4:]
        elif ids["ssn_type"] == "first5" and len(raw_ssn) >= 5:
            ids["ssn_display"] = f"{raw_ssn[:5]}-XX-XXXX"
            ids["ssn_last4"] = ""
        else:
            clean = raw_ssn.replace("-","")
            ids["ssn_display"] = f"XXX-XX-{clean[-4:]}" if len(clean) >= 4 else "XXX-XX-XXXX"
            ids["ssn_last4"] = clean[-4:] if len(clean) >= 4 else ""
    else:
        ids["ssn_display"] = ""
        ids["ssn_last4"] = ""
    ids["ssn_value"] = ""
    return ids

# ── Dork Builder ──────────────────────────────────────────────────────────────
def build_dorks(name_part, location_part, state, city, ids):
    n = f'"{name_part}"'
    loc = f'"{location_part}"' if location_part else ""
    st  = f'"{state}"' if state else ""
    yr  = ids.get("dob_year", "")
    oln = ids.get("oln_number", "")
    emp = ids.get("employer", "")
    street = ids.get("street", "")
    zip_   = ids.get("zip", "")
    ssn_last4 = ids.get("ssn_last4", "")
    ssn_type  = ids.get("ssn_type", "")

    people = [
        ("Base identity",        f'{n} {loc}'.strip(),                                           ""),
        ("Address search",       f'{n} {loc} address OR "street address"'.strip(),               ""),
        ("Phone development",    f'{n} {st} phone OR "phone number"'.strip(),                    ""),
        ("FamilyTreeNow",        f'{n} site:familytreenow.com',                                  ""),
        ("TruePeopleSearch",     f'{n} site:truepeoplesearch.com',                               ""),
        ("Relative development", f'{n} {loc} family OR relative OR spouse OR wife OR husband'.strip(), ""),
        ("Obituary check",       f'{n} obituary OR memorial OR "passed away"',                   ""),
    ]
    if zip_:
        people.append(("ZIP anchor", f'{n} "{zip_}"', ""))
    if yr:
        people.append(("DOB year",     f'{n} "{yr}" {loc}'.strip(), "[DOB]"))
        people.append(("DOB + state",  f'{n} "{yr}" {st}'.strip(),  "[DOB]"))
    if emp:
        people.append(("Employer", f'{n} "{emp}" {loc}'.strip(), "[EMPLOYER]"))

    skip = [
        ("Current address",  f'{n} {st} "current address" OR "lives at" OR resides'.strip(), ""),
        ("Voter reg",        f'{n} "voter registration" "New Mexico"',                        ""),
        ("FEC employer",     f'{n} {loc} employer OR "works at" OR "employed by"'.strip(),    ""),
        ("Property owner",   f'{n} "property owner" "New Mexico"',                            ""),
        ("Property deed",    f'{n} "real estate" OR deed OR parcel {loc}'.strip(),             ""),
        ("Foreclosure lien", f'{n} foreclosure OR lien OR "tax lien" {st}'.strip(),            ""),
        ("Business BBB",     f'{n} {loc} site:bbb.org OR site:yelp.com'.strip(),               ""),
        ("NM SOS business",  f'{n} site:sos.nm.gov',                              ""),
        ("Gov contractor",   f'{n} site:governmentcontracts.us OR site:usaspending.gov',      ""),
    ]
    if street:
        skip.append(("Street anchor", f'{n} "{street}" {loc}'.strip(), ""))
    if yr:
        skip.append(("DOB + address", f'{n} "{yr}" {st} address'.strip(), "[DOB]"))
    if oln and len(oln) >= 4:
        skip.append(("OLN court check", f'"{oln}" site:caselookup.nmcourts.gov', "[OLN]"))
        skip.append(("OLN general",     f'"{oln}" {n}',                           "[OLN]"))

    social = [
        ("Facebook",         f'{n} site:facebook.com',                              ""),
        ("Instagram",        f'{n} site:instagram.com',                             ""),
        ("Twitter/X",        f'{n} site:twitter.com',                               ""),
        ("LinkedIn + state", f'{n} site:linkedin.com {st}'.strip(),                 ""),
        ("TikTok",           f'{n} site:tiktok.com',                                ""),
        ("YouTube",          f'{n} site:youtube.com',                               ""),
        ("Reddit",           f'{n} site:reddit.com',                                ""),
        ("Nextdoor",         f'{n} site:nextdoor.com',                              ""),
        ("Email discovery",  f'{n} "@gmail.com" OR "@yahoo.com" OR "@outlook.com"', ""),
    ]
    if emp:
        social.append(("LinkedIn + employer", f'{n} "{emp}" site:linkedin.com', "[EMPLOYER]"))

    court = [
        ("General court",    f'{n} court OR lawsuit OR judgment OR plaintiff OR defendant {st}'.strip(), ""),
        ("Filed documents",  f'{n} deposition OR affidavit OR declaration OR "case no"',                ""),
        ("PDF records",      f'{n} {loc} filetype:pdf'.strip(),                                          ""),
        ("Justia",           f'{n} site:justia.com',                                                     ""),
        ("CourtListener",    f'{n} site:courtlistener.com',                                              ""),
        ("Google Scholar",   f'{n} site:scholar.google.com',                                            ""),
        ("Caselaw Access",   f'{n} site:case.law',                                                      ""),
        ("NM courts",        f'{n} site:caselookup.nmcourts.gov',                                       ""),
        ("Arrest — county anchor",  f'{n} arrest OR booking OR "booked into" {loc if loc else st}'.strip(), ""),
        ("Arrest — DOB disambig",   f'{n} "{yr}" arrest OR booking OR charges New Mexico'.strip() if yr else f'{n} arrest OR booking OR charges "New Mexico"', "[DOB]" if yr else ""),
        ("Arrest — JailBase",       f'{n} site:jailbase.com',                                               ""),
        ("Arrest — CriminalWatchdog", f'{n} site:criminalwatchdog.com',                                     ""),
        ("Arrest — LookWhoGotBusted", f'{n} site:lookwhogotbusted.com',                                       ""),
        ("Traffic/PI",       f'{n} accident OR collision OR crash OR "hit and run" OR DUI {st}'.strip(), ""),
        ("Spreadsheet",      f'{n} {loc} filetype:xlsx OR filetype:csv'.strip(),                         ""),
        ("Wayback deleted",  f'site:web.archive.org {n}',                                                ""),
        ("Wayback FB archive",f'site:web.archive.org facebook.com {n}',                                 ""),
    ]
    if yr:
        court.append(("DOB court filter", f'{n} "{yr}" court OR docket OR case'.strip(), "[DOB]"))
    if ssn_last4 and ssn_type == "last4":
        court.append(("SSN last4 confirm", f'{n} "{ssn_last4}"', "[SSN]"))

    return {"people": people, "skip": skip, "social": social, "court": court}


def render_dorks(dork_list, section_label):
    lines = []
    lines.append("=" * 50)
    lines.append(f"[ AUTO DORKS ] — {section_label}")
    lines.append("=" * 50)
    lines.append("")
    for label, query, badge in dork_list:
        tag = f"  {badge}" if badge else ""
        lines.append(f"  {label}{tag}")
        lines.append(f"  {query}")
        lines.append(f"  https://www.google.com/search?q={quote_plus(query)}")
        lines.append("")
    return lines


def render_identifier_sources(ids, name_part, first, last):
    lines = []
    yr        = ids.get("dob_year","")
    dob_m     = ids.get("dob_month","")
    dob_d     = ids.get("dob_day","")
    ssn_type  = ids.get("ssn_type","")
    ssn_disp  = ids.get("ssn_display","")
    ssn_last4 = ids.get("ssn_last4","")
    oln       = ids.get("oln_number","")
    oln_state = ids.get("oln_state","NM")

    has_dob = bool(yr or dob_m)
    has_ssn = bool(ssn_disp)
    has_oln = bool(oln and len(oln) >= 4)

    if not (has_dob or has_ssn or has_oln):
        return lines

    lines.append("=" * 50)
    lines.append("ADDITIONAL IDENTIFIER SOURCES")
    lines.append("=" * 50)
    lines.append("")

    if has_dob:
        lines.append("— DATE OF BIRTH —")
        lines.append("")
        if dob_m and dob_d and yr:
            dob_str = f"{dob_m}/{dob_d}/{yr}"
        elif dob_m and yr:
            dob_str = f"{dob_m}/{yr}"
        else:
            dob_str = yr
        lines.append(f"DOB on file: {dob_str}")
        lines.append("")
        lines.append("[NM Voter Portal — DOB exact match]  ⚑ MANUAL: enter name + DOB on site")
        lines.append("  https://voterportal.sos.nm.gov/WhereToVote.aspx")
        lines.append("  TIP: Highest-confidence free address source — DOB exact match eliminates false hits")
        lines.append("")
        lines.append("[FamilyTreeNow — DOB filter]")
        lines.append(f"  https://www.familytreenow.com/search/people/results?first={first}&last={last}&birthyear={yr}")
        lines.append("")
        lines.append("[TruePeopleSearch — DOB narrow]")
        lines.append(f"  https://www.truepeoplesearch.com/results?name={quote_plus(name_part)}&birthdate={dob_str}")
        lines.append("")
        lines.append("[NM CourtLook — DOB filter]  ⚑ MANUAL: enter name + DOB on site")
        lines.append("  https://caselookup.nmcourts.gov/caselookup/app")
        lines.append(f"  TIP: DOB {dob_str} eliminates same-name hits in court results")
        lines.append("")
        if yr:
            lines.append("[SSN Issuance Validator — cross-check DOB/origin]  ⚑ MANUAL: enter SSN prefix on site  [DOB]")
            lines.append("  https://www.ssn-check.org")
            lines.append(f"  TIP: Birth year {yr} — confirm SSN issue state matches subject origin")
            lines.append("")
        lines.append("[FindAGrave — deceased check]  [DOB]")
        lines.append(f"  https://www.findagrave.com/memorial/search?firstname={first}&lastname={last}&birthyear={yr}")
        lines.append("")
        lines.append("[Legacy.com — obituary filter]  [DOB]")
        lines.append(f"  https://www.legacy.com/obituaries/search?keyword={quote_plus(name_part)}")
        lines.append("")

    if has_ssn:
        lines.append("— SOCIAL SECURITY NUMBER —")
        lines.append("")
        lines.append(f"SSN on file: {ssn_disp}")
        lines.append(f"Type: {ssn_type.upper() if ssn_type else 'UNKNOWN'}")
        lines.append("")
        lines.append("[SSN Issuance State Lookup — free]  ⚑ MANUAL: enter SSN prefix on site")
        lines.append("  https://www.ssn-check.org")
        lines.append("  TIP: Confirms state of issuance + approximate year — validates subject age/origin")
        lines.append("")
        if ssn_type == "full":
            lines.append("[PACER — bankruptcy search]  ⚑ MANUAL: register free, search by SSN")
            lines.append("  https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf")
            lines.append("")
            lines.append("[NM Taxation & Revenue — TAP tax lien search]  ⚑ MANUAL: enter name/SSN on site")
            lines.append("  https://tap.state.nm.us/tap/_/")
            lines.append("")
            lines.append("[IRS Tax Lien Search]  ⚑ MANUAL: enter name on site")
            lines.append("  https://www.irs.gov/businesses/small-businesses-self-employed/search-for-a-lien")
            lines.append("")
            lines.append("[UCC Filings NM — debtor search]")
            lines.append("  https://www.sos.state.nm.us/businessservices/corporations/ucc.aspx")
            lines.append("")
        if ssn_last4:
            lines.append(f"[TruePeopleSearch — last-4 SSN filter ({ssn_last4})]")
            lines.append(f"  https://www.truepeoplesearch.com/results?name={quote_plus(name_part)}&ssn={ssn_last4}")
            lines.append("")
            lines.append(f"[ThatsThem — last-4 filter ({ssn_last4})]")
            lines.append(f"  https://thatsthem.com/name/{first.lower()}-{last.lower()}")
            lines.append(f"  TIP: Cross-reference last-4 {ssn_last4} against results returned")
            lines.append("")
        lines.append("⚠ DPPA NOTICE: SSN-sourced records require documented permissible use in case file.")
        lines.append("  18 U.S.C. § 2721(b) — litigation support / service of process.")
        lines.append("")

    if has_oln:
        lines.append("— DRIVER LICENSE / OLN —")
        lines.append("")
        lines.append(f"OLN on file: {oln}  State: {oln_state}")
        lines.append("")
        is_full_oln = len(oln) >= 8
        if is_full_oln:
            mvd_links = {
                "NM": ("NM MVD Record Request","https://www.mvd.newmexico.gov/driver-record-request/","(888) 683-4636"),
                "AZ": ("AZ MVD","https://www.azdot.gov/motor-vehicles","(602) 712-7355"),
                "TX": ("TX DMV","https://www.txdmv.gov/","(888) 368-4689"),
                "CO": ("CO DMV","https://dmv.colorado.gov/","(303) 205-5600"),
                "CA": ("CA DMV","https://www.dmv.ca.gov/","(800) 777-0133"),
            }
            mvd = mvd_links.get(oln_state, ("State MVD","https://www.vehiclehistory.gov/","Check state DMV"))
            lines.append(f"[{mvd[0]} — DPPA formal request]  ⚑ MANUAL: submit DPPA form  [OLN]")
            lines.append(f"  {mvd[1]}")
            lines.append(f"  Phone: {mvd[2]}")
            lines.append(f"  TIP: Submit DPPA permissible use request with OLN {oln}")
            lines.append("")
            lines.append("[AAMVA PDPS — national driver pointer system]  ⚑ MANUAL: DPPA certification required  [OLN]")
            lines.append("  https://www.aamva.org/technology/systems/pdps/")
            lines.append("  TIP: CDL holders and serious traffic offenders")
            lines.append("")
            lines.append("[LexisNexis C.L.U.E. — prior insurance claims]  ⚑ MANUAL: firm account required  [OLN]")
            lines.append(f"  TIP: OLN {oln} — prior claims history material to PI damages")
            lines.append("")
        lines.append("[NM CourtLook — traffic/DUI filter]  ⚑ MANUAL: enter OLN on site  [OLN]")
        lines.append("  https://caselookup.nmcourts.gov/caselookup/app")
        lines.append(f"  TIP: Search by OLN {oln} for traffic citations, DUI, reckless driving history")
        lines.append("")
        lines.append("[Trellis.law — OLN alternate identifier]  [OLN]")
        lines.append(f"  https://trellis.law/person/{first.lower()}-{last.lower()}")
        lines.append("")
        lines.append("⚠ DPPA NOTICE: OLN/MVR data requires documented permissible use.")
        lines.append("  18 U.S.C. § 2721(b) — litigation support / service of process.")
        lines.append("")

    return lines


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: PEOPLE SEARCH
# ══════════════════════════════════════════════════════════════════════════════

def module_people_search(target, job_id, ids=None):
    if ids is None: ids = {}
    emit(job_id, "module_start", {"module": "people"})
    name_part, location_part, first, last, state, city = parse_name_location(target)
    name_plus = quote_plus(name_part)
    loc_plus  = quote_plus(location_part)
    name_url  = name_part.replace(" ", "-").lower()
    lines = []
    lines.append(f"TARGET:   {name_part}")
    if location_part: lines.append(f"LOCATION: {location_part}")
    lines.append("")
    lines.append("=" * 50); lines.append("FREE PEOPLE FINDER SITES"); lines.append("=" * 50); lines.append("")
    sites = [
        ("FAMILYTREENOW",        f"https://www.familytreenow.com/search/people/results?first={first}&last={last}&state={state}"),
        ("TRUEPEOPLESEARCH",     f"https://www.truepeoplesearch.com/results?name={name_plus}&citystatezip={loc_plus}"),
        ("FASTPEOPLESEARCH",     f"https://www.fastpeoplesearch.com/name/{name_url}"),
        ("THATSTHEM",            f"https://thatsthem.com/name/{first}-{last}"),
        ("IDCRAWL",              f"https://www.idcrawl.com/name/{first}-{last}"),
        ("ZABASEARCH",           f"https://www.zabasearch.com/people/{first}+{last}/{state}/"),
        ("411.COM",              f"https://www.411.com/name/{first}-{last}/{state}"),
        ("USPHONEBOOK",          f"https://www.usphonebook.com/{first}-{last}"),
        ("VOTERRECORDS.COM",     f"https://voterrecords.com/voters/{name_url}/1"),
        ("PUBLICRECORDS.ONLINE", f"https://publicrecords.online/search/?first_name={first}&last_name={last}&state={state}"),
        ("RADARIS — partial free",    f"https://radaris.com/p/{first}/{last}/"),
        ("ADDRESSES.COM",             f"https://www.addresses.com/people/{first}-{last}"),
        ("WHITEPAGES — partial free", f"https://www.whitepages.com/name/{first}-{last}/{state}"),
    ]
    for nm, url in sites:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines.append("=" * 50); lines.append("SOCIAL MEDIA"); lines.append("=" * 50); lines.append("")
    for platform, url in [
        ("LinkedIn",  f"https://www.linkedin.com/search/results/people/?keywords={name_plus}"),
        ("Facebook",  f"https://www.facebook.com/search/people/?q={name_plus}"),
        ("Twitter/X", f"https://twitter.com/search?q=%22{name_plus}%22&f=user"),
        ("Instagram", f"https://www.instagram.com/explore/search/keyword/?q={name_plus}"),
        ("TikTok",    f"https://www.tiktok.com/search?q={name_plus}"),
        ("YouTube",   f"https://www.youtube.com/results?search_query={name_plus}"),
        ("Reddit",    f"https://www.reddit.com/search/?q=%22{name_part}%22&type=user"),
    ]:
        lines.append(f"[{platform}]"); lines.append(f"  {url}"); lines.append("")
    lines.append("=" * 50); lines.append("MULTI-PLATFORM LAUNCHERS"); lines.append("=" * 50); lines.append("")
    for nm, url in [
        ("OSINT Vault — 4,577 US public records sources", "https://theosintvault.io/"),
        ("OSINT Vault multi-search launcher — 80+ platforms", f"https://theosintvault.io/multi-search?q={name_plus}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines.append("=" * 50); lines.append("COURT & PUBLIC RECORDS"); lines.append("=" * 50); lines.append("")
    for nm, url in [
        ("NM Courts CourtLook — MANUAL: enter name",  "https://caselookup.nmcourts.gov/caselookup/app"),
        ("PACER Federal Courts — MANUAL: free acct",  "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("CourtListener Free",    f"https://www.courtlistener.com/?q={name_plus}&type=p"),
        ("OpenSanctions",         f"https://www.opensanctions.org/search/?q={name_plus}"),
        ("VINE Offender NM — MANUAL: enter name",     "https://vinelink.vineapps.com/search/NM/Person"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines.extend(render_identifier_sources(ids, name_part, first, last))
    dorks = build_dorks(name_part, location_part, state, city, ids)
    lines.extend(render_dorks(dorks["people"], "PEOPLE SEARCH"))
    try:
        san_out, _, _ = run_cmd(f"curl -s 'https://api.opensanctions.org/search/default?q={name_plus}&schema=Person' 2>/dev/null", timeout=10)
        san_data = json.loads(san_out); results = san_data.get("results",[])
        lines.append("=" * 50); lines.append("LIVE SANCTIONS / WATCHLIST CHECK"); lines.append("=" * 50); lines.append("")
        if results:
            lines.append(f"WARNING: {len(results)} MATCH(ES) FOUND")
            for r in results[:5]: lines.append(f"  * {r.get('caption','?')} -- Score: {r.get('score','?')}")
        else: lines.append("No matches found on sanctions/watchlists")
        lines.append("")
    except: pass
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "people", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: PUBLIC RECORDS
# ══════════════════════════════════════════════════════════════════════════════

def module_public_records(target, job_id, ids=None):
    if ids is None: ids = {}
    emit(job_id, "module_start", {"module": "public_records"})
    name_part, location_part, first, last, state, city = parse_name_location(target)
    name_plus = quote_plus(name_part)
    lines = [f"TARGET: {name_part}", ""]
    lines.append("=" * 50); lines.append("FREE PEOPLE & ADDRESS RECORDS"); lines.append("=" * 50); lines.append("")
    for nm, url in [
        ("JudyRecords 740M cases",       f"https://www.judyrecords.com/search?q={name_plus}"),
        ("FamilyTreeNow",                f"https://www.familytreenow.com/search/people/results?first={first}&last={last}"),
        ("TruePeopleSearch",             f"https://www.truepeoplesearch.com/results?name={name_plus}"),
        ("FastPeopleSearch",             f"https://www.fastpeoplesearch.com/name/{name_part.replace(' ','-').lower()}"),
        ("ThatsThem",                    f"https://thatsthem.com/name/{first}-{last}"),
        ("IDCrawl",                      f"https://www.idcrawl.com/name/{first}-{last}"),
        ("PublicRecords.Online",         f"https://publicrecords.online/search/?first_name={first}&last_name={last}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines.append("=" * 50); lines.append("ARREST & CRIMINAL RECORDS"); lines.append("=" * 50); lines.append("")
    for nm, url in [
        ("JudyRecords",                  f"https://www.judyrecords.com/search?q={name_plus}"),
        ("Trellis.law state courts",     f"https://trellis.law/person/{first}-{last}"),
        ("NM Courts CourtLook — MANUAL: enter name",  "https://caselookup.nmcourts.gov/caselookup/app"),
        ("PACER Federal Courts — MANUAL: free acct",  "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("CourtListener Federal",        f"https://www.courtlistener.com/?q={name_plus}&type=p"),
        ("UniCourt — 10 free/month",     f"https://unicourt.com/search#?q={name_plus}"),
        ("PlainSite — federal + corp",   f"https://www.plainsite.org/search/?q={name_plus}"),
        ("Google Scholar Case Law",      f"https://scholar.google.com/scholar?q={name_plus}&as_sdt=4,32"),
        ("Caselaw Access Project 1.7M+", f"https://www.case.law/search/#/search?q={name_plus}&jurisdiction=nm"),
        ("VINE Offender NM — MANUAL: enter name",     "https://vinelink.vineapps.com/search/NM/Person"),
        ("NM Corrections Inmate — MANUAL: enter name","https://www.cd.nm.gov/divisions/oid/offender-search/"),
        ("JailBase arrest bookings",     f"https://www.jailbase.com/search/?name_searched={name_plus}"),
        ("CriminalWatchdog",             f"https://www.criminalwatchdog.com/faq/search-results?fname={first}&lname={last}&state={state}"),
        ("LookWhoGotBusted",             f"https://lookwhogotbusted.com/?s={name_plus}"),
        ("OpenSanctions",                f"https://www.opensanctions.org/search/?q={name_plus}"),
        ("NM Sex Offender Registry — MANUAL: enter name", "https://www.dps.nm.gov/sex-offender-registry/"),
        ("National Sex Offender",        f"https://www.nsopw.gov/Search/Results?firstName={first}&lastName={last}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines.append("=" * 50); lines.append("VITAL RECORDS & GENEALOGY"); lines.append("=" * 50); lines.append("")
    for nm, url in [
        ("FamilySearch",   f"https://www.familysearch.org/search/record/results?q.givenName={first}&q.surname={last}"),
        ("Ancestry",       f"https://www.ancestry.com/search/?name={first}_{last}"),
        ("FindAGrave",     f"https://www.findagrave.com/memorial/search?firstname={first}&lastname={last}"),
        ("BillionGraves",  f"https://billiongraves.com/search/results/#firstname={first}&lastname={last}"),
        ("Legacy.com",     f"https://www.legacy.com/obituaries/search?keyword={name_plus}"),
        ("NamUs Missing Persons — MANUAL: enter name", "https://www.namus.gov/MissingPersons/Search#/results"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines.append("=" * 50); lines.append("ASSET RECORDS — AIRCRAFT & VESSELS"); lines.append("=" * 50); lines.append("")
    lines.append("FAA and USCG registrations are public federal records — no account, no cost.")
    lines.append("")
    for nm, url in [
        ("FAA Aircraft Registry — owner name search",     f"https://registry.faa.gov/aircraftinquiry/Search/OwnerSearch?OwnerName={name_plus}"),
        ("FAA Aircraft Registry — N-number lookup",       "https://registry.faa.gov/aircraftinquiry/Search/NNumberInquiry"),
        ("FAA Airmen Certification — pilot license",      f"https://amsrvs.registry.faa.gov/airmeninquiry/Main.aspx"),
        ("USCG Vessel Documentation — owner search",      f"https://cgmix.uscg.mil/vesselinfo/vesseldetails.aspx"),
        ("USCG CGMIX vessel search",                      f"https://cgmix.uscg.mil/vesselinfo/Default.aspx"),
        ("BoatInfoWorld — free vessel search",            f"https://www.boatinfoworld.com/searchvessel.asp?vesselname={name_plus}"),
        ("NM Boating — state registration — MANUAL",     "https://www.wildlife.state.nm.us/boating/"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines.append("=" * 50); lines.append("PROFESSIONAL LICENSES"); lines.append("=" * 50); lines.append("")
    for nm, url in [
        ("NM RLD License Search",   f"https://www.rld.nm.gov/licensing-and-regulation/licensee-search/?SearchName={quote_plus(name_part)}"),
        ("NM Medical Board — MANUAL: enter name",       "https://www.nmmb.state.nm.us/"),
        ("NM Bar Find a Lawyer",    f"https://nmbar.org/Nmbar/Find_A_Lawyer/NMBar/MembersClients/Find_a_Lawyer.aspx"),
        ("NPPES Medical NPI",       f"https://npiregistry.cms.hhs.gov/search?search_type=ind&first_name={first}&last_name={last}"),
        ("CareerOneStop License",   f"https://www.careeronestop.org/Toolkit/Credentials/find-licenses.aspx?keyword={quote_plus(name_part)}&location={state}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    dorks = build_dorks(name_part, location_part, state, city, ids)
    lines.extend(render_dorks(dorks["court"], "PUBLIC RECORDS"))
    try:
        out, _, _ = run_cmd(f"curl -s 'https://www.courtlistener.com/api/rest/v3/people/?name_last={last}&name_first={first}&format=json' 2>/dev/null", timeout=10)
        data = json.loads(out); count = data.get("count",0)
        if count > 0:
            lines += ["=" * 50, f"COURTLISTENER — {count} RECORD(S) FOUND", "=" * 50, ""]
            for r in data.get("results",[])[:3]:
                lines.append(f"  Name: {r.get('name_full','N/A')}"); lines.append(f"  URL:  https://www.courtlistener.com{r.get('absolute_url','')}"); lines.append("")
    except: pass
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "public_records", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: PROPERTY
# ══════════════════════════════════════════════════════════════════════════════

def module_property(target, job_id, ids=None):
    if ids is None: ids = {}
    emit(job_id, "module_start", {"module": "property"})
    name_part, location_part, first, last, state, city = parse_name_location(target)
    name_plus = quote_plus(name_part)
    lines = [f"TARGET: {name_part}"]
    if location_part: lines.append(f"LOCATION: {location_part}")
    lines.append("")
    # PropWire first — only free national tool that searches by owner name with results
    lines += ["=" * 50, "NATIONAL PROPERTY DATABASES — FREE", "=" * 50, ""]
    for nm, url in [
        ("PropWire — owner name search",       f"https://propwire.com/search?q={name_plus}"),
        ("County Office — owner search",       f"https://www.countyoffice.org/property-records-search/?q={name_plus}"),
        ("FamilyTreeNow — address history",    f"https://www.familytreenow.com/search/people/results?first={first}&last={last}"),
        ("NETR Online — all 50 states — MANUAL: select county", "https://publicrecords.netronline.com/state/NM"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "NEW MEXICO PROPERTY RECORDS — ALL 33 COUNTIES — MANUAL: enter owner name", "=" * 50, ""]
    lines.append("⚑ All county assessor portals require manual name entry. Click link, search by owner.")
    lines.append("")
    for nm, url in [
        ("Bernalillo County Assessor",  "https://assessor.bernco.gov/public.access/search/commonsearch.aspx?mode=owner"),
        ("Sandoval County Assessor",    "https://www.sandovalcountynm.gov/assessor/property-search/"),
        ("Santa Fe County Assessor",    "https://www.santafecountynm.gov/assessor"),
        ("Dona Ana County Assessor",    "https://assessor.donaanacounty.org/"),
        ("Valencia County Assessor",    "https://www.co.valencia.nm.us/assessor"),
        ("Chavez/Roswell Assessor",     "https://www.chavescounty.net/assessor/"),
        ("Lea County Assessor",         "https://www.leacounty.net/assessor/"),
        ("Otero County Assessor",       "https://www.co.otero.nm.us/assessor/"),
        ("San Juan County Assessor",    "https://www.sjcounty.net/departments/assessor"),
        ("McKinley County Assessor",    "https://www.co.mckinley.nm.us/assessor"),
        ("Eddy County Assessor",        "https://www.co.eddy.nm.us/137/Assessor"),
        ("Curry County Assessor",       "https://www.currycounty.org/assessor"),
        ("Roosevelt County Assessor",   "https://www.rooseveltcounty.com/assessor"),
        ("Sierra County Assessor",      "https://www.sierracountynm.org/assessor/"),
        ("Grant County Assessor",       "https://www.grantcountynm.gov/assessor"),
        ("Luna County Assessor",        "https://www.lunacountynm.us/assessor"),
        ("Hidalgo County Assessor",     "https://www.hidalgocounty.org/assessor/"),
        ("Socorro County Assessor",     "https://www.socorrocounty.org/assessor"),
        ("Lincoln County Assessor",     "https://www.lincolncountynm.net/assessor"),
        ("Torrance County Assessor",    "https://www.torrancecountynm.org/assessor"),
        ("Taos County Assessor",        "https://www.taoscounty.org/assessor"),
        ("Rio Arriba County Assessor",  "https://www.rio-arriba.org/assessor"),
        ("San Miguel County Assessor",  "https://www.sanmiguelcounty.org/assessor/"),
        ("Cibola County Assessor",      "https://www.co.cibola.nm.us/assessor/"),
        ("Los Alamos County Assessor",  "https://www.losalamosnm.us/assessor"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "TAX & LIENS — FREE", "=" * 50, ""]
    for nm, url in [
        ("NM Taxation & Revenue TAP — MANUAL: enter name/TIN",  "https://tap.state.nm.us/tap/_/"),
        ("Federal Tax Liens PACER — MANUAL: free acct",          "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("UCC Filings NM",                                        f"https://www.sos.state.nm.us/businessservices/corporations/ucc.aspx"),
        ("Bankruptcy PACER — MANUAL: free acct",                  "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    prop_dorks = [
        ("Property owner NM",  f'"{name_part}" property owner New Mexico', ""),
        ("Real estate deed",   f'"{name_part}" real estate deed', ""),
        ("Assessor parcel",    f'"{name_part}" assessor parcel', ""),
        ("Foreclosure lien",   f'"{name_part}" foreclosure lien', ""),
    ]
    if ids.get("zip"):   prop_dorks.append(("ZIP property",   f'"{name_part}" "{ids["zip"]}" property', ""))
    if ids.get("street"): prop_dorks.append(("Street anchor", f'"{name_part}" "{ids["street"]}"', ""))
    lines.extend(render_dorks(prop_dorks, "PROPERTY"))
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "property", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: SKIP TRACE
# ══════════════════════════════════════════════════════════════════════════════

def module_skip_trace(target, job_id, ids=None):
    if ids is None: ids = {}
    emit(job_id, "module_start", {"module": "skip_trace"})
    name_part, location_part, first, last, state, city = parse_name_location(target)
    name_plus = quote_plus(name_part)
    name_url  = name_part.replace(" ","-").lower()
    city_plus = quote_plus(city)
    lines = [f"TARGET:   {name_part}"]
    if location_part: lines.append(f"LOCATION: {location_part}")
    lines += ["", "DPPA: Law firms qualify under 18 U.S.C. 2721(b) for litigation & process serving.", ""]
    lines += ["=" * 50, "TIER 1 — FREE SOURCES (pre-loaded results)", "=" * 50, ""]
    for nm, url in [
        ("OSINT Vault multi-search — 80+ platforms", f"https://theosintvault.io/multi-search?q={name_plus}"),
        ("FamilyTreeNow",       f"https://www.familytreenow.com/search/people/results?first={first}&last={last}&state={state}"),
        ("TruePeopleSearch",    f"https://www.truepeoplesearch.com/results?name={name_plus}&citystatezip={city_plus}+{state}"),
        ("FastPeopleSearch",    f"https://www.fastpeoplesearch.com/name/{name_url}"),
        ("ThatsThem",           f"https://thatsthem.com/name/{first}-{last}"),
        ("IDCrawl",             f"https://www.idcrawl.com/name/{first}-{last}"),
        ("ZabaSearch",          f"https://www.zabasearch.com/people/{first}+{last}/{state}/"),
        ("411.com",             f"https://www.411.com/name/{first}-{last}/{state}"),
        ("USPhoneBook",         f"https://www.usphonebook.com/{first}-{last}"),
        ("PublicRecords.Online",f"https://publicrecords.online/search/?first_name={first}&last_name={last}&state={state}"),
        ("Radaris — partial free",   f"https://radaris.com/p/{first}/{last}/"),
        ("Whitepages — partial free",f"https://www.whitepages.com/name/{first}-{last}/{state}"),
        ("Addresses.com",            f"https://www.addresses.com/people/{first}-{last}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "TIER 2 — VOTER REGISTRATION (best free address source)", "=" * 50, ""]
    lines.append(f"[FEC Political Contributions]")
    lines.append(f"  https://www.fec.gov/data/receipts/individual-contributions/?contributor_name={name_plus}")
    lines.append("")
    voter_portals = {
        "AL":("AL Voter Status","https://myinfo.alabamavotes.gov/VoterView/RegistrantSearch.do"),
        "AK":("AK Voter Search","https://myvoterinformation.alaska.gov/"),
        "AZ":("AZ Voter Registration","https://my.arizona.vote/VoterView/RegistrantSearch.do"),
        "CA":("CA Voter Status","https://voterstatus.sos.ca.gov/"),
        "CO":("CO Voter Portal","https://www.sos.state.co.us/voter/pages/pub/olvr/findVoterReg.xhtml"),
        "FL":("FL Voter Lookup","https://registration.elections.myflorida.com/CheckVoterStatus"),
        "GA":("GA Voter Status","https://mvp.sos.ga.gov/s/"),
        "NM":("NM Voter Portal","https://voterportal.sos.nm.gov/WhereToVote.aspx"),
        "NY":("NY Voter Status","https://voterlookup.elections.ny.gov/"),
        "TX":("TX Voter Search","https://teamrv-mvp.sos.texas.gov/MVP/mvp.do"),
        "NC":("NC Voter Lookup","https://vt.ncsbe.gov/RegLkup/"),
        "PA":("PA Voter Status","https://www.pavoterservices.pa.gov/pages/voterregistrationstatus.aspx"),
        "VA":("VA Voter Lookup","https://vote.elections.virginia.gov/VoterInformation"),
        "WA":("WA Voter Status","https://voter.votewa.gov/WhereToVote.aspx"),
        "MI":("MI Voter Info","https://mvic.sos.state.mi.us/"),
        "WI":("WI Voter Lookup","https://myvote.wi.gov/en-us/"),
        "OR":("OR Voter Status","https://sos.oregon.gov/voting/pages/myvote.aspx"),
        "IL":("IL Voter Lookup","https://www.elections.il.gov/votinginformation/RegistrationLookup.aspx"),
        "IN":("IN Voter Search","https://indianavoters.in.gov/"),
        "OH":("OH Voter Search","https://voterlookup.ohiosos.gov/voterlookup.aspx"),
    }
    lines.append(f"[VoterRecords.com — all states]")
    lines.append(f"  https://voterrecords.com/voters/{name_url}/1")
    lines.append("")
    sp = voter_portals.get(state.upper() if state else "NM")
    if sp:
        lines.append(f"[{sp[0]} — MANUAL: enter name + DOB]")
        lines.append(f"  {sp[1]}")
        lines.append("")
    else:
        lines.append(f"[State Voter Portal Finder]")
        lines.append("  https://www.usa.gov/voter-registration-card")
        lines.append("")
    lines.append(f"[Google Voter Reg dork]")
    lines.append(f"  https://www.google.com/search?q=%22{name_plus}%22+%22voter+registration%22+%22{quote_plus(location_part)}%22")
    lines.append("")
    lines += ["=" * 50, "TIER 3 — RELATIVES & ASSOCIATES", "=" * 50, ""]
    for nm, url in [
        ("FamilyTreeNow relatives",  f"https://www.familytreenow.com/search/people/results?first={first}&last={last}"),
        ("TruePeopleSearch",         f"https://www.truepeoplesearch.com/results?name={name_plus}"),
        ("ThatsThem associates",     f"https://thatsthem.com/name/{first}-{last}"),
        ("IDCrawl connections",      f"https://www.idcrawl.com/name/{first}-{last}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "TIER 4 — ADDRESS VERIFICATION", "=" * 50, ""]
    for nm, url in [
        ("USPS ZIP+4 Lookup — MANUAL: enter address",       "https://tools.usps.com/zip-code-lookup.htm?byaddress"),
        ("Google Maps street verify",                        f"https://www.google.com/maps/search/{name_plus}+{city_plus}+{state}"),
        ("Bernalillo County Assessor — MANUAL: owner search","https://assessor.bernco.gov/public.access/search/commonsearch.aspx?mode=owner"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "TIER 5 — WORKPLACE & EMPLOYMENT", "=" * 50, ""]
    for nm, url in [
        ("LinkedIn",                   f"https://www.linkedin.com/search/results/people/?keywords={name_plus}&origin=GLOBAL_SEARCH_HEADER"),
        ("Google LinkedIn + state",    f"https://www.google.com/search?q=site:linkedin.com+%22{name_plus}%22+%22{state}%22"),
        ("Google employer dork",       f"https://www.google.com/search?q=%22{name_plus}%22+employer+OR+works+OR+%22employed+at%22"),
        ("FEC Political Donations",    f"https://www.fec.gov/data/receipts/individual-contributions/?contributor_name={name_plus}"),
        ("OpenSecrets donor search",   f"https://www.opensecrets.org/donor-lookup/results?name={name_plus}"),
        ("NM RLD Contractor License",  f"https://www.rld.nm.gov/licensing-and-regulation/licensee-search/?SearchName={quote_plus(name_part)}"),
        ("NPPES Medical NPI",          f"https://npiregistry.cms.hhs.gov/search?search_type=ind&first_name={first}&last_name={last}"),
        ("NM Bar attorney search",     f"https://nmbar.org/Nmbar/Find_A_Lawyer/NMBar/MembersClients/Find_a_Lawyer.aspx"),
        ("NM SOS Business Search",     f"https://sos.nm.gov/business/business-search?name={name_plus}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "TIER 6 — ASSET RECORDS", "=" * 50, ""]
    for nm, url in [
        ("FAA Aircraft — owner name",    f"https://registry.faa.gov/aircraftinquiry/Search/OwnerSearch?OwnerName={name_plus}"),
        ("FAA Airmen — pilot cert",      "https://amsrvs.registry.faa.gov/airmeninquiry/Main.aspx"),
        ("USCG Vessel — owner search",   "https://cgmix.uscg.mil/vesselinfo/Default.aspx"),
        ("BoatInfoWorld vessel search",  f"https://www.boatinfoworld.com/searchvessel.asp?vesselname={name_plus}"),
        ("NM WCA — workers comp records","https://www.workerscomp.nm.gov/"),
        ("USPS PO Box trace — 39 CFR 265.6 formal request", "https://postalinspectors.uspis.gov/"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines.extend(render_identifier_sources(ids, name_part, first, last))
    try:
        san_out, _, _ = run_cmd(f"curl -s 'https://api.opensanctions.org/search/default?q={name_plus}&schema=Person' 2>/dev/null", timeout=10)
        san_data = json.loads(san_out); results = san_data.get("results",[])
        lines += ["=" * 50, "LIVE SANCTIONS / WATCHLIST CHECK", "=" * 50, ""]
        if results:
            lines.append(f"WARNING: {len(results)} MATCH(ES) FOUND")
            for r in results[:5]: lines.append(f"  * {r.get('caption','?')} -- Score: {r.get('score','?')}")
        else: lines.append("No matches found on sanctions/watchlists")
        lines.append("")
    except: pass
    dorks = build_dorks(name_part, location_part, state, city, ids)
    lines.extend(render_dorks(dorks["skip"], "SKIP TRACE"))
    # ── IDI HANDOFF BLOCK ──────────────────────────────────────────────────────
    lines += ["", "=" * 50, "IDI — INVESTIGATIVE DATA INTELLIGENCE HANDOFF", "=" * 50, ""]
    lines.append("When free-tier results are exhausted, escalate to IDI.")
    lines.append("IDI is a permissible-use commercial database — law firm access required.")
    lines.append("")
    lines.append("QUERY PACKAGE — paste directly into IDI search:")
    lines.append("")
    lines.append(f"  Full Name:   {name_part}")
    if location_part: lines.append(f"  Location:    {location_part}")
    dob_m  = ids.get("dob_month","")
    dob_d  = ids.get("dob_day","")
    dob_yr = ids.get("dob_year","")
    if dob_m and dob_d and dob_yr:
        lines.append(f"  DOB:         {dob_m}/{dob_d}/{dob_yr}")
    elif dob_yr:
        lines.append(f"  DOB Year:    {dob_yr}")
    ssn_disp = ids.get("ssn_display","")
    ssn_type = ids.get("ssn_type","")
    if ssn_disp:
        lines.append(f"  SSN:         {ssn_disp}  ({ssn_type.upper() if ssn_type else 'ON FILE'})")
    oln = ids.get("oln_number","")
    oln_state = ids.get("oln_state","NM")
    if oln: lines.append(f"  OLN:         {oln}  State: {oln_state}")
    emp = ids.get("employer","")
    if emp: lines.append(f"  Employer:    {emp}")
    street = ids.get("street","")
    zip_   = ids.get("zip","")
    if street: lines.append(f"  Street:      {street}")
    if zip_:   lines.append(f"  ZIP:         {zip_}")
    lines.append("")
    lines.append("IDI RECOMMENDED SEARCH SEQUENCE:")
    lines.append("  1. Person Search — confirm identity, harvest current address")
    lines.append("  2. Address History — full address timeline with date ranges")
    lines.append("  3. Associates / Relatives — develop alternate contact points")
    lines.append("  4. Phone Report — current + historical numbers")
    lines.append("  5. Employment — verify employer, develop service of process address")
    if ssn_disp: lines.append("  6. SSN Trace — confirm identity, address history anchored to SSN")
    if oln:      lines.append("  6. MVR / Driver History — violations, license status, DUI flags")
    lines.append("")
    lines.append("IDI ESCALATION TRIGGERS:")
    lines.append("  * Free-tier sources return stale/conflicting addresses")
    lines.append("  * Subject has common name — need DOB/SSN disambiguation")
    lines.append("  * Service of process failed — need current employer/alternate address")
    lines.append("  * Litigation hold — comprehensive address history required for record")
    lines.append("")
    lines.append("⚠ DPPA: Document permissible use before IDI query.")
    lines.append("  18 U.S.C. § 2721(b) — litigation support / service of process.")
    lines.append("  Log: case number, attorney authorization, query date, investigator.")
    lines.append("")
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "skip_trace", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: SOCIAL MEDIA
# ══════════════════════════════════════════════════════════════════════════════

def module_social_media(target, job_id, ids=None):
    if ids is None: ids = {}
    emit(job_id, "module_start", {"module": "social_media"})
    if "," in target: name_quoted = target.split(",")[0].strip()
    else: name_quoted = target.strip()
    name_plus = quote_plus(name_quoted)
    parts = name_quoted.split(); first = parts[0] if parts else target; last = parts[-1] if len(parts) > 1 else ""
    _, location_part, _, _, state, city = parse_name_location(target)
    lines = [f"TARGET: {name_quoted}", ""]
    for section, items in [
        ("FACEBOOK INTELLIGENCE", [
            ("People Search",   f"https://www.facebook.com/search/people/?q={name_plus}"),
            ("Posts mentioning",f"https://www.facebook.com/search/posts/?q={name_plus}"),
            ("Photos tagged",   f"https://www.facebook.com/search/photos/?q={name_plus}"),
            ("Check-ins",       f"https://www.facebook.com/search/places/?q={name_plus}"),
            ("Groups",          f"https://www.facebook.com/search/groups/?q={name_plus}"),
            ("Events",          f"https://www.facebook.com/search/events/?q={name_plus}"),
            ("Marketplace",     f"https://www.facebook.com/marketplace/search/?query={name_plus}"),
            ("Sowsearch deep",  f"https://sowsearch.info/search?q={name_plus}"),
            ("Google FB dork",  f"https://www.google.com/search?q=site:facebook.com+%22{name_plus}%22"),
        ]),
        ("INSTAGRAM INTELLIGENCE", [
            ("Profile search",  f"https://www.instagram.com/explore/search/keyword/?q={name_plus}"),
            ("Hashtag search",  f"https://www.instagram.com/explore/tags/{name_plus.replace('+','')}/"),
            ("Google IG dork",  f"https://www.google.com/search?q=site:instagram.com+%22{name_plus}%22"),
        ]),
        ("TWITTER/X INTELLIGENCE", [
            ("People search",   f"https://twitter.com/search?q=%22{name_plus}%22&f=user"),
            ("Recent posts",    f"https://twitter.com/search?q=%22{name_plus}%22&f=live"),
            ("Top posts",       f"https://twitter.com/search?q=%22{name_plus}%22&f=top"),
            ("Near ABQ",        f"https://twitter.com/search?q=%22{name_plus}%22+near%3A%22Albuquerque%22"),
        ]),
        ("LINKEDIN INTELLIGENCE", [
            ("People search",   f"https://www.linkedin.com/search/results/people/?keywords={name_plus}"),
            ("Posts search",    f"https://www.linkedin.com/search/results/content/?keywords={name_plus}"),
            ("Google LI dork",  f"https://www.google.com/search?q=site:linkedin.com/in+%22{name_plus}%22"),
        ]),
        ("TIKTOK / YOUTUBE / REDDIT", [
            ("TikTok user",     f"https://www.tiktok.com/search/user?q={name_plus}"),
            ("YouTube channel", f"https://www.youtube.com/results?search_query={name_plus}&sp=EgIQAg%253D%253D"),
            ("Reddit user",     f"https://www.reddit.com/search/?q=%22{name_quoted}%22&type=user"),
            ("Reddit posts",    f"https://www.reddit.com/search/?q=%22{name_quoted}%22"),
        ]),
        ("OTHER PLATFORMS", [
            ("Snapchat",        f"https://www.snapchat.com/add/{first.lower()}{last.lower()}"),
            ("Pinterest",       f"https://www.pinterest.com/search/people/?q={name_plus}"),
            ("Nextdoor — MANUAL: search local area", "https://nextdoor.com/find-neighbors/"),
            ("Venmo",           f"https://venmo.com/{first.lower()}{last.lower()}"),
            ("Cash App",        f"https://cash.app/${first.lower()}{last.lower()}"),
        ]),
    ]:
        lines += ["=" * 50, section, "=" * 50, ""]
        for lbl, url in items:
            lines.append(f"[{lbl}]"); lines.append(f"  {url}"); lines.append("")
    dorks = build_dorks(name_quoted, location_part, state, city, ids)
    lines.extend(render_dorks(dorks["social"], "SOCIAL MEDIA"))
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "social_media", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: SOCIAL FOOTPRINT
# ══════════════════════════════════════════════════════════════════════════════

def module_social_footprint(target, job_id, ids=None):
    if ids is None: ids = {}
    emit(job_id, "module_start", {"module": "social_footprint"})
    name_part, location_part, first, last, state, city = parse_name_location(target)
    name_plus = quote_plus(name_part)
    uvars = [f"{first.lower()}{last.lower()}", f"{first.lower()}.{last.lower()}", f"{first.lower()}_{last.lower()}", f"{first.lower()}{last.lower()[:3]}", f"{first.lower()[0]}{last.lower()}"] if first and last else []
    lines = [f"TARGET:   {name_part}"]
    if location_part: lines.append(f"LOCATION: {location_part}")
    lines += ["", "=" * 50, "DIRECT PROFILE ATTEMPTS — USERNAME VARIATIONS", "=" * 50, ""]
    for uname in uvars[:4]:
        lines.append(f"Username: {uname}")
        for plat, url in [
            ("Facebook",  f"https://www.facebook.com/{uname}"),
            ("Instagram", f"https://www.instagram.com/{uname}/"),
            ("Twitter/X", f"https://twitter.com/{uname}"),
            ("TikTok",    f"https://www.tiktok.com/@{uname}"),
            ("LinkedIn",  f"https://www.linkedin.com/in/{uname}"),
        ]:
            lines.append(f"  [{plat}]  {url}")
        lines.append("")
    lines += ["=" * 50, "REAL-TIME SOCIAL SEARCH — FREE TOOLS", "=" * 50, ""]
    for nm, url in [
        ("WhatsMyName username sweep",        f"https://whatsmyname.app/?q={first.lower()}{last.lower()}"),
        ("Sowsearch FB deep",                 f"https://sowsearch.info/search?q={name_plus}"),
        ("Google forum/community search",     f"https://www.google.com/search?q=%22{name_plus}%22+forum+OR+community+OR+discussion"),
        ("Google Groups",                     f"https://groups.google.com/search/groups?q={name_plus}"),
        ("IDCrawl social+records",            f"https://www.idcrawl.com/name/{first.lower()}-{last.lower()}"),
        ("Epieos email-to-social — MANUAL: enter email or name", f"https://epieos.com/?q={name_plus}&t=name"),
        ("GHunt Google acct recon — MANUAL: run CLI with email", "https://github.com/mxrch/GHunt"),
        ("SocialBlade — account age/stats", f"https://socialblade.com/search?query={name_plus}"),
        ("NM Politics — public figures NM", f"https://www.nmpolitics.net/?s={name_plus}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "PAYMENT APP PROFILES", "=" * 50, ""]
    lines.append("TIP: Venmo public feed shows transactions, memos, associates — confirms relationships/employer.")
    lines.append(f"  Venmo-OSINT CLI: github.com/sc1341/Venmo-OSINT  (run offline once username confirmed)")
    lines.append("")
    for nm, url in [
        ("Venmo direct profile",   f"https://venmo.com/{first.lower()}{last.lower()}"),
        ("Venmo Google dork",      f"https://www.google.com/search?q=%22{name_plus}%22+site:venmo.com"),
        ("Cash App direct",        f"https://cash.app/${first.lower()}{last.lower()}"),
        ("PayPal.me direct",       f"https://www.paypal.com/paypalme/{first.lower()}{last.lower()}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "WAYBACK MACHINE — DELETED & ARCHIVED CONTENT", "=" * 50, ""]
    lines.append("TIP: Captures deleted social posts, old addresses, prior business listings, removed content.")
    lines.append("")
    for nm, url in [
        ("Wayback name search",       f"https://web.archive.org/web/*/%22{quote_plus(name_part)}%22"),
        ("Wayback FB profile archive",f"https://web.archive.org/web/*/facebook.com/*{first.lower()}*{last.lower()}*"),
        ("Wayback LinkedIn archive",  f"https://web.archive.org/web/*/linkedin.com/in/*{last.lower()}*"),
        ("Wayback CDX API name hits", f"https://web.archive.org/cdx/search/cdx?url=*&output=text&limit=10&fl=original,timestamp&filter=original:.*{first.lower()}.*{last.lower()}.*"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "LINKEDIN DEEP SEARCH", "=" * 50, ""]
    for nm, url in [
        ("LinkedIn people search",    f"https://www.linkedin.com/search/results/people/?keywords={name_plus}"),
        ("LinkedIn + NM geo filter",  f"https://www.linkedin.com/search/results/people/?keywords={name_plus}&geoUrn=%5B%22102095887%22%5D"),
        ("Google LI profile dork",    f"https://www.google.com/search?q=site:linkedin.com/in+%22{name_plus}%22"),
        ("Google LI + location",      f"https://www.google.com/search?q=site:linkedin.com+%22{name_plus}%22+%22New+Mexico%22"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "REVERSE IMAGE & FACE SEARCH — FREE", "=" * 50, "", "Upload subject photo to find additional profiles.", ""]
    for nm, url in [
        ("Yandex — best for faces — MANUAL: upload photo",  "https://yandex.com/images/"),
        ("PimEyes — FREEMIUM: limited free — MANUAL: upload", "https://pimeyes.com/en"),
        ("Lenso.ai — MANUAL: upload photo",                 "https://lenso.ai/en"),
        ("Google Reverse Image — MANUAL: upload photo",     "https://images.google.com/"),
        ("TinEye — MANUAL: upload or paste URL",            "https://tineye.com/"),
        ("Bing Visual Search — MANUAL: upload photo",       "https://www.bing.com/images/search?view=detailv2&iss=sbi"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    dorks = build_dorks(name_part, location_part, state, city, ids)
    lines.extend(render_dorks(dorks["social"], "SOCIAL FOOTPRINT"))
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "social_footprint", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# NON-PERSON MODULES
# ══════════════════════════════════════════════════════════════════════════════

def module_hit_and_run(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "hit_and_run"})
    target_clean = target.upper().strip()
    parts = target_clean.split()
    NM_STATES = ["NM","TX","AZ","CO","CA","FL","NY","IL","OH","GA","NC","MI","PA","WA","OR"]
    if len(parts) >= 2 and parts[-1] in NM_STATES:
        plate = parts[0].replace("-","").replace(",","").strip(); state = parts[-1]
    elif len(parts) == 1 and 3 <= len(target_clean) <= 8 and target_clean.replace("-","").replace(",","").isalnum():
        plate = target_clean.replace("-","").replace(",","").strip(); state = "NM"
    else:
        plate = target_clean.replace(" ","").replace("-","").replace(",","").strip(); state = "NM"
    is_vin = len(plate) == 17
    lines = [f"TARGET:  {target}", f"PARSED:  {'VIN' if is_vin else 'Plate'}={plate}  State={state}", ""]
    lines += ["NOTE: Free tools return make/model/theft data only.", "  Owner name/address requires state MVD DPPA request.", ""]
    lines += ["=" * 50, "STEP 1 — IDENTIFY VEHICLE FROM PHOTO/VIDEO", "=" * 50, ""]
    for nm, url in [
        ("Carnet.ai — MANUAL: upload photo",        "https://carnet.ai/"),
        ("Google Reverse Image — MANUAL: upload",   "https://images.google.com/"),
        ("Yandex — MANUAL: upload",                 "https://yandex.com/images/"),
        ("TinEye — MANUAL: upload or URL",          "https://tineye.com/"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "STEP 2 — VIN & PLATE LOOKUP", "=" * 50, ""]
    if is_vin:
        vin_links = [
            ("NHTSA VIN Decoder",           f"https://vpic.nhtsa.dot.gov/decoder/Car/{plate}/0"),
            ("Driving-Tests.org VIN",       f"https://driving-tests.org/vin-decoder/?vin={plate}"),
            ("VIN.report free decode",       f"https://www.vin.report/vin/{plate}"),
            ("VINDecoderZ free",             f"https://www.vindecoderz.com/EN/check-lookup/{plate}"),
            ("EpicVIN decode",              f"https://epicvin.com/vin-decoder?vin={plate}"),
            ("VinFreeCheck",                f"https://www.vinfreecheck.com/?vin={plate}"),
            ("NICB VINCheck stolen/salvage — MANUAL: enter VIN", "https://www.nicb.org/vincheck"),
            ("NHTSA Recalls by VIN",        f"https://www.nhtsa.gov/vehicle/{plate}///complaints"),
            ("NMVTIS Title Check — MANUAL: enter VIN", "https://www.vehiclehistory.gov/"),
        ]
    else:
        vin_links = [
            ("EpicVIN plate lookup",        f"https://epicvin.com/license-plate-lookup?plate={plate}&state={state}"),
            ("Faxvin plate lookup",         f"https://www.faxvin.com/license-plate-lookup/{state.lower()}/{plate}"),
            ("NICB VINCheck stolen — MANUAL: enter VIN", "https://www.nicb.org/vincheck"),
            ("NMVTIS Title Check — MANUAL: enter VIN",   "https://www.vehiclehistory.gov/"),
            ("NHTSA VIN Decoder — MANUAL: enter VIN",    "https://vpic.nhtsa.dot.gov/decoder/"),
            ("NHTSA Recalls — MANUAL: enter VIN",        "https://www.nhtsa.gov/recalls"),
        ]
    for nm, url in vin_links:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "STEP 3 — SOCIAL MEDIA PLATE SEARCH", "=" * 50, ""]
    for nm, url in [
        ("Facebook posts",  f"https://www.facebook.com/search/posts/?q={plate}"),
        ("Instagram",       f"https://www.instagram.com/explore/search/keyword/?q={plate}"),
        ("Twitter/X live",  f"https://twitter.com/search?q=%22{plate}%22&f=live"),
        ("Reddit",          f"https://www.reddit.com/search/?q=%22{plate}%22"),
        ("YouTube",         f"https://www.youtube.com/results?search_query=%22{plate}%22"),
        ("Google Images",   f"https://www.google.com/search?tbm=isch&q=%22{plate}%22+New+Mexico"),
        ("Nextdoor — MANUAL: search local area", "https://nextdoor.com/"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "STEP 4 — WITNESS & DASHCAM", "=" * 50, ""]
    for nm, url in [
        ("r/NewMexico hit & run",           "https://www.reddit.com/r/newmexico/search/?q=hit+and+run&sort=new"),
        ("r/Albuquerque",                   "https://www.reddit.com/r/Albuquerque/search/?q=hit+and+run&sort=new"),
        ("Google News ABQ",                 "https://www.google.com/search?q=%22hit+and+run%22+%22albuquerque%22&tbm=nws"),
        ("ABQ Journal",                     "https://www.abqjournal.com/?s=hit+run"),
        ("Waze Incident Map — MANUAL",      "https://www.waze.com/livemap"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "STEP 5 — OWNER IDENTIFICATION", "=" * 50, ""]
    lines += ["Once make/model/plate confirmed:", "  -> NM MVD DPPA request for registered owner", "  -> Run owner name through SKIP TRACE module", "  -> NM Courts: https://caselookup.nmcourts.gov/caselookup/app", ""]
    if is_vin:
        lines += ["=" * 50, "NHTSA LIVE COMPLAINTS — VIN", "=" * 50, ""]
        try:
            nhtsa_url = f"https://api.nhtsa.gov/complaints/complaintsByVehicle?vin={plate}"
            nhtsa_out, _, _ = run_cmd(f"curl -s '{nhtsa_url}' 2>/dev/null", timeout=10)
            nhtsa_data = json.loads(nhtsa_out)
            complaints = nhtsa_data.get("results", [])
            if complaints:
                lines.append(f"  {len(complaints)} NHTSA complaint(s) found for VIN {plate}")
                for c in complaints[:5]:
                    lines.append(f"  Component: {c.get('component','N/A')}")
                    lines.append(f"  Date:      {c.get('dateOfIncident','N/A')}")
                    lines.append(f"  Summary:   {str(c.get('summary',''))[:200]}")
                    lines.append("")
            else:
                lines.append(f"  No NHTSA complaints on file for VIN {plate}")
                lines.append("")
        except:
            lines.append(f"  [NHTSA API link] https://api.nhtsa.gov/complaints/complaintsByVehicle?vin={plate}")
            lines.append("")
    lines += ["=" * 50, "GOOGLE DORKS", "=" * 50, ""]
    for dork in [f'"{plate}" New Mexico accident OR crash OR "hit and run"', f'"{plate}" NM plate dashcam OR witness OR footage', f'"{plate}" site:facebook.com', f'"{plate}" site:reddit.com']:
        lines.append(f"  {dork}"); lines.append(f"  https://www.google.com/search?q={quote_plus(dork)}"); lines.append("")
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "hit_and_run", "result": result})
    return result


def module_photo_forensics(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "photo_forensics"})
    from urllib.parse import quote
    lines = [f"TARGET: {target}", ""]
    is_url = target.startswith("http")
    te = quote(target, safe='') if is_url else target
    lines += ["=" * 50, "REVERSE IMAGE SEARCH — FREE", "=" * 50, ""]
    if is_url:
        rev = [
            ("Google Reverse Image",    f"https://images.google.com/searchbyimage?image_url={te}"),
            ("TinEye",                  f"https://tineye.com/search?url={te}"),
            ("Yandex best for faces",   f"https://yandex.com/images/search?url={te}&rpt=imageview"),
            ("Bing Visual Search",      f"https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:{te}"),
            ("Lenso.ai",                f"https://lenso.ai/en?url={te}"),
            ("PimEyes — MANUAL: upload","https://pimeyes.com/en"),
        ]
    else:
        rev = [
            ("Google Reverse Image — MANUAL: upload",   "https://images.google.com/"),
            ("TinEye — MANUAL: upload",                 "https://tineye.com/"),
            ("Yandex best for faces — MANUAL: upload",  "https://yandex.com/images/"),
            ("Bing Visual Search — MANUAL: upload",     "https://www.bing.com/images/search?view=detailv2&iss=sbi"),
            ("Lenso.ai — MANUAL: upload",               "https://lenso.ai/en"),
            ("PimEyes — FREEMIUM: limited free — MANUAL: upload", "https://pimeyes.com/en"),
        ]
    for nm, url in rev: lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "PHOTO METADATA EXTRACTION — FREE", "=" * 50, ""]
    for nm, url in [
        ("Jeffrey EXIF Viewer — MANUAL: upload",  "http://exif.regex.info/exif.cgi"),
        ("Izitru — photo authenticity verify — MANUAL: upload", "https://www.izitru.com/"),
        ("ExifTool Online — MANUAL: upload",      "https://exiftool.org/"),
        ("Metadata2Go — MANUAL: upload",          "https://www.metadata2go.com/"),
        ("FotoForensics — MANUAL: upload",        "https://fotoforensics.com/"),
        ("Forensically — MANUAL: upload",         "https://29a.ch/photo-forensics/"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "VIDEO FORENSICS — FREE", "=" * 50, ""]
    for nm, url in [
        ("InVID WeVerify — MANUAL: install browser plugin", "https://www.invid-project.eu/tools-and-services/invid-verification-plugin/"),
        ("YouTube DataViewer",                              "https://citizenevidence.org/"),
        ("TrueMedia.org — MANUAL: upload",                  "https://www.truemedia.org/"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "GEOLOCATION FROM PHOTOS — FREE", "=" * 50, ""]
    for nm, url in [
        ("SunCalc shadow/time analysis — MANUAL: set location+time", "https://www.suncalc.org/"),
        ("Google Maps Street View",                                   "https://www.google.com/maps"),
        ("Bing Maps",                                                 "https://www.bing.com/maps"),
        ("Google Earth Web",                                          "https://earth.google.com/web/"),
        ("Overpass Turbo — MANUAL: query OSM features",               "https://overpass-turbo.eu/"),
        ("GeoHack — MANUAL: enter coordinates",                       "https://geohack.toolforge.org/"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    if is_url:
        lines += ["=" * 50, "AUTOMATED METADATA EXTRACTION", "=" * 50, ""]
        try:
            out, _, _ = run_cmd(f'curl -s -L -o /tmp/fivet_img.jpg "{target}" 2>/dev/null && exiftool /tmp/fivet_img.jpg 2>/dev/null | head -40', timeout=15)
            lines.append(out if out else "No metadata extracted.")
        except: lines.append("Could not fetch image.")
        lines.append("")
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "photo_forensics", "result": result})
    return result


def module_geolocation(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "geolocation"})
    import re as _re
    lines = [f"TARGET: {target}", ""]
    lp = quote_plus(target)
    for section, items in [
        ("MAP INTELLIGENCE — FREE", [
            ("Google Maps",         f"https://www.google.com/maps/search/{lp}"),
            ("Google Street View",  f"https://www.google.com/maps?q={lp}&layer=c"),
            ("Google Earth Web",    f"https://earth.google.com/web/search/{lp}"),
            ("Bing Maps",           f"https://www.bing.com/maps?q={lp}"),
            ("OpenStreetMap",       f"https://www.openstreetmap.org/search?query={lp}"),
            ("Apple Maps",          f"https://maps.apple.com/?q={lp}"),
        ]),
        ("SATELLITE & HISTORICAL — FREE", [
            ("Google Earth Historical",  f"https://earth.google.com/web/search/{lp}"),
            ("USGS EarthExplorer — MANUAL: draw AOI", "https://earthexplorer.usgs.gov/"),
            ("NASA Worldview",           "https://worldview.earthdata.nasa.gov/"),
            ("Bing Birds Eye",           f"https://www.bing.com/maps?q={lp}&style=b"),
        ]),
        ("SPECIALIZED TOOLS — FREE", [
            ("Overpass Turbo — MANUAL: query OSM",               "https://overpass-turbo.eu/"),
            ("SunCalc sun position — MANUAL: set location",      "https://www.suncalc.org/"),
            ("CalcMaps distance/area",                            "https://www.calcmaps.com/map-distance/"),
        ]),
    ]:
        lines += ["=" * 50, section, "=" * 50, ""]
        for nm, url in items: lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    if _re.match(r'^\d+\.\d+\.\d+\.\d+$', target):
        try:
            out, _, _ = run_cmd(f"curl -s 'https://ipapi.co/{target}/json/' 2>/dev/null")
            data = json.loads(out)
            lines += ["=" * 50, "IP GEOLOCATION (LIVE)", "=" * 50, ""]
            lines += [f"IP:       {data.get('ip',target)}", f"City:     {data.get('city','N/A')}", f"Region:   {data.get('region','N/A')}", f"Country:  {data.get('country_name','N/A')}", f"Org/ISP:  {data.get('org','N/A')}", f"Lat/Lon:  {data.get('latitude','N/A')}, {data.get('longitude','N/A')}"]
            lat = data.get('latitude',''); lon = data.get('longitude','')
            if lat and lon: lines.append(f"Maps:     https://www.google.com/maps?q={lat},{lon}")
            lines.append("")
        except: pass
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "geolocation", "result": result})
    return result


def module_username_search(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "username_search"})
    lines = [f"TARGET USERNAME: {target}", ""]
    lines += ["=" * 50, "AUTOMATED SCANNER", "=" * 50, ""]
    out, _, _ = run_cmd(f"python3 -m sherlock {target} --timeout 8 2>/dev/null", timeout=120)
    if out and "not found" not in out.lower():
        lines += ["[SHERLOCK — 300+ platforms]", out, ""]
    out2, _, rc2 = run_cmd(f"python3 -m maigret {target} --top-sites 50 2>/dev/null", timeout=120)
    if out2 and rc2 == 0:
        lines += ["[MAIGRET — full dossier]", out2[:2000], ""]
    lines += ["=" * 50, "MANUAL USERNAME SEARCH — FREE", "=" * 50, ""]
    for nm, url in [
        ("WhatsMyName",     f"https://whatsmyname.app/?q={target}"),
        ("IDCrawl",         f"https://www.idcrawl.com/{target}"),
        ("UserSearch.org",  f"https://usersearch.org/results_normal.php?q={target}"),
        ("Namechk",         f"https://namechk.com/{target}"),

    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "PLATFORM DIRECT CHECKS", "=" * 50, ""]
    for nm, url in [
        ("Twitter/X",  f"https://twitter.com/{target}"),
        ("Instagram",  f"https://www.instagram.com/{target}/"),
        ("TikTok",     f"https://www.tiktok.com/@{target}"),
        ("YouTube",    f"https://www.youtube.com/@{target}"),
        ("Reddit",     f"https://www.reddit.com/user/{target}"),
        ("GitHub",     f"https://github.com/{target}"),
        ("LinkedIn",   f"https://www.linkedin.com/in/{target}"),
        ("Pinterest",  f"https://www.pinterest.com/{target}/"),
        ("Snapchat",   f"https://www.snapchat.com/add/{target}"),
        ("Venmo",      f"https://venmo.com/{target}"),
        ("Cash App",   f"https://cash.app/${target}"),
        ("Telegram",   f"https://t.me/{target}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "username_search", "result": result})
    return result


def module_phone(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "phone"})
    clean = target.replace("-","").replace("(","").replace(")","").replace(" ","").replace("+1","").strip()
    formatted = f"({clean[:3]}) {clean[3:6]}-{clean[6:]}" if len(clean)==10 else target
    phone_plus1 = f"+1{clean}" if len(clean)==10 else target
    e164 = f"+1{clean}" if len(clean)==10 else target
    lines = [f"TARGET:    {formatted}", f"CLEANED:   {clean}", f"E.164:     {e164}  (international format for tool submissions)", ""]
    ipqs_key = os.environ.get("IPQS_API_KEY",""); nv_key = os.environ.get("NUMVERIFY_API_KEY","")
    if ipqs_key:
        try:
            d = json.loads(run_cmd(f"curl -s 'https://www.ipqualityscore.com/api/json/phone/{ipqs_key}/{clean}' 2>/dev/null", timeout=10)[0])
            if d.get("success"):
                lines += ["=== CARRIER INTELLIGENCE (IPQS) ===", f"Valid: {d.get('valid','N/A')}", f"Line Type: {d.get('line_type','N/A')}", f"Carrier: {d.get('carrier','N/A')}", f"Risky: {d.get('risky',False)}", f"VoIP: {d.get('VOIP',False)}", f"Prepaid: {d.get('prepaid',False)}", ""]
        except: pass
    if not ipqs_key and not nv_key:
        lines += ["Add IPQS_API_KEY or NUMVERIFY_API_KEY to Render env vars for live carrier data.", ""]
    lines += ["=" * 50, "CARRIER & LINE TYPE INTELLIGENCE", "=" * 50, ""]
    lines.append("TIP: Confirm VoIP vs landline vs mobile before serve attempt — changes strategy.")
    lines.append("")
    for nm, url in [
        ("CarrierLookup — carrier/line type",      f"https://www.carrierlookup.com/?number={clean}"),
        ("FreeCarrierLookup — carrier/line type",  f"https://www.freecarrierlookup.com/?phonenumber={clean}&api_key=demo"),
        ("PhoneValidator — validate + carrier",    f"https://www.phonevalidator.com/index.aspx?phonenumber={clean}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "FREE REVERSE LOOKUP SITES", "=" * 50, "", "NOTE: SpyDialer calls the number silently — target may see missed call. Use deliberately.", ""]
    for nm, url in [
        ("SpyDialer — name via voicemail",  f"https://www.spydialer.com/default.aspx?phone={clean}"),
        ("NumLookup — owner name/carrier",  f"https://www.numlookup.com/?number={clean}"),
        ("TruePeopleSearch",                f"https://www.truepeoplesearch.com/results?phoneno={clean}"),
        ("ThatsThem",                       f"https://thatsthem.com/phone/{clean}"),
        ("FastPeopleSearch",                f"https://www.fastpeoplesearch.com/phone/{clean}"),
        ("USPhoneBook",                     f"https://www.usphonebook.com/{clean}"),
        ("411.com — partial free",           f"https://www.411.com/phone/{clean}"),
        ("TrueCaller — basic ID free",      f"https://www.truecaller.com/search/us/{clean}"),
        ("Zlookup — free reverse lookup",    f"https://www.zlookup.com/results?phone_number={clean}"),
        ("PhoneOwner.us — owner + address",  f"https://www.phoneowner.us/{clean}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "CROWDSOURCED CALLER ID", "=" * 50, ""]
    lines.append("TIP: Cross-reference — if 2+ sources return same name, treat as confirmed lead.")
    lines.append("")
    for nm, url in [
        ("Sync.ME — caller ID partial free",    f"https://sync.me/search/?number={phone_plus1}"),
        ("Hiya — spam rating free/ID partial",  f"https://hiya.com/phone-number/{clean}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "MESSAGING APP PRESENCE — MANUAL WORKFLOW", "=" * 50, ""]
    lines.append("⚑ MANUAL: Save number as contact in phone, then check each app.")
    lines.append("  WhatsApp: reveals profile photo + status to anyone with number saved.")
    lines.append("  Telegram: reveals username, profile photo, last seen — HIGHEST VALUE.")
    lines.append("  Signal:   reveals if registered — profile visible if privacy not locked.")
    lines.append("")
    for nm, url in [
        ("WhatsApp — open chat with number",  f"https://wa.me/{phone_plus1.replace('+','')}"),
        ("Telegram web — search number",      f"https://web.telegram.org/"),
        ("Telegram t.me link attempt",        f"https://t.me/+{phone_plus1.replace('+','')}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "SPAM & REPORT DATABASES", "=" * 50, ""]
    for nm, url in [
        ("800Notes — community reports",    f"https://800notes.com/Phone.aspx/{clean}"),
        ("Nomorobo — robocall check",       f"https://www.nomorobo.com/lookup/{clean}"),
        ("WhoCallsMe — community reports",  f"https://www.whocallsme.com/phone-number/{clean}"),
        ("ShouldIAnswer — community DB",    f"https://www.shouldianswer.com/phone-number/{clean}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    # Build number format variations for reuse detection
    dashed = f"{clean[:3]}-{clean[3:6]}-{clean[6:]}" if len(clean)==10 else clean
    spaced  = f"{clean[:3]} {clean[3:6]} {clean[6:]}" if len(clean)==10 else clean
    lines += ["=" * 50, "GOOGLE DORKS", "=" * 50, ""]
    lines.append("TIP: Search all format variations — number reuse across platforms is your pivot point.")
    lines.append("")
    for dork in [
        f'"{formatted}"',
        f'"{dashed}"',
        f'"{spaced}"',
        f'"{clean}"',
        f'"{phone_plus1}"',
        f'"{formatted}" name address',
        f'"{clean}" site:facebook.com',
        f'"{clean}" site:linkedin.com',
        f'"{formatted}" site:whitepages.com OR site:spokeo.com',
        f'"{clean}" spam OR scam OR fraud',
    ]:
        lines.append(f"  {dork}"); lines.append(f"  https://www.google.com/search?q={quote_plus(dork)}"); lines.append("")
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "phone", "result": result})
    return result


def module_email_investigate(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "email_investigate"})
    lines = [f"TARGET EMAIL: {target}", ""]
    out, _, rc = run_cmd(f"python3 -m holehe {target} --only-used 2>/dev/null", timeout=120)
    if out and "holehe" not in out.lower() and "error" not in out.lower():
        lines += ["=" * 50, "HOLEHE — ACCOUNT DETECTION 120+ SITES", "=" * 50, "", out, ""]
    try:
        data = json.loads(run_cmd(f"curl -s 'https://emailrep.io/{target}' -H 'User-Agent: fivet-osint' 2>/dev/null", timeout=10)[0])
        details = data.get("details",{})
        lines += ["=" * 50, "EMAIL REPUTATION — FREE", "=" * 50, "", f"Reputation: {data.get('reputation','N/A')}", f"Suspicious: {data.get('suspicious',False)}", f"Blacklisted: {details.get('blacklisted',False)}", f"Data Breach: {details.get('data_breach',False)}", f"Disposable: {details.get('disposable',False)}", f"Profiles: {chr(44).join(details.get('profiles',[])) or 'None detected'}", ""]
    except: pass
    try:
        domain = target.split("@")[1]
        dns_out = run_cmd(f"dig +short A {domain} 2>/dev/null")[0]
        mx_out  = run_cmd(f"dig +short MX {domain} 2>/dev/null")[0]
        if dns_out or mx_out:
            lines += ["=" * 50, f"EMAIL DOMAIN INTEL: {domain}", "=" * 50, ""]
            if dns_out: lines.append(f"Domain IP:   {dns_out.split()[0]}")
            if mx_out:  lines.append(f"Mail Server: {mx_out}")
            lines.append("")
    except: pass
    lines += ["=" * 50, "FREE LOOKUP SITES", "=" * 50, ""]
    for nm, url in [
        ("TruePeopleSearch",    f"https://www.truepeoplesearch.com/results?emailaddress={target}"),
        ("ThatsThem",           f"https://thatsthem.com/email/{target}"),
        ("EmailRep reputation", f"https://emailrep.io/{target}"),
        ("Skymem — email in web archives", f"https://www.skymem.info/srp?q={target}"),
        ("Verifalia — deliverability check", f"https://verifalia.com/validate-email"),
        ("HaveIBeenPwned",      f"https://haveibeenpwned.com/account/{target}"),
        ("breach.vip — username/email breach search", f"https://breach.vip/"),
        ("Epieos social lookup",f"https://epieos.com/?q={target}&t=email"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "GOOGLE DORKS", "=" * 50, ""]
    for dork in [f'"{target}"', f'"{target}" name address phone', f'"{target}" site:linkedin.com', f'"{target}" site:facebook.com', f'"{target}" site:pastebin.com', f'"{target}" leaked OR breach OR hacked']:
        lines.append(f"  {dork}"); lines.append(f"  https://www.google.com/search?q={quote_plus(dork)}"); lines.append("")
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "email_investigate", "result": result})
    return result


def module_plate_lookup(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "plate_lookup"})
    tc = target.upper().strip(); parts = tc.split()
    states = ["NM","TX","AZ","CO","CA","FL","NY","IL","OH","GA","NC","MI","PA","WA","OR"]
    if len(parts) >= 2 and parts[-1] in states:
        plate = parts[0].replace("-","").replace(",","").strip(); state = parts[-1]
    else:
        plate = tc.replace(" ","").replace("-","").replace(",","").strip(); state = "NM"
    lines = [f"TARGET PLATE: {plate}", f"STATE:        {state}", "NOTE: DPPA permissible purpose required. Law firms qualify.", ""]
    lines += ["=" * 50, "FREE VEHICLE LOOKUP", "=" * 50, ""]
    for nm, url in [
        ("Faxvin plate lookup",         f"https://www.faxvin.com/license-plate-lookup/{state.lower()}/{plate}"),
        ("EpicVIN plate lookup",        f"https://epicvin.com/license-plate-lookup?plate={plate}&state={state}"),
        ("NICB VINCheck stolen — MANUAL: enter VIN", "https://www.nicb.org/vincheck"),
        ("NHTSA Recalls — MANUAL: enter VIN",        "https://www.nhtsa.gov/recalls"),
        ("NMVTIS Title Check — MANUAL: enter VIN",   "https://www.vehiclehistory.gov/"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, f"{state} MVD RECORDS REQUEST", "=" * 50, "", "⚑ MANUAL: Submit DPPA form to state MVD for registered owner.", ""]
    mvd_links = {"NM":("NM MVD","https://www.mvd.newmexico.gov/","(888) 683-4636"),"AZ":("AZ MVD","https://www.azdot.gov/motor-vehicles","(602) 712-7355"),"TX":("TX DMV","https://www.txdmv.gov/","(888) 368-4689"),"CO":("CO DMV","https://dmv.colorado.gov/","(303) 205-5600"),"CA":("CA DMV","https://www.dmv.ca.gov/","(800) 777-0133")}
    mn, mu, mp = mvd_links.get(state,("State MVD","https://www.vehiclehistory.gov/","Check state DMV website"))
    lines += [f"[{mn} DPPA request]", f"  {mu}", f"  Phone: {mp}", ""]
    lines += ["=" * 50, "SOCIAL MEDIA PLATE SEARCH", "=" * 50, ""]
    for nm, url in [
        ("Facebook",    f"https://www.facebook.com/search/posts/?q={plate}"),
        ("Instagram",   f"https://www.instagram.com/explore/search/keyword/?q={plate}"),
        ("Twitter/X",   f"https://twitter.com/search?q=%22{plate}%22&f=live"),
        ("Reddit",      f"https://www.reddit.com/search/?q=%22{plate}%22"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "DPPA PERMISSIBLE PURPOSES — Law Firm", "=" * 50, "", "  * Litigation or investigation in anticipation of litigation", "  * Service of process", "  * Licensed private investigator research", "  * Insurance claims investigation", "  * Locating missing persons or witnesses", "", "Cite: 18 U.S.C. 2721(b)"]
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "plate_lookup", "result": result})
    return result


def module_business(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "business"})
    name_plus = quote_plus(target)
    lines = [f"TARGET: {target}", ""]
    try:
        data = json.loads(run_cmd(f"curl -s 'https://api.opencorporates.com/v0.4/companies/search?q={name_plus}&jurisdiction_code=us_nm&format=json' 2>/dev/null", timeout=10)[0])
        companies = data.get("results",{}).get("companies",[])
        if companies:
            lines += ["=" * 50, "OPENCORPORATES — NM RESULTS", "=" * 50, ""]
            for c in companies[:5]:
                co = c.get("company",{})
                lines += [f"  Name: {co.get('name','N/A')}", f"  Status: {co.get('current_status','N/A')}", f"  Registered: {co.get('incorporation_date','N/A')}", f"  URL: {co.get('opencorporates_url','N/A')}", ""]
        else: lines += ["No NM results from OpenCorporates.", ""]
    except Exception as e: lines += [f"OpenCorporates: {str(e)}", ""]
    lines += ["=" * 50, "SECRETARY OF STATE — FREE", "=" * 50, ""]
    for nm, url in [
        ("NM SOS Business Search",  f"https://sos.nm.gov/business/business-search?name={name_plus}"),
        ("NM SOS alt portal",       "https://sos.nm.gov/business/"),
        ("AZ SOS",                  f"https://ecorp.azcc.gov/BusinessSearch/BusinessSearch?SearchTerm={name_plus}"),
        ("CO SOS",                  f"https://www.sos.state.co.us/biz/BusinessEntityCriteriaExt.do?nameTyp=ENT&entityName={name_plus}"),
        ("TX SOS — MANUAL: enter name", "https://mycpa.cpa.state.tx.us/coa/Index.html"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "FEDERAL DATABASES — FREE", "=" * 50, ""]
    for nm, url in [
        ("SAM.gov federal contractors",     f"https://sam.gov/search/?keywords={name_plus}&sort=relevanceScore&index=ei&is_active=true&page=1"),
        ("SEC EDGAR public companies",      f"https://www.sec.gov/cgi-bin/browse-edgar?company={name_plus}&CIK=&type=&dateb=&owner=include&count=40&search_text=&action=getcompany"),
        ("OpenCorporates all states",       f"https://opencorporates.com/companies?q={name_plus}&jurisdiction_code=us"),
        ("PACER business search — MANUAL",  "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("BBB Albuquerque",                 f"https://www.bbb.org/search?find_text={name_plus}&find_loc=Albuquerque%2C+NM"),
        ("SEC EDGAR full text search",      f"https://efts.sec.gov/LATEST/search-index?q=%22{name_plus}%22&dateRange=custom&startdt=2000-01-01"),
        ("Manta business directory",        f"https://www.manta.com/search?search_source=nav&search={name_plus}"),
        ("NM Legislature lobbyist/donor",   f"https://nmlegis.gov/Legislation/search?search={name_plus}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["=" * 50, "BUSINESS INTELLIGENCE — FREE", "=" * 50, ""]
    for nm, url in [
        ("LinkedIn company",    f"https://www.linkedin.com/search/results/companies/?keywords={name_plus}"),
        ("Yelp",                f"https://www.yelp.com/search?find_desc={name_plus}&find_loc=Albuquerque%2C+NM"),
        ("Google business",     f"https://www.google.com/search?q={name_plus}+Albuquerque+NM+business"),
        ("OpenCorporates officers", f"https://opencorporates.com/officers?q={name_plus}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "business", "result": result})
    return result


def module_whois(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "whois"})
    api_key = os.environ.get("WHOIS_API_KEY","at_free")
    try:
        data = json.loads(run_cmd(f"curl -s 'https://www.whoisxmlapi.com/whoisserver/WhoisService?apiKey={api_key}&domainName={target}&outputFormat=JSON' 2>/dev/null")[0])
        record = data.get("WhoisRecord",{}); registrant = record.get("registrant",{})
        lines = [f"Domain: {record.get('domainName',target)}", f"Registrar: {record.get('registrarName','N/A')}", f"Created: {record.get('createdDate','N/A')}", f"Expires: {record.get('expiresDate','N/A')}", f"Registrant: {registrant.get('organization',registrant.get('name','N/A'))}", f"Country: {registrant.get('country','N/A')}"]
        ns = record.get("nameServers",{}).get("hostNames",[])
        if ns: lines.append(f"Nameservers: {chr(44).join(ns[:4])}")
        result = "\n".join(lines)
    except:
        out, _, _ = run_cmd(f"whois {target} 2>/dev/null | head -40")
        result = out if out else f"WHOIS lookup failed for {target}"
    emit(job_id, "module_done", {"module": "whois", "result": result})
    return result


def module_urlscan(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "urlscan"})
    from urllib.parse import quote_plus as qp
    lines = [f"TARGET: {target}", ""]
    lines += ["=" * 50, "URLSCAN.IO — FREE DOMAIN ANALYSIS", "=" * 50, ""]
    for nm, url in [
        ("URLScan search",      f"https://urlscan.io/search/#page.domain:{target}"),
        ("URLScan live scan",   f"https://urlscan.io/scan/"),
        ("URLVoid reputation",  f"https://www.urlvoid.com/scan/{target}/"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "urlscan", "result": result})
    return result


def module_dns(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "dns"})
    lines = []
    for rtype in ["A","AAAA","MX","NS","TXT","CNAME"]:
        out, _, _ = run_cmd(f"dig +short {rtype} {target} 2>/dev/null")
        if out: lines.append(f"[{rtype}] {out}")
    result = "\n".join(lines) if lines else "No DNS records found."
    emit(job_id, "module_done", {"module": "dns", "result": result})
    return result


def module_nmap(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "nmap"})
    common_ports = {21:"FTP",22:"SSH",25:"SMTP",53:"DNS",80:"HTTP",110:"POP3",143:"IMAP",443:"HTTPS",445:"SMB",3306:"MySQL",3389:"RDP",8080:"HTTP-Alt",8443:"HTTPS-Alt"}
    try:
        ip = socket.gethostbyname(target); open_ports = []
        for port, service in common_ports.items():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM); sock.settimeout(1)
                if sock.connect_ex((ip, port)) == 0: open_ports.append(f"  {port}/tcp  OPEN  {service}")
                sock.close()
            except: pass
        result = f"Host: {target} ({ip})\n\n" + ("\n".join(open_ports) if open_ports else "No common ports open.")
    except Exception as e: result = f"Port scan failed: {str(e)}"
    emit(job_id, "module_done", {"module": "nmap", "result": result})
    return result


def module_geoip(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "geoip"})
    try:
        ip = socket.gethostbyname(target)
        data = json.loads(run_cmd(f"curl -s 'https://ipapi.co/{ip}/json/' 2>/dev/null")[0])
        result = "\n".join([f"IP: {data.get('ip',ip)}", f"City: {data.get('city','N/A')}", f"Region: {data.get('region','N/A')}", f"Country: {data.get('country_name','N/A')}", f"Org/ISP: {data.get('org','N/A')}", f"Timezone: {data.get('timezone','N/A')}", f"Lat/Lon: {data.get('latitude','N/A')}, {data.get('longitude','N/A')}"])
    except Exception as e: result = f"GeoIP lookup failed: {str(e)}"
    emit(job_id, "module_done", {"module": "geoip", "result": result})
    return result


def module_shodan(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "shodan"})
    api_key = os.environ.get("SHODAN_API_KEY","")
    if not api_key: result = "Add SHODAN_API_KEY to Render Environment Variables."
    else:
        try:
            import urllib.request
            ip = socket.gethostbyname(target)
            with urllib.request.urlopen(f"https://api.shodan.io/shodan/host/{ip}?key={api_key}", timeout=10) as r:
                data = json.loads(r.read().decode())
            if "error" in data: result = f"Shodan: {data['error']}"
            else:
                ports = data.get("ports",[])
                result = "\n".join([f"IP: {ip}", f"Org: {data.get('org','N/A')}", f"ISP: {data.get('isp','N/A')}", f"Country: {data.get('country_name','N/A')}", f"Ports: {chr(44).join(map(str,ports)) or 'None'}", f"Vulns: {chr(44).join(data.get('vulns',{}).keys()) or 'None'}"])
        except Exception as e: result = f"Shodan: {str(e)}"
    emit(job_id, "module_done", {"module": "shodan", "result": result})
    return result


def module_virustotal(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "virustotal"})
    api_key = os.environ.get("VT_API_KEY","")
    if not api_key: result = "Add VT_API_KEY to Render Environment Variables."
    else:
        out, _, _ = run_cmd(f"curl -s --request GET --url 'https://www.virustotal.com/api/v3/domains/{target}' --header 'x-apikey: {api_key}' 2>/dev/null")
        try:
            data = json.loads(out); attrs = data.get("data",{}).get("attributes",{}); stats = attrs.get("last_analysis_stats",{}); cats = attrs.get("categories",{})
            result = f"Malicious: {stats.get('malicious',0)}\nSuspicious: {stats.get('suspicious',0)}\nHarmless: {stats.get('harmless',0)}\nReputation: {attrs.get('reputation','N/A')}\nCategories: {chr(44).join(set(cats.values())) if cats else 'N/A'}"
        except: result = out[:500] if out else "VirusTotal lookup failed."
    emit(job_id, "module_done", {"module": "virustotal", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE REGISTRY
# ══════════════════════════════════════════════════════════════════════════════



def module_cmv(target, job_id, ids=None):
    """CMV / Trucking Company Investigation Module."""
    emit(job_id, "module_start", {"module": "cmv"})
    import re as _re
    from urllib.parse import quote_plus as qp

    t = target.strip()
    t_clean = _re.sub(r'[\s\-#]+', ' ', t).strip()
    t_upper = t_clean.upper()

    dot_match = _re.match(r'^(?:US\s*DOT\s*#?\s*|DOT\s*#?\s*)?(\d{1,8})$', t_upper.replace(' ',''))
    mc_match  = _re.match(r'^(?:MC|MX)\s*#?\s*(\d+)$', t_upper.replace(' ',''))
    ff_match  = _re.match(r'^FF\s*#?\s*(\d+)$', t_upper.replace(' ',''))

    if dot_match:
        is_dot = True; is_mc = False
        dot_num = dot_match.group(1); mc_num = ""
        id_type = "USDOT"; id_display = f"USDOT #{dot_num}"
    elif mc_match:
        is_dot = False; is_mc = True
        mc_num = mc_match.group(1)
        mc_prefix = "MX" if t_upper.replace(' ','').startswith("MX") else "MC"
        dot_num = ""; id_type = mc_prefix; id_display = f"{mc_prefix} #{mc_num}"
    elif ff_match:
        is_dot = False; is_mc = True
        mc_num = ff_match.group(1); dot_num = ""
        id_type = "FF"; id_display = f"FF #{mc_num}"
    else:
        is_dot = False; is_mc = False
        dot_num = ""; mc_num = ""; id_type = "NAME"; id_display = t

    t_plus = qp(t)
    lines = [f"TARGET:  {t}", ""]
    if is_dot:
        lines += [f"TYPE:    USDOT Number — {id_display}",
                  "NOTE:    Accepted: 2033842 | DOT2033842 | USDOT 2033842 | USDOT#2033842"]
    elif is_mc:
        lines += [f"TYPE:    {id_type} Number — {id_display}",
                  "NOTE:    Accepted: MC123456 | MC-123456 | MC #123456 | MX123456"]
    else:
        lines += [f"TYPE:    Company Name Search — {id_display}",
                  "NOTE:    Enter DOT or MC number for direct parameterized links"]
    lines.append("")

    # STEP 1 — SAFER SNAPSHOT — NO LOGIN, LOADS DIRECTLY
    lines += ["=" * 50, "STEP 1 — FMCSA SAFER SNAPSHOT (no login required)", "=" * 50, ""]
    lines.append("Returns: legal name, DBA, address, phone, fleet size, cargo type,")
    lines.append("  safety rating, crash history (24 mo), inspection/OOS summary.")
    lines.append("")
    if is_dot:
        lines.append(f"[SAFER Snapshot — DOT {dot_num}]")
        lines.append(f"  https://safer.fmcsa.dot.gov/query.asp?query_type=queryCarrierSnapshot&query_param=USDOT&query_string={dot_num}")
    elif is_mc:
        lines.append(f"[SAFER Snapshot — MC {mc_num}]")
        lines.append(f"  https://safer.fmcsa.dot.gov/query.asp?query_type=queryCarrierSnapshot&query_param=MC_MX&query_string={mc_num}")
    else:
        lines.append("[SAFER General Search — MANUAL: enter DOT/MC/name]")
        lines.append("  https://safer.fmcsa.dot.gov/CompanySnapshot.aspx")
    lines.append("")

    # STEP 2 — L&I — CAPTCHA ON FIRST LOAD THEN DATA SHOWS
    lines += ["=" * 50, "STEP 2 — FMCSA LICENSING & INSURANCE (L&I)", "=" * 50, ""]
    lines.append("Returns: active authority, insurance on file, BOC-3 agent,")
    lines.append("  pending cancellations, authority history since 1995.")
    lines.append("NOTE: Complete CAPTCHA on first load — data displays after.")
    lines.append("")
    if is_dot:
        lines.append(f"[L&I — DOT {dot_num}]")
        lines.append(f"  https://li-public.fmcsa.dot.gov/LIVIEW/pkg_carrquery.prc_carrlist?n_dotno={dot_num}&pv_vpath=LIVIEW")
    elif is_mc:
        lines.append(f"[L&I — MC {mc_num}]")
        lines.append(f"  https://li-public.fmcsa.dot.gov/LIVIEW/pkg_carrquery.prc_carrlist?n_mcno={mc_num}&pv_vpath=LIVIEW")
    else:
        lines.append("[L&I — MANUAL: enter company name]")
        lines.append("  https://li-public.fmcsa.dot.gov/LIVIEW/pkg_carrquery.prc_carrlist")
    lines.append("")

    # STEP 3 — CSA SMS — REQUIRES FREE LOGIN
    lines += ["=" * 50, "STEP 3 — CSA SAFETY MEASUREMENT SYSTEM (SMS / BASIC SCORES)", "=" * 50, ""]
    lines.append("Returns 7 BASIC percentile scores (0-100, higher = worse):")
    lines.append("  Unsafe Driving | HOS Compliance | Vehicle Maintenance | Driver Fitness")
    lines.append("  Controlled Substance | Hazardous Materials | Crash Indicator")
    lines.append("NOTE: Crash Indicator visible to law enforcement only.")
    lines.append("REQUIRES FREE FMCSA ACCOUNT — register at ai.fmcsa.dot.gov")
    lines.append("ALERT threshold: score above 65th-75th percentile = FMCSA may intervene.")
    lines.append("")
    lines.append("[CSA SMS — MANUAL: login then enter DOT number]")
    lines.append("  https://ai.fmcsa.dot.gov/SMS/")
    lines.append("")

    # STEP 4 — CRASH DATA
    lines += ["=" * 50, "STEP 4 — CRASH HISTORY", "=" * 50, ""]
    lines.append("FMCSA threshold: fatality, injury requiring offsite medical, or tow-away.")
    lines.append("Property-damage-only crashes below threshold not included.")
    lines.append("")
    for nm, url in [
        ("FMCSA Large Truck Crash Facts — annual statistics report",
         "https://www.fmcsa.dot.gov/safety/data-and-statistics/large-truck-and-bus-crash-facts"),
        ("A&I Crash Statistics — carrier-level query — MANUAL: enter DOT",
         "https://ai.fmcsa.dot.gov/CrashStatistics"),
        ("DataQs — challenged crash records — requires free login",
         "https://dataqs.fmcsa.dot.gov/"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")

    # STEP 5 — DRIVER BACKGROUND
    lines += ["=" * 50, "STEP 5 — DRIVER BACKGROUND", "=" * 50, ""]
    lines.append("CDL Clearinghouse: drug/alcohol violations. Free limited query, account required.")
    lines.append("PSP: 3yr inspection + 5yr crash per driver. Requires driver consent + ~$10 fee.")
    lines.append("")
    for nm, url in [
        ("CDL Drug & Alcohol Clearinghouse — free limited query — account required",
         "https://clearinghouse.fmcsa.dot.gov/"),
        ("PSP Driver History — driver consent + fee required",
         "https://www.psp.fmcsa.dot.gov/"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")

    # STEP 6 — THIRD PARTY TOOLS — ALL MANUAL ENTRY
    lines += ["=" * 50, "STEP 6 — THIRD PARTY VERIFICATION (all manual entry)", "=" * 50, ""]
    for nm, url in [
        ("CarrierChk — free, no login — authority/insurance/safety rating",
         "https://carrierchk.com/"),
        ("FMCSA National Consumer Complaint Database",
         "https://nccdb.fmcsa.dot.gov/nccdb/home.aspx"),
        ("CVSA Inspection Standards reference",
         "https://cvsa.org/inspections/"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")

    # STEP 7 — CORPORATE STRUCTURE
    lines += ["=" * 50, "STEP 7 — CORPORATE STRUCTURE & LIABILITY CHAIN", "=" * 50, ""]
    lines.append("Shell companies, alter egos, common ownership — critical for liability.")
    lines.append("")
    for nm, url in [
        ("NM SOS Business Search",
         f"https://sos.nm.gov/business/business-search?name={t_plus}"),
        ("OpenCorporates — all 50 states",
         f"https://opencorporates.com/companies?q={t_plus}&jurisdiction_code=us"),
        ("SEC EDGAR full text search",
         f"https://efts.sec.gov/LATEST/search-index?q=%22{t_plus}%22"),
        ("PlainSite — federal + corporate litigation",
         f"https://www.plainsite.org/search/?q={t_plus}"),
        ("CourtListener — federal case search",
         f"https://www.courtlistener.com/?q={t_plus}&type=r"),
        ("PACER — federal litigation — MANUAL: free account required",
         "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")

    # STEP 8 — GOOGLE DORKS
    lines += ["=" * 50, "GOOGLE DORKS — CMV INVESTIGATION", "=" * 50, ""]
    search_term = dot_num if is_dot else mc_num if is_mc else t
    from urllib.parse import quote_plus
    for dork in [
        f'"{search_term}" FMCSA OR DOT OR "motor carrier"',
        f'"{search_term}" trucking accident OR crash OR collision',
        f'"{search_term}" "out of service" OR "safety violation" OR "BASIC"',
        f'"{search_term}" lawsuit OR litigation OR settlement OR verdict',
        f'"{search_term}" "hours of service" OR HOS OR fatigue',
        f'"{search_term}" "drug test" OR "controlled substance" OR DUI',
        f'"{search_term}" site:courtlistener.com',
        f'"{search_term}" site:plainsite.org',
    ]:
        lines.append(f"  {dork}")
        lines.append(f"  https://www.google.com/search?q={quote_plus(dork)}")
        lines.append("")

    # LIVE SAFER API CALL
    if is_dot and dot_num:
        lines += ["=" * 50, "LIVE SAFER DATA", "=" * 50, ""]
        try:
            import json, subprocess, re
            safer_api = f"https://safer.fmcsa.dot.gov/query.asp?query_type=queryCarrierSnapshot&query_param=USDOT&query_string={dot_num}"
            out, _, _ = (lambda cmd: subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15))(
                f"curl -s -L '{safer_api}' 2>/dev/null"
            )
            if out and len(out) > 200:
                def extract(pattern, text, default="N/A"):
                    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
                    return m.group(1).strip() if m else default
                legal  = extract(r'Legal Name[^:]*?:\s*</td>\s*<td[^>]*>(.*?)</td>', out)
                dba    = extract(r'DBA Name[^:]*?:\s*</td>\s*<td[^>]*>(.*?)</td>', out)
                status = extract(r'Operating Status[^:]*?:\s*</td>\s*<td[^>]*>(.*?)</td>', out)
                rating = extract(r'Safety Rating[^:]*?:\s*</td>\s*<td[^>]*>(.*?)</td>', out)
                if legal != "N/A":
                    lines += [f"Legal Name:    {legal}", f"DBA:           {dba}",
                              f"Status:        {status}", f"Safety Rating: {rating}", ""]
                else:
                    lines += ["SAFER data returned — open Step 1 link for full snapshot.", ""]
            else:
                lines += ["Open Step 1 link for SAFER snapshot.", ""]
        except:
            lines += ["Open Step 1 link for SAFER snapshot.", ""]

    lines += ["", "NOTE: All FMCSA carrier data is public federal record.",
              "  Driver PSP records require driver consent + permissible use documentation.", ""]
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "cmv", "result": result})
    return result



MODULE_MAP = {
    "people":            module_people_search,
    "public_records":    module_public_records,
    "property":          module_property,
    "skip_trace":        module_skip_trace,
    "social_media":      module_social_media,
    "social_footprint":  module_social_footprint,
    "photo_forensics":   module_photo_forensics,
    "hit_and_run":       module_hit_and_run,
    "username_search":   module_username_search,
    "phone":             module_phone,
    "email_investigate": module_email_investigate,
    "plate_lookup":      module_plate_lookup,
    "geolocation":       module_geolocation,
    "business":          module_business,
    "whois":             module_whois,
    "dns":               module_dns,
    "nmap":              module_nmap,
    "geoip":             module_geoip,
    "shodan":            module_shodan,
    "virustotal":        module_virustotal,
    "urlscan":           module_urlscan,
    "cmv":               module_cmv,
}

DOMAIN_IP_MODULES = {"whois","dns","nmap","geoip","shodan","virustotal","urlscan"}
PERSON_ONLY_MODULES = {"people","public_records","property","skip_trace","social_media","social_footprint","photo_forensics"}


def run_investigation(job_id, target, target_type, selected_modules, ids):
    try:
        cutoff = time.time() - 3600
        stale = [jid for jid, j in list(jobs.items()) if datetime.fromisoformat(j["started"]).timestamp() < cutoff]
        for jid in stale: del jobs[jid]
        tt = (target_type or "PERSON").upper()
        if tt == "PERSON":
            selected_modules = [m for m in selected_modules if m not in DOMAIN_IP_MODULES]
        elif tt in ("DOMAIN","IP"):
            selected_modules = [m for m in selected_modules if m not in PERSON_ONLY_MODULES]
        threads = []
        for mod_id in selected_modules:
            fn = MODULE_MAP.get(mod_id)
            if fn:
                t = threading.Thread(target=fn, args=(target, job_id, ids), daemon=True)
                threads.append(t); t.start()
        for t in threads: t.join(timeout=130)
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
            username = key[5:].lower(); parts = val.split(':')
            if len(parts) >= 3: users[username] = {'password':parts[0],'role':parts[1],'name':parts[2]}
    return users

def log_auth_event(username, action, detail, ip="unknown"):
    print(f"[AUTH] {action} | user={username} | {detail} | ip={ip} | time={datetime.utcnow().isoformat()}")

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.json; username = data.get("username","").strip().lower(); password = data.get("password","")
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    users = get_users(); user = users.get(username)
    if user and user["password"] == password:
        token = secrets.token_hex(32)
        active_sessions[token] = {"username":username,"role":user["role"],"name":user["name"],"created":datetime.utcnow().isoformat(),"ip":ip}
        log_auth_event(username,"LOGIN_SUCCESS",f"User {user['name']} authenticated",ip)
        return jsonify({"success":True,"token":token,"username":username,"role":user["role"],"name":user["name"]})
    else:
        log_auth_event(username,"LOGIN_FAILED","Invalid credentials",ip)
        return jsonify({"success":False,"error":"Invalid credentials"}), 401

@app.route("/api/auth/verify", methods=["POST"])
def verify_token():
    data = request.json; token = data.get("token",""); session = active_sessions.get(token)
    if session: return jsonify({"valid":True,"username":session["username"],"role":session["role"],"name":session["name"]})
    return jsonify({"valid":False}), 401

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    data = request.json; token = data.get("token",""); session = active_sessions.pop(token, None)
    if session: log_auth_event(session["username"],"LOGOUT","User logged out")
    return jsonify({"success":True})

@app.route("/api/auth/audit", methods=["POST"])
def get_audit():
    data = request.json; token = data.get("token",""); session = active_sessions.get(token)
    if not session or session.get("role") != "admin": return jsonify({"error":"Unauthorized"}), 403
    return jsonify({"message":"Audit log is written to Render server logs. Check Render dashboard -> Logs."})

@app.route("/api/investigate", methods=["GET","POST"])
def investigate():
    if request.method == "POST": data = request.json or {}
    else: data = request.args
    target = data.get("target","").strip()
    target_type = data.get("type","PERSON")
    modules_param = data.get("modules","")
    if isinstance(modules_param, str) and modules_param: selected_modules = modules_param.split(",")
    elif isinstance(modules_param, list): selected_modules = modules_param
    else: selected_modules = list(MODULE_MAP.keys())
    if not target: return jsonify({"error":"No target provided"}), 400
    ids = parse_identifiers(data)
    job_id = f"job_{int(time.time()*1000)}"
    new_job(job_id)
    threading.Thread(target=run_investigation, args=(job_id, target, target_type, selected_modules, ids), daemon=True).start()
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
                if event["type"] in ("done","error"): break
            except queue.Empty:
                yield f"data: {json.dumps({'type':'ping','data':{}})}\n\n"
                if jobs[job_id]["status"] in ("complete","error"): break
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/api/health")
def health():
    tools = {t: tool_available(t) for t in ["whois","dig","curl"]}
    return jsonify({"status":"ok","tools":tools})

@app.route("/")
def index():
    return "FIVE T OSINT Backend running. Connect your frontend."

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
