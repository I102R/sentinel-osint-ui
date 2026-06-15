"""
FIVE T OSINT Agent - Backend Server
Runs on Render.com (Python 3.10+)
All modules audited June 2026 — paywall tools removed, free tools verified.
v2.0 — Additional Identifiers (DOB/SSN/OLN), auto dorks integrated into all PERSON modules
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
    """Extract and validate Additional Identifier fields from request data.
    SSN raw value is cleared immediately after masking — never logged."""
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
    ids["ssn_value"] = ""  # clear raw value immediately
    return ids

# ── Dork Builder ──────────────────────────────────────────────────────────────
def build_dorks(name_part, location_part, state, city, ids):
    """Build all dork sets keyed by module: people, skip, social, court.
    Each value is a list of (label, query_string, badge) tuples."""
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
        ("NM SOS business",  f'{n} site:portal.sos.state.nm.us',                              ""),
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
        ("NM courts",        f'{n} site:caselookup.nmcourts.gov',                                       ""),
        ("Arrest/booking",   f'{n} arrest OR booking OR mugshot {st}'.strip(),                           ""),
        ("Traffic/PI",       f'{n} accident OR collision OR crash OR "hit and run" OR DUI {st}'.strip(), ""),
        ("Spreadsheet",      f'{n} {loc} filetype:xlsx OR filetype:csv'.strip(),                         ""),
    ]
    if yr:
        court.append(("DOB court filter", f'{n} "{yr}" court OR docket OR case'.strip(), "[DOB]"))
    if ssn_last4 and ssn_type == "last4":
        court.append(("SSN last4 confirm", f'{n} "{ssn_last4}"', "[SSN]"))

    return {"people": people, "skip": skip, "social": social, "court": court}


def render_dorks(dork_list, section_label):
    """Render a dork list into output lines."""
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
    """Render Additional Identifier source blocks. SSN raw value already cleared."""
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
        lines.append("[NM Voter Portal — DOB exact match]")
        lines.append("  https://voterportal.servis.sos.nm.gov/WhereToVote.aspx")
        lines.append("  TIP: Enter full DOB for exact-match — highest-confidence free address source")
        lines.append("")
        lines.append("[FamilyTreeNow — DOB filter]")
        lines.append(f"  https://www.familytreenow.com/search/people/results?first={first}&last={last}&birthyear={yr}")
        lines.append("")
        lines.append("[TruePeopleSearch — DOB narrow]")
        lines.append(f"  https://www.truepeoplesearch.com/results?name={quote_plus(name_part)}&birthdate={dob_str}")
        lines.append("")
        lines.append("[NM CourtLook — DOB filter]")
        lines.append("  https://caselookup.nmcourts.gov/caselookup/app")
        lines.append(f"  TIP: Use DOB {dob_str} to eliminate same-name hits in court results")
        lines.append("")
        if yr:
            lines.append("[SSN Issuance Validator — cross-check DOB/origin]  [DOB]")
            lines.append("  https://www.ssnvalidator.com")
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
        lines.append("[SSN Issuance State Lookup — free]")
        lines.append("  https://www.ssnvalidator.com")
        lines.append("  TIP: Confirms state of issuance and approximate year — validates subject age/origin")
        lines.append("")
        if ssn_type == "full":
            lines.append("[PACER — bankruptcy search with SSN filter]")
            lines.append("  https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf")
            lines.append("")
            lines.append("[NM Taxation & Revenue — TAP tax lien search]")
            lines.append("  https://tap.state.nm.us/tap/_/")
            lines.append("")
            lines.append("[IRS Tax Lien Search]")
            lines.append("  https://www.irs.gov/businesses/small-businesses-self-employed/search-for-a-lien")
            lines.append("")
            lines.append("[UCC Filings NM — debtor search]")
            lines.append("  https://portal.sos.state.nm.us/BFS/online/UCCFilings/SearchUCC")
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
            lines.append(f"[{mvd[0]} — DPPA formal request]  [OLN]")
            lines.append(f"  {mvd[1]}")
            lines.append(f"  Phone: {mvd[2]}")
            lines.append(f"  TIP: Submit DPPA permissible use request with OLN {oln}")
            lines.append("")
            lines.append("[AAMVA PDPS — national driver pointer system]  [OLN]")
            lines.append("  https://www.aamva.org/technology/systems/pdps/")
            lines.append("  TIP: CDL holders and serious traffic offenders — DPPA certification required")
            lines.append("")
            lines.append("[LexisNexis C.L.U.E. — prior insurance claims]  [OLN]")
            lines.append("  https://personalreports.lexisnexis.com")
            lines.append(f"  TIP: OLN {oln} — prior claims history material to PI damages")
            lines.append("")
        lines.append("[NM CourtLook — traffic/DUI filter]  [OLN]")
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
        ("FAMILYTREENOW BEST FREE",  f"https://www.familytreenow.com/search/people/results?first={first}&last={last}&state={state}"),
        ("TRUEPEOPLESEARCH",         f"https://www.truepeoplesearch.com/results?name={name_plus}&citystatezip={loc_plus}"),
        ("FASTPEOPLESEARCH",         f"https://www.fastpeoplesearch.com/name/{name_url}"),
        ("THATSTHEM 100% FREE",      f"https://thatsthem.com/name/{first}-{last}"),
        ("IDCRAWL social+records",   f"https://www.idcrawl.com/name/{first}-{last}"),
        ("PEEKYOU social+arrests",   f"https://www.peekyou.com/{first}_{last}"),
        ("SORTEDBYNAME.COM",         f"https://www.sortedbyname.com/search?q={name_plus}"),
        ("ZABASEARCH",               f"https://www.zabasearch.com/people/{first}+{last}/{state}/"),
        ("411.COM",                  f"https://www.411.com/name/{first}-{last}/{state}"),
        ("USPHONEBOOK",              f"https://www.usphonebook.com/{first}-{last}"),
        ("CLUSTRMAPS",               f"https://clustrmaps.com/person/{last}-{first}/"),
        ("SEARCHPEOPLEFREE",         f"https://www.searchpeoplefree.com/find/{first}-{last}"),
        ("NUWBER",                   f"https://nuwber.com/search?firstName={first}&lastName={last}&city={quote_plus(city)}&state={state}"),
        ("VOTERRECORDS.COM",         f"https://voterrecords.com/voters/{name_url}/1"),
        ("PUBLICRECORDS.ONLINE",     f"https://publicrecords.online/search/?first_name={first}&last_name={last}&state={state}"),
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
    lines.append("=" * 50); lines.append("COURT & PUBLIC RECORDS"); lines.append("=" * 50); lines.append("")
    for nm, url in [
        ("NM Courts CourtLook",   "https://caselookup.nmcourts.gov/caselookup/app"),
        ("PACER Federal Courts",  "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("CourtListener Free",    f"https://www.courtlistener.com/?q={name_plus}&type=p"),
        ("OpenSanctions",         f"https://www.opensanctions.org/search/?q={name_plus}"),
        ("VINE Offender NM",      "https://vinelink.vineapps.com/search/NM/Person"),
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


def module_public_records(target, job_id, ids=None):
    if ids is None: ids = {}
    emit(job_id, "module_start", {"module": "public_records"})
    name_part, location_part, first, last, state, city = parse_name_location(target)
    name_plus = quote_plus(name_part)
    lines = [f"TARGET: {name_part}", ""]
    lines.append("=" * 50); lines.append("FREE PEOPLE & ADDRESS RECORDS"); lines.append("=" * 50); lines.append("")
    for nm, url in [
        ("JudyRecords 740M Court Cases FREE", f"https://www.judyrecords.com/search?q={name_plus}"),
        ("FamilyTreeNow BEST FREE", f"https://www.familytreenow.com/search/people/results?first={first}&last={last}"),
        ("TruePeopleSearch", f"https://www.truepeoplesearch.com/results?name={name_plus}"),
        ("FastPeopleSearch", f"https://www.fastpeoplesearch.com/name/{name_part.replace(' ','-').lower()}"),
        ("ThatsThem 100% FREE", f"https://thatsthem.com/name/{first}-{last}"),
        ("IDCrawl", f"https://www.idcrawl.com/name/{first}-{last}"),
        ("ClustrMaps", f"https://clustrmaps.com/person/{last}-{first}/"),
        ("SearchPeopleFree", f"https://www.searchpeoplefree.com/find/{first}-{last}"),
        ("Nuwber", f"https://nuwber.com/search?firstName={first}&lastName={last}"),
        ("PublicRecords.Online", f"https://publicrecords.online/search/?first_name={first}&last_name={last}"),
        ("PublicRecordsNow", "https://www.publicrecordsnow.com/"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines.append("=" * 50); lines.append("ARREST & CRIMINAL RECORDS"); lines.append("=" * 50); lines.append("")
    for nm, url in [
        ("JudyRecords 740M US Cases FREE", f"https://www.judyrecords.com/search?q={name_plus}"),
        ("Trellis.law State Courts Free", f"https://trellis.law/person/{first}-{last}"),
        ("NM Courts CourtLook", "https://caselookup.nmcourts.gov/caselookup/app"),
        ("PACER Federal Courts", "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("CourtListener Free Federal", f"https://www.courtlistener.com/?q={name_plus}&type=p"),
        ("VINE Offender Search NM", "https://vinelink.vineapps.com/search/NM/Person"),
        ("NM Corrections Inmate", "https://www.cd.nm.gov/divisions/oid/offender-search/"),
        ("JailBase Arrest Bookings FREE", f"https://www.jailbase.com/search/?name_searched={name_plus}"),
        ("ArrestFacts", f"https://arrestfacts.com/search?name={name_plus}"),
        ("BustedMugshots", f"https://bustedmugshots.com/search?name={name_plus}"),
        ("MugshotSearch", f"https://www.mugshots.com/search?q={name_plus}"),
        ("OpenSanctions", f"https://www.opensanctions.org/search/?q={name_plus}"),
        ("Sex Offender Registry NM", "https://www.nmsexoffender.dps.nm.gov/"),
        ("Sex Offender Registry National", f"https://www.nsopw.gov/Search/Results?firstName={first}&lastName={last}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines.append("=" * 50); lines.append("VITAL RECORDS & GENEALOGY"); lines.append("=" * 50); lines.append("")
    for nm, url in [
        ("FamilySearch Free", f"https://www.familysearch.org/search/record/results?q.givenName={first}&q.surname={last}"),
        ("Ancestry limited free", f"https://www.ancestry.com/search/?name={first}_{last}"),
        ("FindAGrave", f"https://www.findagrave.com/memorial/search?firstname={first}&lastname={last}"),
        ("BillionGraves", f"https://billiongraves.com/search/results/#firstname={first}&lastname={last}"),
        ("Legacy.com Obituaries", f"https://www.legacy.com/obituaries/search?keyword={name_plus}"),
        ("NamUs Missing Persons", "https://www.namus.gov/MissingPersons/Search#/results"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines.append("=" * 50); lines.append("PROFESSIONAL LICENSES"); lines.append("=" * 50); lines.append("")
    for nm, url in [
        ("NM License Lookup", "https://www.rld.nm.gov/licensing-and-regulation/"),
        ("NM Medical Board", "https://www.nmmb.state.nm.us/"),
        ("NM Bar Association", "https://www.nmbar.org/"),
        ("NPPES Medical NPI", f"https://npiregistry.cms.hhs.gov/search?search_type=ind&first_name={first}&last_name={last}"),
        ("BLS License Lookup", "https://www.careeronestop.org/Toolkit/Credentials/find-licenses.aspx"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    dorks = build_dorks(name_part, location_part, state, city, ids)
    lines.extend(render_dorks(dorks["court"], "PUBLIC RECORDS"))
    try:
        out, _, _ = run_cmd(f"curl -s 'https://www.courtlistener.com/api/rest/v3/people/?name_last={last}&name_first={first}&format=json' 2>/dev/null", timeout=10)
        data = json.loads(out); count = data.get("count",0)
        if count > 0:
            lines += ["=" * 50, f"COURTLISTENER -- {count} RECORD(S) FOUND", "=" * 50, ""]
            for r in data.get("results",[])[:3]:
                lines.append(f"  Name: {r.get('name_full','N/A')}"); lines.append(f"  URL:  https://www.courtlistener.com{r.get('absolute_url','')}"); lines.append("")
    except: pass
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "public_records", "result": result})
    return result



def module_property(target, job_id, ids=None):
    if ids is None: ids = {}
    emit(job_id, "module_start", {"module": "property"})
    name_part, location_part, first, last, state, city = parse_name_location(target)
    name_plus = quote_plus(name_part)
    lines = [f"TARGET: {name_part}"]
    if location_part: lines.append(f"LOCATION: {location_part}")
    lines.append("")
    lines += ["="*50, "NEW MEXICO PROPERTY RECORDS -- ALL 33 COUNTIES", "="*50, ""]
    for nm, url in [
        ("Bernalillo County Assessor","https://assessor.bernco.gov/public.access/search/commonsearch.aspx?mode=owner"),
        ("Sandoval County Assessor","https://www.sandovalcountynm.gov/assessor/property-search/"),
        ("Santa Fe County Assessor","https://www.santafecountynm.gov/assessor"),
        ("Dona Ana County Assessor","https://assessor.donaanacounty.org/"),
        ("Valencia County Assessor","https://www.co.valencia.nm.us/assessor"),
        ("Chavez Roswell Assessor","https://www.chaves.nm.us/departments/assessor"),
        ("Lea County Assessor","https://www.leacountynm.gov/departments/assessor"),
        ("Otero County Assessor","https://www.oterocountynm.gov/county-offices/assessor"),
        ("San Juan County Assessor","https://www.sjcounty.net/departments/assessor"),
        ("McKinley County Assessor","https://www.co.mckinley.nm.us/assessor"),
        ("Eddy County Assessor","https://www.co.eddy.nm.us/137/Assessor"),
        ("Curry County Assessor","https://www.currycounty.org/assessor"),
        ("Roosevelt County Assessor","https://www.rooseveltcounty.com/assessor"),
        ("Sierra County Assessor","https://sierracountynm.gov/assessor/"),
        ("Grant County Assessor","https://www.grantcountynm.gov/assessor"),
        ("Luna County Assessor","https://www.lunacountynm.us/assessor"),
        ("Hidalgo County Assessor","https://www.hidalgocountynm.gov/assessor"),
        ("Socorro County Assessor","https://www.socorrocounty.org/assessor"),
        ("Lincoln County Assessor","https://www.lincolncountynm.net/assessor"),
        ("Torrance County Assessor","https://www.torrancecountynm.org/assessor"),
        ("Taos County Assessor","https://www.taoscounty.org/assessor"),
        ("Rio Arriba County Assessor","https://www.rio-arriba.org/assessor"),
        ("San Miguel County Assessor","https://www.co.san-miguel.nm.us/assessor"),
        ("Cibola County Assessor","https://www.cibolacounty.org/assessor"),
        ("Los Alamos County Assessor","https://www.losalamosnm.us/assessor"),
        ("NETR All NM Counties","https://publicrecords.netronline.com/state/NM"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "NATIONAL PROPERTY DATABASES -- FREE", "="*50, ""]
    for nm, url in [
        ("PropWire Free Owner Search", f"https://propwire.com/search?q={name_plus}"),
        ("County Office", f"https://www.countyoffice.org/property-records-search/?q={name_plus}"),
        ("FamilyTreeNow Address Hist", f"https://www.familytreenow.com/search/people/results?first={first}&last={last}"),
        ("NETR Online All 50 States","https://publicrecords.netronline.com/"),
        ("Realtor.com", f"https://www.realtor.com/realestateandhomes-search/{location_part.replace(' ','-') or 'new-mexico'}/"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "TAX & LIENS -- FREE", "="*50, ""]
    for nm, url in [
        ("NM Taxation & Revenue","https://tap.state.nm.us/tap/_/"),
        ("Federal Tax Liens PACER","https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("UCC Filings NM","https://portal.sos.state.nm.us/BFS/online/UCCFilings/SearchUCC"),
        ("Bankruptcy Search","https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    prop_dorks = [
        ("Property owner NM", f'"{name_part}" property owner New Mexico', ""),
        ("Real estate deed", f'"{name_part}" real estate deed', ""),
        ("Assessor parcel", f'"{name_part}" assessor parcel', ""),
        ("Foreclosure lien", f'"{name_part}" foreclosure lien', ""),
    ]
    if ids.get("zip"): prop_dorks.append(("ZIP property", f'"{name_part}" "{ids["zip"]}" property', ""))
    if ids.get("street"): prop_dorks.append(("Street anchor", f'"{name_part}" "{ids["street"]}"', ""))
    lines.extend(render_dorks(prop_dorks, "PROPERTY"))
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "property", "result": result})
    return result


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
    lines += ["="*50, "TIER 1 -- FREE SOURCES THAT SHOW FULL RESULTS", "="*50, ""]
    for nm, url in [
        ("FamilyTreeNow BEST FREE", f"https://www.familytreenow.com/search/people/results?first={first}&last={last}&state={state}"),
        ("TruePeopleSearch", f"https://www.truepeoplesearch.com/results?name={name_plus}&citystatezip={city_plus}+{state}"),
        ("FastPeopleSearch", f"https://www.fastpeoplesearch.com/name/{name_url}"),
        ("ThatsThem 100% FREE", f"https://thatsthem.com/name/{first}-{last}"),
        ("IDCrawl social+records", f"https://www.idcrawl.com/name/{first}-{last}"),
        ("ZabaSearch aliases+history", f"https://www.zabasearch.com/people/{first}+{last}/{state}/"),
        ("411.com", f"https://www.411.com/name/{first}-{last}/{state}"),
        ("USPhoneBook", f"https://www.usphonebook.com/{first}-{last}"),
        ("Clustrmaps", f"https://clustrmaps.com/person/{last}-{first}/"),
        ("SearchPeopleFree", f"https://www.searchpeoplefree.com/find/{first}-{last}"),
        ("Nuwber", f"https://nuwber.com/search?firstName={first}&lastName={last}&city={city_plus}&state={state}"),
        ("PublicRecords.Online", f"https://publicrecords.online/search/?first_name={first}&last_name={last}&state={state}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "TIER 2 -- VOTER REGISTRATION", "="*50, ""]
    lines.append("Voter registration = most reliable free address source.")
    lines.append("")
    lines.append(f"FEC: https://www.fec.gov/data/receipts/individual-contributions/?contributor_name={name_plus}")
    lines.append("")
    voter_portals = {
        "AL":("AL Voter Status","https://myinfo.alabamavotes.gov/VoterView/RegistrantSearch.do"),
        "AK":("AK Voter Search","https://myvoterinformation.alaska.gov/"),
        "AZ":("AZ Voter Registration","https://my.arizona.vote/VoterView/RegistrantSearch.do"),
        "CA":("CA Voter Status","https://voterstatus.sos.ca.gov/"),
        "CO":("CO Voter Portal","https://www.sos.state.co.us/voter/pages/pub/olvr/findVoterReg.xhtml"),
        "FL":("FL Voter Lookup","https://registration.elections.myflorida.com/CheckVoterStatus"),
        "GA":("GA Voter Status","https://mvp.sos.ga.gov/s/"),
        "NM":("NM Voter Portal","https://voterportal.servis.sos.nm.gov/WhereToVote.aspx"),
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
    voter = [("VoterRecords.com ALL STATES", f"https://voterrecords.com/voters/{name_url}/1")]
    sp = voter_portals.get(state.upper() if state else "NM")
    if sp: voter.append((f"{sp[0]} STATE PORTAL", sp[1]))
    else:  voter.append(("NVRA State Portal Finder","https://www.usa.gov/voter-registration-card"))
    voter.append(("Google Voter Reg Search", f"https://www.google.com/search?q=%22{name_plus}%22+%22voter+registration%22+%22{quote_plus(location_part)}%22"))
    for nm, url in voter:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "TIER 3 -- RELATIVES & ASSOCIATES", "="*50, ""]
    for nm, url in [
        ("FamilyTreeNow Relatives", f"https://www.familytreenow.com/search/people/results?first={first}&last={last}"),
        ("TruePeopleSearch Relatives", f"https://www.truepeoplesearch.com/results?name={name_plus}"),
        ("ClustrMaps Address Cluster", f"https://clustrmaps.com/person/{last}-{first}/"),
        ("ThatsThem Associates", f"https://thatsthem.com/name/{first}-{last}"),
        ("IDCrawl Social Connections", f"https://www.idcrawl.com/name/{first}-{last}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "TIER 4 -- ADDRESS VERIFICATION", "="*50, ""]
    for nm, url in [
        ("USPS Address Lookup","https://tools.usps.com/zip-code-lookup.htm?byaddress"),
        ("Melissa Address Check","https://www.melissa.com/v2/lookups/addresscheck/"),
        ("Google Maps Verify", f"https://www.google.com/maps/search/{name_plus}+{city_plus}+{state}"),
        ("Bernalillo County Assessor","https://assessor.bernco.gov/public.access/search/commonsearch.aspx?mode=owner"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "TIER 5 -- WORKPLACE & EMPLOYMENT", "="*50, ""]
    for nm, url in [
        ("LinkedIn People Search", f"https://www.linkedin.com/search/results/people/?keywords={name_plus}&origin=GLOBAL_SEARCH_HEADER"),
        ("Google LinkedIn + State", f"https://www.google.com/search?q=site:linkedin.com+%22{name_plus}%22+%22{state}%22"),
        ("Google Employer Dork", f"https://www.google.com/search?q=%22{name_plus}%22+employer+OR+works+OR+%22employed+at%22"),
        ("FEC Political Donations", f"https://www.fec.gov/data/receipts/individual-contributions/?contributor_name={name_plus}"),
        ("OpenSecrets Donor Search", f"https://www.opensecrets.org/donor-lookup/results?name={name_plus}"),
        ("NM Contractor License","https://www.rld.nm.gov/licensing-and-regulation/"),
        ("NPPES Medical NPI", f"https://npiregistry.cms.hhs.gov/search?search_type=ind&first_name={first}&last_name={last}"),
        ("NM Bar if attorney","https://www.nmbar.org/"),
        ("NM SOS Business Search", f"https://portal.sos.state.nm.us/BFS/online/CorporationFormation/SearchBusinesses?SearchCriteria={name_plus}"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines.extend(render_identifier_sources(ids, name_part, first, last))
    try:
        san_out, _, _ = run_cmd(f"curl -s 'https://api.opensanctions.org/search/default?q={name_plus}&schema=Person' 2>/dev/null", timeout=10)
        san_data = json.loads(san_out); results = san_data.get("results",[])
        lines += ["="*50, "LIVE SANCTIONS / WATCHLIST CHECK", "="*50, ""]
        if results:
            lines.append(f"WARNING: {len(results)} MATCH(ES) FOUND")
            for r in results[:5]: lines.append(f"  * {r.get('caption','?')} -- Score: {r.get('score','?')}")
        else: lines.append("No matches found on sanctions/watchlists")
        lines.append("")
    except: pass
    dorks = build_dorks(name_part, location_part, state, city, ids)
    lines.extend(render_dorks(dorks["skip"], "SKIP TRACE"))
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "skip_trace", "result": result})
    return result


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
            ("People Search", f"https://www.facebook.com/search/people/?q={name_plus}"),
            ("Posts mentioning", f"https://www.facebook.com/search/posts/?q={name_plus}"),
            ("Photos tagged", f"https://www.facebook.com/search/photos/?q={name_plus}"),
            ("Check-ins", f"https://www.facebook.com/search/places/?q={name_plus}"),
            ("Groups", f"https://www.facebook.com/search/groups/?q={name_plus}"),
            ("Events", f"https://www.facebook.com/search/events/?q={name_plus}"),
            ("Marketplace", f"https://www.facebook.com/marketplace/search/?query={name_plus}"),
            ("Sowsearch Deep", f"https://sowsearch.info/search?q={name_plus}"),
            ("Google FB Search", f"https://www.google.com/search?q=site:facebook.com+%22{name_plus}%22"),
        ]),
        ("INSTAGRAM INTELLIGENCE", [
            ("Profile Search", f"https://www.instagram.com/explore/search/keyword/?q={name_plus}"),
            ("Hashtag Search", f"https://www.instagram.com/explore/tags/{name_plus.replace('+','')}/"),
            ("Google IG Search", f"https://www.google.com/search?q=site:instagram.com+%22{name_plus}%22"),
        ]),
        ("TWITTER/X INTELLIGENCE", [
            ("People Search", f"https://twitter.com/search?q=%22{name_plus}%22&f=user"),
            ("Recent Posts", f"https://twitter.com/search?q=%22{name_plus}%22&f=live"),
            ("Top Posts", f"https://twitter.com/search?q=%22{name_plus}%22&f=top"),
            ("Near ABQ", f"https://twitter.com/search?q=%22{name_plus}%22+near%3A%22Albuquerque%22"),
        ]),
        ("LINKEDIN INTELLIGENCE", [
            ("People Search", f"https://www.linkedin.com/search/results/people/?keywords={name_plus}"),
            ("Posts Search", f"https://www.linkedin.com/search/results/content/?keywords={name_plus}"),
            ("Google LI Search", f"https://www.google.com/search?q=site:linkedin.com/in+%22{name_plus}%22"),
        ]),
        ("TIKTOK / YOUTUBE / REDDIT", [
            ("TikTok User", f"https://www.tiktok.com/search/user?q={name_plus}"),
            ("YouTube Channel", f"https://www.youtube.com/results?search_query={name_plus}&sp=EgIQAg%253D%253D"),
            ("Reddit User", f"https://www.reddit.com/search/?q=%22{name_quoted}%22&type=user"),
            ("Reddit Posts", f"https://www.reddit.com/search/?q=%22{name_quoted}%22"),
        ]),
        ("OTHER PLATFORMS", [
            ("Snapchat", f"https://www.snapchat.com/add/{first.lower()}{last.lower()}"),
            ("Pinterest", f"https://www.pinterest.com/search/people/?q={name_plus}"),
            ("Nextdoor","https://nextdoor.com/find-neighbors/"),
            ("Venmo", f"https://venmo.com/{first.lower()}{last.lower()}"),
            ("Cash App", f"https://cash.app/${first.lower()}{last.lower()}"),
        ]),
    ]:
        lines += ["="*50, section, "="*50, ""]
        for lbl, url in items:
            lines.append(f"[{lbl}]"); lines.append(f"  {url}"); lines.append("")
    dorks = build_dorks(name_quoted, location_part, state, city, ids)
    lines.extend(render_dorks(dorks["social"], "SOCIAL MEDIA"))
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "social_media", "result": result})
    return result


def module_social_footprint(target, job_id, ids=None):
    if ids is None: ids = {}
    emit(job_id, "module_start", {"module": "social_footprint"})
    name_part, location_part, first, last, state, city = parse_name_location(target)
    name_plus = quote_plus(name_part)
    uvars = [f"{first.lower()}{last.lower()}", f"{first.lower()}.{last.lower()}", f"{first.lower()}_{last.lower()}", f"{first.lower()}{last.lower()[:3]}", f"{first.lower()[0]}{last.lower()}"] if first and last else []
    lines = [f"TARGET:   {name_part}"]
    if location_part: lines.append(f"LOCATION: {location_part}")
    lines += ["", "="*50, "DIRECT PROFILE ATTEMPTS -- USERNAME VARIATIONS", "="*50, ""]
    for uname in uvars[:4]:
        lines.append(f"Username: {uname}")
        for plat, url in [("Facebook",f"https://www.facebook.com/{uname}"),("Instagram",f"https://www.instagram.com/{uname}/"),("Twitter/X",f"https://twitter.com/{uname}"),("TikTok",f"https://www.tiktok.com/@{uname}"),("LinkedIn",f"https://www.linkedin.com/in/{uname}")]:
            lines.append(f"  [{plat}]  {url}")
        lines.append("")
    lines += ["="*50, "REAL-TIME SOCIAL SEARCH -- FREE TOOLS", "="*50, ""]
    for nm, url in [
        ("Social Searcher FREE", f"https://www.social-searcher.com/social-buzz/?q={name_plus}"),
        ("Social Catfish", f"https://socialcatfish.com/search/?q={name_plus}"),
        ("PeekYou social+arrests", f"https://www.peekyou.com/{first.lower()}_{last.lower()}"),
        ("Sowsearch FB Deep", f"https://sowsearch.info/search?q={name_plus}"),
        ("Boardreader forums", f"https://boardreader.com/s/{name_plus}.html"),
        ("WhatsMyName usernames", f"https://whatsmyname.app/?q={first.lower()}{last.lower()}"),
        ("IDCrawl social+records", f"https://www.idcrawl.com/name/{first.lower()}-{last.lower()}"),
        ("Epieos email->social", f"https://epieos.com/?q={name_plus}&t=name"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "LINKEDIN DEEP SEARCH", "="*50, ""]
    for nm, url in [
        ("LinkedIn People Search", f"https://www.linkedin.com/search/results/people/?keywords={name_plus}"),
        ("LinkedIn + NM filter", f"https://www.linkedin.com/search/results/people/?keywords={name_plus}&geoUrn=%5B%22102095887%22%5D"),
        ("Google LI Profile", f"https://www.google.com/search?q=site:linkedin.com/in+%22{name_plus}%22"),
        ("Google LI + Location", f"https://www.google.com/search?q=site:linkedin.com+%22{name_plus}%22+%22New+Mexico%22"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "REVERSE IMAGE & FACE SEARCH -- FREE", "="*50, "", "Upload subject photo to find additional profiles.", ""]
    for nm, url in [
        ("Yandex BEST for faces","https://yandex.com/images/"),
        ("PimEyes face search","https://pimeyes.com/en"),
        ("Lenso.ai face search","https://lenso.ai/en"),
        ("Google Reverse Image","https://images.google.com/"),
        ("TinEye","https://tineye.com/"),
        ("Bing Visual Search","https://www.bing.com/images/search?view=detailv2&iss=sbi"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    dorks = build_dorks(name_part, location_part, state, city, ids)
    lines.extend(render_dorks(dorks["social"], "SOCIAL FOOTPRINT"))
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "social_footprint", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# NON-PERSON MODULES — ids=None added, otherwise unchanged from v1
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
    lines += ["="*50, "STEP 1 -- IDENTIFY VEHICLE FROM PHOTO/VIDEO", "="*50, ""]
    for nm, url in [
        ("Carnet.ai ID make/model from photo","https://carnet.ai/"),
        ("Remini clean blurry images","https://app.remini.ai/"),
        ("LetsEnhance upscale low-res","https://letsenhance.io/"),
        ("Google Reverse Image","https://images.google.com/"),
        ("Yandex Reverse Image better for vehicles","https://yandex.com/images/"),
        ("TinEye find where image appears","https://tineye.com/"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "STEP 2 -- VIN & PLATE LOOKUP", "="*50, ""]
    if is_vin:
        vin_links = [
            ("NHTSA VIN Decoder FREE specs/recalls", f"https://vpic.nhtsa.dot.gov/decoder/Car/{plate}/0"),
            ("Driving-Tests.org VIN 100% free", f"https://driving-tests.org/vin-decoder/?vin={plate}"),
            ("EpicVIN free basic decode", f"https://epicvin.com/vin-decoder?vin={plate}"),
            ("VinFreeCheck free specs", f"https://www.vinfreecheck.com/?vin={plate}"),
            ("NICB VINCheck FREE stolen/salvage","https://www.nicb.org/vincheck"),
            ("NHTSA Recalls by VIN", f"https://www.nhtsa.gov/vehicle/{plate}///complaints"),
            ("NMVTIS Title Check","https://www.vehiclehistory.gov/"),
        ]
    else:
        vin_links = [
            ("NHTSA VIN Decoder FREE","https://vpic.nhtsa.dot.gov/decoder/"),
            ("Driving-Tests.org VIN free","https://driving-tests.org/vin-decoder/"),
            ("EpicVIN Plate Lookup", f"https://epicvin.com/license-plate-lookup?plate={plate}&state={state}"),
            ("Faxvin Plate Search", f"https://www.faxvin.com/license-plate-lookup/{state.lower()}/{plate}"),
            ("VehicleHistory.com Plate", f"https://www.vehiclehistory.com/license-plate-search?plate={plate}&state={state}"),
            ("NICB VINCheck FREE","https://www.nicb.org/vincheck"),
            ("NMVTIS Title Check","https://www.vehiclehistory.gov/"),
            ("NHTSA Recalls","https://www.nhtsa.gov/recalls"),
        ]
    for nm, url in vin_links:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "STEP 3 -- SOCIAL MEDIA PLATE SEARCH", "="*50, ""]
    for nm, url in [
        ("Facebook Posts", f"https://www.facebook.com/search/posts/?q={plate}"),
        ("Instagram", f"https://www.instagram.com/explore/search/keyword/?q={plate}"),
        ("Twitter/X Live", f"https://twitter.com/search?q=%22{plate}%22&f=live"),
        ("Reddit", f"https://www.reddit.com/search/?q=%22{plate}%22"),
        ("YouTube", f"https://www.youtube.com/results?search_query=%22{plate}%22"),
        ("Google Images", f"https://www.google.com/search?tbm=isch&q=%22{plate}%22+New+Mexico"),
        ("Nextdoor local witness","https://nextdoor.com/"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "STEP 4 -- WITNESS & DASHCAM", "="*50, ""]
    for nm, url in [
        ("r/NewMexico hit & run","https://www.reddit.com/r/newmexico/search/?q=hit+and+run&sort=new"),
        ("r/Albuquerque","https://www.reddit.com/r/Albuquerque/search/?q=hit+and+run&sort=new"),
        ("Google News ABQ hit and run","https://www.google.com/search?q=%22hit+and+run%22+%22albuquerque%22&tbm=nws"),
        ("ABQ Journal","https://www.abqjournal.com/?s=hit+run"),
        ("Waze Incident Map","https://www.waze.com/livemap"),
    ]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "STEP 5 -- OWNER IDENTIFICATION", "="*50, ""]
    lines += ["Once make/model/plate confirmed:","  -> NM MVD DPPA request for registered owner","  -> Run owner name through SKIP TRACE module","  -> NM Courts: https://caselookup.nmcourts.gov/caselookup/app",""]
    lines += ["="*50, "GOOGLE DORKS", "="*50, ""]
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
    lines += ["="*50, "REVERSE IMAGE SEARCH -- FREE", "="*50, ""]
    if is_url:
        rev = [("Google Reverse Image",f"https://images.google.com/searchbyimage?image_url={te}"),("TinEye",f"https://tineye.com/search?url={te}"),("Yandex Best for faces",f"https://yandex.com/images/search?url={te}&rpt=imageview"),("Bing Visual Search",f"https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:{te}"),("Lenso.ai",f"https://lenso.ai/en?url={te}"),("PimEyes","https://pimeyes.com/en")]
    else:
        rev = [("Google Reverse Image","https://images.google.com/"),("TinEye","https://tineye.com/"),("Yandex Best for faces","https://yandex.com/images/"),("Bing Visual Search","https://www.bing.com/images/search?view=detailv2&iss=sbi"),("Lenso.ai","https://lenso.ai/en"),("PimEyes","https://pimeyes.com/en")]
    for nm, url in rev: lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "PHOTO METADATA EXTRACTION -- FREE", "="*50, ""]
    for nm, url in [("Jeffrey EXIF Viewer","http://exif.regex.info/exif.cgi"),("ExifTool Online","https://exiftool.org/"),("Metadata2Go","https://www.metadata2go.com/"),("FotoForensics","https://fotoforensics.com/"),("Forensically","https://29a.ch/photo-forensics/"),("ImageEdited","https://imageedited.com/")]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "VIDEO FORENSICS -- FREE", "="*50, ""]
    for nm, url in [("InVID WeVerify","https://www.invid-project.eu/tools-and-services/invid-verification-plugin/"),("YouTube DataViewer","https://citizenevidence.amnestyusa.org/"),("TrueMedia.org","https://www.truemedia.org/")]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "GEOLOCATION FROM PHOTOS -- FREE", "="*50, ""]
    for nm, url in [("SunCalc shadow/time analysis","https://www.suncalc.org/"),("Google Maps Street View","https://www.google.com/maps"),("Bing Maps","https://www.bing.com/maps"),("Google Earth Web","https://earth.google.com/web/"),("Overpass Turbo","https://overpass-turbo.eu/"),("GeoHack","https://geohack.toolforge.org/")]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    if is_url:
        lines += ["="*50, "AUTOMATED METADATA EXTRACTION", "="*50, ""]
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
        ("MAP INTELLIGENCE -- FREE", [("Google Maps",f"https://www.google.com/maps/search/{lp}"),("Google Street View",f"https://www.google.com/maps?q={lp}&layer=c"),("Google Earth Web",f"https://earth.google.com/web/search/{lp}"),("Bing Maps",f"https://www.bing.com/maps?q={lp}"),("OpenStreetMap",f"https://www.openstreetmap.org/search?query={lp}"),("Apple Maps",f"https://maps.apple.com/?q={lp}")]),
        ("SATELLITE & HISTORICAL -- FREE", [("Google Earth Historical",f"https://earth.google.com/web/search/{lp}"),("Sentinel Hub","https://www.sentinel-hub.com/explore/eobrowser/"),("USGS EarthExplorer","https://earthexplorer.usgs.gov/"),("NASA Worldview","https://worldview.earthdata.nasa.gov/"),("Bing Birds Eye",f"https://www.bing.com/maps?q={lp}&style=b")]),
        ("SPECIALIZED TOOLS -- FREE", [("Wigle.net WiFi Networks","https://wigle.net/search#fullSearch"),("Overpass Turbo","https://overpass-turbo.eu/"),("SunCalc Sun Position","https://www.suncalc.org/"),("CalcMaps Distance/Area","https://www.calcmaps.com/map-distance/")]),
    ]:
        lines += ["="*50, section, "="*50, ""]
        for nm, url in items: lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    if _re.match(r'^\d+\.\d+\.\d+\.\d+$', target):
        try:
            out, _, _ = run_cmd(f"curl -s 'https://ipapi.co/{target}/json/' 2>/dev/null")
            data = json.loads(out)
            lines += ["="*50, "IP GEOLOCATION (LIVE)", "="*50, ""]
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
    lines += ["="*50, "AUTOMATED SCANNER", "="*50, ""]
    out, _, _ = run_cmd(f"python3 -m sherlock {target} --timeout 8 2>/dev/null", timeout=120)
    if out and "not found" not in out.lower():
        lines += ["[SHERLOCK -- 300+ PLATFORMS]", out, ""]
    out2, _, rc2 = run_cmd(f"python3 -m maigret {target} --top-sites 50 2>/dev/null", timeout=120)
    if out2 and rc2 == 0:
        lines += ["[MAIGRET -- FULL DOSSIER]", out2[:2000], ""]
    lines += ["="*50, "MANUAL USERNAME SEARCH -- FREE", "="*50, ""]
    for nm, url in [("WhatsMyName FREE",f"https://whatsmyname.app/?q={target}"),("IDCrawl FREE",f"https://www.idcrawl.com/{target}"),("UserSearch.org",f"https://usersearch.org/results_normal.php?q={target}"),("Namechk",f"https://namechk.com/{target}"),("Instant Username",f"https://instantusername.com/#/{target}"),("Sherlock Web","https://sherlock-project.github.io/")]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "PLATFORM DIRECT CHECKS", "="*50, ""]
    for nm, url in [("Twitter/X",f"https://twitter.com/{target}"),("Instagram",f"https://www.instagram.com/{target}/"),("TikTok",f"https://www.tiktok.com/@{target}"),("YouTube",f"https://www.youtube.com/@{target}"),("Reddit",f"https://www.reddit.com/user/{target}"),("GitHub",f"https://github.com/{target}"),("LinkedIn",f"https://www.linkedin.com/in/{target}"),("Pinterest",f"https://www.pinterest.com/{target}/"),("Twitch",f"https://www.twitch.tv/{target}"),("Snapchat",f"https://www.snapchat.com/add/{target}"),("Venmo",f"https://venmo.com/{target}"),("Cash App",f"https://cash.app/${target}"),("Telegram",f"https://t.me/{target}"),("Patreon",f"https://www.patreon.com/{target}"),("Linktree",f"https://linktr.ee/{target}")]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "username_search", "result": result})
    return result


def module_phone(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "phone"})
    clean = target.replace("-","").replace("(","").replace(")","").replace(" ","").replace("+1","").strip()
    formatted = f"({clean[:3]}) {clean[3:6]}-{clean[6:]}" if len(clean)==10 else target
    phone_plus1 = f"+1{clean}" if len(clean)==10 else target
    lines = [f"TARGET:    {formatted}", f"CLEANED:   {clean}", ""]
    ipqs_key = os.environ.get("IPQS_API_KEY",""); nv_key = os.environ.get("NUMVERIFY_API_KEY","")
    if ipqs_key:
        try:
            d = json.loads(run_cmd(f"curl -s 'https://www.ipqualityscore.com/api/json/phone/{ipqs_key}/{clean}' 2>/dev/null", timeout=10)[0])
            if d.get("success"):
                lines += ["=== CARRIER INTELLIGENCE (IPQS) ===", f"Valid: {d.get('valid','N/A')}", f"Line Type: {d.get('line_type','N/A')}", f"Carrier: {d.get('carrier','N/A')}", f"Risky: {d.get('risky',False)}", f"VoIP: {d.get('VOIP',False)}", f"Prepaid: {d.get('prepaid',False)}", ""]
        except: pass
    if not ipqs_key and not nv_key:
        lines += ["Add IPQS_API_KEY or NUMVERIFY_API_KEY to Render env vars for live carrier data.", ""]
    lines += ["="*50, "FREE REVERSE LOOKUP SITES", "="*50, "", "NOTE: SpyDialer calls the number silently. Target may see missed call. Use intentionally.", ""]
    for nm, url in [("SPYDIALER FREE name via voicemail",f"https://www.spydialer.com/default.aspx?phone={clean}"),("NUMLOOKUP FREE owner name carrier",f"https://www.numlookup.com/?number={clean}"),("ANYWHO free directory",f"https://www.anywho.com/reverse-lookup/{clean}"),("TRUEPEOPLESEARCH FREE",f"https://www.truepeoplesearch.com/results?phoneno={clean}"),("THATSTHEM FREE",f"https://thatsthem.com/phone/{clean}"),("FASTPEOPLESEARCH",f"https://www.fastpeoplesearch.com/phone/{clean}"),("FONEFINDER carrier lookup",f"https://fonefinder.net/findphone.php?areacode={clean[:3]}&exchange={clean[3:6]}&thenumber={clean[6:]}"),("USPHONEBOOK",f"https://www.usphonebook.com/{clean}"),("411.COM",f"https://www.411.com/phone/{clean}"),("TRUECALLER community ID",f"https://www.truecaller.com/search/us/{clean}")]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "SPAM & REPORT DATABASES", "="*50, ""]
    for nm, url in [("800NOTES",f"https://800notes.com/Phone.aspx/{clean}"),("CALLERCENTER",f"https://callercenter.com/{clean}"),("NOMOROBO",f"https://www.nomorobo.com/lookup/{clean}"),("SPAMCALLS",f"https://spamcalls.net/en/search?n={clean}"),("WHOCALLEDUS",f"https://whocalledus.com/calls/{clean}/")]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "GOOGLE DORKS", "="*50, ""]
    for dork in [f'"{formatted}"', f'"{clean}"', f'"{phone_plus1}"', f'"{formatted}" name address', f'"{clean}" site:facebook.com', f'"{clean}" spam OR scam OR fraud']:
        lines.append(f"  {dork}"); lines.append(f"  https://www.google.com/search?q={quote_plus(dork)}"); lines.append("")
    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "phone", "result": result})
    return result


def module_email_investigate(target, job_id, ids=None):
    emit(job_id, "module_start", {"module": "email_investigate"})
    lines = [f"TARGET EMAIL: {target}", ""]
    out, _, rc = run_cmd(f"python3 -m holehe {target} --only-used 2>/dev/null", timeout=120)
    if out and "holehe" not in out.lower() and "error" not in out.lower():
        lines += ["="*50, "HOLEHE -- ACCOUNT DETECTION 120+ SITES", "="*50, "", out, ""]
    try:
        data = json.loads(run_cmd(f"curl -s 'https://emailrep.io/{target}' -H 'User-Agent: fivet-osint' 2>/dev/null", timeout=10)[0])
        details = data.get("details",{})
        lines += ["="*50, "EMAIL REPUTATION -- FREE", "="*50, "", f"Reputation: {data.get('reputation','N/A')}", f"Suspicious: {data.get('suspicious',False)}", f"Blacklisted: {details.get('blacklisted',False)}", f"Data Breach: {details.get('data_breach',False)}", f"Disposable: {details.get('disposable',False)}", f"Profiles: {chr(44).join(details.get('profiles',[])) or 'None detected'}", ""]
    except: pass
    try:
        domain = target.split("@")[1]
        dns_out = run_cmd(f"dig +short A {domain} 2>/dev/null")[0]
        mx_out = run_cmd(f"dig +short MX {domain} 2>/dev/null")[0]
        if dns_out or mx_out:
            lines += ["="*50, f"EMAIL DOMAIN INTEL: {domain}", "="*50, ""]
            if dns_out: lines.append(f"Domain IP:   {dns_out.split()[0]}")
            if mx_out: lines.append(f"Mail Server: {mx_out}")
            lines.append("")
    except: pass
    lines += ["="*50, "FREE LOOKUP SITES", "="*50, ""]
    for nm, url in [("TRUEPEOPLESEARCH FREE",f"https://www.truepeoplesearch.com/results?emailaddress={target}"),("THATSTHEM FREE",f"https://thatsthem.com/email/{target}"),("EMAILREP reputation",f"https://emailrep.io/{target}"),("HUNTER.IO verify",f"https://hunter.io/email-verifier/{target}"),("HAVEIBEENPWNED breaches",f"https://haveibeenpwned.com/account/{target}"),("DEHASHED breaches",f"https://www.dehashed.com/search?query={target}"),("EPIEOS social lookup",f"https://epieos.com/?q={target}&t=email")]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "GOOGLE DORKS", "="*50, ""]
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
    lines += ["="*50, "FREE VEHICLE LOOKUP", "="*50, ""]
    for nm, url in [("VehicleHistory.com",f"https://www.vehiclehistory.com/license-plate-search?plate={plate}&state={state}"),("Faxvin Plate Search",f"https://www.faxvin.com/license-plate-lookup/{state.lower()}/{plate}"),("NICB VINCheck stolen check","https://www.nicb.org/vincheck"),("NHTSA Recalls","https://www.nhtsa.gov/recalls"),("NMVTIS Vehicle History","https://www.vehiclehistory.gov/")]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, f"{state} MVD RECORDS REQUEST", "="*50, "", "Submit DPPA request to state MVD for registered owner info.", ""]
    mvd_links = {"NM":("NM MVD","https://www.mvd.newmexico.gov/","(888) 683-4636"),"AZ":("AZ MVD","https://www.azdot.gov/motor-vehicles","(602) 712-7355"),"TX":("TX DMV","https://www.txdmv.gov/","(888) 368-4689"),"CO":("CO DMV","https://dmv.colorado.gov/","(303) 205-5600"),"CA":("CA DMV","https://www.dmv.ca.gov/","(800) 777-0133")}
    mn, mu, mp = mvd_links.get(state,("State MVD","https://www.vehiclehistory.gov/","Check state DMV website"))
    lines += [f"[{mn} DPPA request]", f"  {mu}", f"  Phone: {mp}", ""]
    lines += ["="*50, "SOCIAL MEDIA PLATE SEARCH", "="*50, ""]
    for nm, url in [("Facebook",f"https://www.facebook.com/search/posts/?q={plate}"),("Instagram",f"https://www.instagram.com/explore/search/keyword/?q={plate}"),("Twitter/X",f"https://twitter.com/search?q=%22{plate}%22&f=live"),("Reddit",f"https://www.reddit.com/search/?q=%22{plate}%22")]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "DPPA PERMISSIBLE PURPOSES Law Firm", "="*50, "", "  * Litigation or investigation in anticipation of litigation", "  * Service of process", "  * Licensed private investigator research", "  * Insurance claims investigation", "  * Locating missing persons or witnesses", "", "Cite: 18 U.S.C. 2721(b)"]
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
            lines += ["="*50, "OPENCORPORATES -- NM RESULTS FREE", "="*50, ""]
            for c in companies[:5]:
                co = c.get("company",{})
                lines += [f"  Name: {co.get('name','N/A')}", f"  Status: {co.get('current_status','N/A')}", f"  Registered: {co.get('incorporation_date','N/A')}", f"  URL: {co.get('opencorporates_url','N/A')}", ""]
        else: lines += ["No NM results from OpenCorporates.", ""]
    except Exception as e: lines += [f"OpenCorporates: {str(e)}", ""]
    lines += ["="*50, "SECRETARY OF STATE -- FREE", "="*50, ""]
    for nm, url in [("NM SOS Business Search",f"https://portal.sos.state.nm.us/BFS/online/CorporationFormation/SearchBusinesses?SearchCriteria={name_plus}"),("NM SOS alternate","https://businessportal.sos.nm.gov/"),("AZ SOS",f"https://ecorp.azcc.gov/BusinessSearch/BusinessSearch?SearchTerm={name_plus}"),("CO SOS",f"https://www.sos.state.co.us/biz/BusinessEntityCriteriaExt.do?nameTyp=ENT&entityName={name_plus}"),("TX SOS","https://mycpa.cpa.state.tx.us/coa/Index.html")]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "FEDERAL DATABASES -- FREE", "="*50, ""]
    for nm, url in [("SAM.gov Federal Contractors",f"https://sam.gov/search/?keywords={name_plus}&sort=relevanceScore&index=ei&is_active=true&page=1"),("SEC EDGAR Public Companies",f"https://www.sec.gov/cgi-bin/browse-edgar?company={name_plus}&CIK=&type=&dateb=&owner=include&count=40&search_text=&action=getcompany"),("OpenCorporates All States",f"https://opencorporates.com/companies?q={name_plus}&jurisdiction_code=us"),("PACER Business Search","https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),("BBB Albuquerque",f"https://www.bbb.org/search?find_text={name_plus}&find_loc=Albuquerque%2C+NM")]:
        lines.append(f"[{nm}]"); lines.append(f"  {url}"); lines.append("")
    lines += ["="*50, "BUSINESS INTELLIGENCE -- FREE", "="*50, ""]
    for nm, url in [("LinkedIn Company",f"https://www.linkedin.com/search/results/companies/?keywords={name_plus}"),("Yelp Business",f"https://www.yelp.com/search?find_desc={name_plus}&find_loc=Albuquerque%2C+NM"),("Google Business",f"https://www.google.com/search?q={name_plus}+Albuquerque+NM+business"),("Bizapedia NM","https://www.bizapedia.com/nm/"),("Corporationwiki",f"https://www.corporationwiki.com/search/results?term={name_plus}"),("OpenCorporates Officers",f"https://opencorporates.com/officers?q={name_plus}")]:
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
# MODULE REGISTRY — dorks tab retired, auto-integrated into PERSON modules
# ══════════════════════════════════════════════════════════════════════════════

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
}

DOMAIN_IP_MODULES = {"whois","dns","nmap","geoip","shodan","virustotal"}
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
