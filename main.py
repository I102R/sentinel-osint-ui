"""
FIVE T OSINT Agent - Backend Server
Runs on Render.com (Python 3.10+)
All modules audited June 2026 — paywall tools removed, free tools verified.
"""

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import subprocess, threading, json, os, time, queue, socket, re
import urllib.request, urllib.parse, urllib.error
import tempfile, shutil
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
# Flask-CORS handles CORS headers and OPTIONS preflight on its own. A manual
# after_request/before_request handler previously duplicated the
# Access-Control-Allow-Origin header, which broke login — removed.

jobs = {}

def new_job(job_id, owner=""):
    jobs[job_id] = {
        "status": "running",
        "started": datetime.utcnow().isoformat(),
        "results": {},
        "events": queue.Queue(),
        "owner": owner,
    }

def emit(job_id, event_type, data):
    if job_id in jobs:
        jobs[job_id]["events"].put({"type": event_type, "data": data})

def run_cmd(cmd, timeout=60):
    # cmd MUST be a list of arguments (no shell). This avoids shell-injection
    # and quoting bugs (e.g. names with apostrophes).
    try:
        r = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except FileNotFoundError:
        prog = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else str(cmd)
        return "", f"Command not found: {prog}", 1
    except subprocess.TimeoutExpired:
        return "", "Timed out", 1
    except Exception as e:
        return "", str(e), 1

def tool_available(name):
    return shutil.which(name) is not None

# ── HTTP Helpers (stdlib urllib — no shell, no curl) ──────────────────────────
DEFAULT_UA = "fivet-osint"

def http_get(url, headers=None, timeout=10):
    """GET a URL and return the body as text. Mirrors `curl -s`: on an HTTP
    error status the response body is still returned (many APIs return useful
    JSON on 4xx), rather than raising."""
    hdrs = {"User-Agent": DEFAULT_UA}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.read().decode("utf-8", "replace")
        except Exception:
            raise

def http_get_json(url, headers=None, timeout=10):
    return json.loads(http_get(url, headers=headers, timeout=timeout))

def log_err(source, exc):
    """Log a non-fatal live-fetch error (source + exception) and continue.
    Replaces the old bare `except: pass` blocks so failures aren't silent."""
    print(f"[FETCH-ERR] {source}: {exc}")

def http_post(url, data=None, headers=None, timeout=12):
    """POST to a URL and return the body as text. `data` may be a dict (form-
    urlencoded) or bytes/str. Mirrors `curl -s` error-body behavior."""
    hdrs = {"User-Agent": "Mozilla/5.0"}
    if headers:
        hdrs.update(headers)
    if isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode()
    elif isinstance(data, str):
        body = data.encode()
    else:
        body = data
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.read().decode("utf-8", "replace")
        except Exception:
            raise

# ── Name Parser Helper ────────────────────────────────────────────────────────
def parse_name_location(target):
    US_STATES = {
        'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN',
        'IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV',
        'NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN',
        'TX','UT','VT','VA','WA','WV','WI','WY','DC'
    }
    target = (target or "").strip()
    state = ""
    city = ""

    if "," in target:
        # Trust the comma form: "Name, City ST" (or "Name, City", or "Name, ST").
        name_part, _, rest = target.partition(",")
        name_part = name_part.strip()
        location_part = rest.strip()
        loc_tokens = location_part.split()
        if loc_tokens and loc_tokens[-1].upper() in US_STATES:
            state = loc_tokens[-1].upper()
            city = " ".join(loc_tokens[:-1]).strip()
        else:
            city = location_part
    else:
        tokens = target.split()
        # Only peel a trailing token when it is a real 2-letter state code.
        if len(tokens) >= 2 and tokens[-1].upper() in US_STATES:
            state = tokens[-1].upper()
            remainder = tokens[:-1]
            # A state code was present, so a further trailing token MAY be a
            # city — but only if doing so still leaves a first + last name.
            # This prevents "John Smith TX" from treating "Smith" as the city.
            if len(remainder) >= 3:
                city = remainder[-1]
                name_part = " ".join(remainder[:-1]).strip()
            else:
                name_part = " ".join(remainder).strip()
        else:
            # No trailing state code: keep the whole remainder as the name.
            name_part = target
        location_part = (city + " " + state).strip()

    name_words = name_part.split()
    first = name_words[0] if name_words else ""
    last = name_words[-1] if len(name_words) > 1 else ""
    return name_part, location_part, first, last, state, city


# ── Partial-DOB Helpers ───────────────────────────────────────────────────────
_MONTHS = {"january","february","march","april","may","june","july","august",
           "september","october","november","december",
           "jan","feb","mar","apr","jun","jul","aug","sep","sept","oct","nov","dec"}

def dob_is_full_date(dob):
    """True only for a complete numeric date (MM/DD/YYYY or similar) that is safe
    to hand to a site's dob= parameter. Partial forms (MM/YYYY, 'March 1987')
    return False so callers echo them verbatim instead of mangling them."""
    d = (dob or "").strip()
    if not d:
        return False
    parts = re.split(r"[/\-.]", d)
    if len(parts) == 3 and all(p.strip().isdigit() for p in parts):
        return True
    return False

def dob_born_hint(dob):
    """Return a 'born <as entered>' filter hint, echoing the DOB exactly as the
    investigator typed it (partial or full). Empty string when no DOB."""
    d = (dob or "").strip()
    return f"born {d}" if d else ""


# ── Person Resolver ───────────────────────────────────────────────────────────
def resolve_person(target, extra=None):
    """Normalize a person target into a bundle the person-modules build URLs and
    dorks from. When structured name parts are supplied in `extra`, the free-text
    name parser is bypassed for the name (location still comes from extra.city/
    state, falling back to the parsed location). Name-only searches keep today's
    behavior exactly.

    Returned dict keys:
      first, middle, paternal, maternal
      surnames        list of surnames to search under (BOTH when paternal AND
                      maternal are present — DBs index Hispanic subjects under
                      either — else the single known/parsed surname)
      first_last      list of (first, surname) pairs for {first}-{last} style URLs
      name_orderings  list of full-name strings; two orderings ("Pat Mat" and
                      "Mat Pat") when both surnames present, else one
      primary_name    name_orderings[0] (display / single-name fallback)
      last_primary    paternal when present (single last name where one is needed)
      state, city, location_part
      structured      True when structured name parts drove the result
    """
    extra = extra or {}
    first    = (extra.get("first") or "").strip()
    middle   = (extra.get("middle") or "").strip()
    paternal = (extra.get("paternal") or "").strip()
    maternal = (extra.get("maternal") or "").strip()

    p_name, p_location, p_first, p_last, p_state, p_city = parse_name_location(target)

    structured = bool(first or middle or paternal or maternal)

    if structured:
        # Structured parts take precedence; parser is bypassed for the name.
        base = " ".join(w for w in [first, middle] if w).strip()
        if paternal and maternal:
            surnames = [paternal, maternal]
            name_orderings = [
                " ".join(w for w in [base, paternal, maternal] if w).strip(),
                " ".join(w for w in [base, maternal, paternal] if w).strip(),
            ]
            last_primary = paternal
        elif paternal or maternal:
            single = paternal or maternal
            surnames = [single]
            name_orderings = [" ".join(w for w in [base, single] if w).strip()]
            last_primary = single
        else:
            surnames = []
            name_orderings = [base] if base else []
            last_primary = ""
        state = (extra.get("state") or p_state or "").strip()
        city  = (extra.get("city") or p_city or "").strip()
    else:
        # Legacy free-text path — single ordering, exactly as before.
        first = p_first
        surnames = [p_last] if p_last else []
        name_orderings = [p_name] if p_name else []
        last_primary = p_last
        state = (extra.get("state") or p_state or "").strip()
        city  = (extra.get("city") or p_city or "").strip()

    if not name_orderings:
        name_orderings = [p_name] if p_name else [target.strip()]

    first_last = [(first, s) for s in surnames] if (first and surnames) else []
    location_part = " ".join(w for w in [city, state] if w).strip()

    return {
        "first": first, "middle": middle,
        "paternal": paternal, "maternal": maternal,
        "surnames": surnames,
        "first_last": first_last,
        "name_orderings": name_orderings,
        "primary_name": name_orderings[0] if name_orderings else target.strip(),
        "last_primary": last_primary,
        "state": state, "city": city, "location_part": location_part,
        "structured": structured,
    }


# ══════════════════════════════════════════════════════════════════════════════
# CURATED DORK REFERENCE — TUNABLE KNOBS (PERSON target type)
# ──────────────────────────────────────────────────────────────────────────────
# LAYER 1: domain knowledge. To tune dork output over time, edit these lists —
# adding a site or a term is a one-line change here. The assembly logic in
# build_person_dorks() (LAYER 2) weaves the subject's entered fields into these
# patterns. Nothing here executes a search; output is query text for a human.
# ══════════════════════════════════════════════════════════════════════════════

# NM news/media domains for site: dorks (Crash & Incident + Social web-mentions).
# Court/record domains are intentionally NOT here — court records are handled
# separately through a paid database.
NM_NEWS_DOMAINS = [
    "abqjournal.com", "krqe.com", "koat.com", "kob.com",
    "santafenewmexican.com", "currentargus.com", "lcsun-news.com",
    "daily-times.com",
]

# Spanish-language life-event / obituary terms (Spanish category — toggle-gated).
SPANISH_TERMS = [
    "obituario", "funeraria", "servicios funerarios", "esquela", "en memoria",
    "quinceañera", "bautizo", "boda", "misa",
]

# Crash / incident vocabulary (Crash & Incident category).
CRASH_INCIDENT_TERMS = [
    "accident", "crash", "collision", "hit and run", "DWI", "DUI",
    "citation", "booking", "arrest",
]

# Generic employment vocabulary. The SPECIFIC employer comes from the employer
# form field, never a hard-coded company list.
EMPLOYMENT_TERMS = [
    "employer", "works", "works at", "employed at", "employed by",
]

# Address / residency vocabulary (Identity & Address category).
ADDRESS_TERMS = [
    "address", "lives in", "resident of", "current address", "domicile",
]

# URL-path fragments for inurl: precision dorks (Precision category).
URL_PATH_KEYWORDS = [
    "profile", "person", "record", "detail", "obituary", "obituaries",
    "memorial", "inmate", "offender", "booking", "arrest",
]

# Major social platforms for exact-name site: dorks (Social category).
SOCIAL_DOMAINS = [
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "linkedin.com", "tiktok.com", "reddit.com",
]

# Common consumer email providers (Social category email-provider dork).
EMAIL_PROVIDER_DORK = ('"@gmail.com" OR "@yahoo.com" OR "@hotmail.com" '
                       'OR "@outlook.com"')

# Loose (UNQUOTED) concept OR-blocks for Google dorks. Mode 6 rule: concepts
# like address / employment are expressed as loose single words, never as a
# stack of quoted phrases — quoting forces Google to require every phrase
# verbatim and the query returns nothing. Edit these to tune concept coverage.
ADDRESS_LOOSE = "address OR residence OR lives"
EMPLOYMENT_LOOSE = "employer OR works OR employed"

# ── BANNED SITES ────────────────────────────────────────────────────────────
# Sites that never yield usable subject data. Matched as a case-insensitive
# SUBSTRING against every generated URL, so a bare domain blocks all paths on
# it. Editable: add a domain here to ban it everywhere in one place. is_banned()
# is the single choke point every link list is filtered through.
BANNED_SITES = [
    # Mode 1 — low-yield SEO aggregators (generic ad-filled "records for NAME"
    # landing pages, not data).
    "countyoffice.org",
    "publicrecords.online",
    "propwire.com",          # name-only owner search returns a generic teaser
    # Mode 2 — paywall teases (blur the data, then charge). Listed defensively:
    # even if not currently emitted, they can never sneak back in.
    "spokeo.com",
    "beenverified.com",
    "intelius.com",
    "truthfinder.com",
    "peoplefinders.com",
    "instantcheckmate.com",
]


def dork_year(dob):
    """Extract a 4-digit year (19xx/20xx) from a full or partial DOB string,
    or '' if none is present. Used by precision co-occurrence dorks."""
    m = re.search(r"\b(19|20)\d{2}\b", dob or "")
    return m.group(0) if m else ""


def dork_url(dork):
    """Build the Google search URL for a dork string (human clicks it; nothing
    is executed here)."""
    encoded = dork.replace(" ", "+").replace('"', '%22').replace("'", "%27")
    return f"https://www.google.com/search?q={encoded}"


def phone_formats(digits):
    """Return the common written formats of a 10-digit US number for a single
    multi-format phone dork. Empty list if not exactly 10 digits."""
    d = re.sub(r"\D", "", digits or "")
    if len(d) != 10:
        return []
    a, p, l = d[:3], d[3:6], d[6:]
    return [d, f"({a}) {p}-{l}", f"{a}-{p}-{l}", f"{a}.{p}.{l}"]


def is_banned(url):
    """True if a URL points at a BANNED_SITES domain (Mode 1/2). Case-insensitive
    substring match, so any path on a banned domain is blocked. Every link list
    is filtered through this — a banned site can never reach output."""
    u = (url or "").lower()
    return any(b in u for b in BANNED_SITES)


def google_dork(name, phrase="", terms="", intext="", sites=None):
    """THE single Google-dork constructor. Every generated dork routes through
    here so the Mode 6 quoting rule cannot be violated dork-by-dork:

      • `name`   — the full name, the ONLY element hard-quoted automatically and
                   always as one phrase. State abbreviations and single common
                   words are NEVER auto-quoted; pass them via `terms` (loose).
      • `phrase` — at most ONE additional quoted phrase (scalar → stacking of
                   quoted phrases is structurally impossible).
      • `terms`  — loose, UNQUOTED words / OR-blocks / operators for concepts
                   (address, employer, voter) and for locations beyond the city.
      • `intext` — at most ONE intext: value (scalar → intext: cannot be stacked).
      • `sites`  — optional list of domains → "(site:a OR site:b)".

    Returns (dork_string, google_search_url). Executes nothing — text only."""
    parts = []
    if name:
        parts.append(f'"{name}"')
    if phrase:
        parts.append(f'"{phrase}"')
    if terms:
        parts.append(terms)
    if intext:
        parts.append(f'intext:"{intext}"' if " " in intext else f"intext:{intext}")
    if sites:
        parts.append("(" + " OR ".join(f"site:{d}" for d in sites) + ")")
    dork = " ".join(p for p in parts if p).strip()
    return dork, dork_url(dork)


def dedup_links(links, seen):
    """Filter a list of (label, url) pairs: drop BANNED_SITES and any URL already
    present in `seen` (report-wide dedup — across every category and both surname
    orderings). Mutates `seen`. Returns the surviving pairs in order."""
    out = []
    for label, url in links:
        if not url or is_banned(url) or url in seen:
            continue
        seen.add(url)
        out.append((label, url))
    return out


def emit_section(lines, title, links, seen, note="", rule="="):
    """Append a titled link section to `lines`, after filtering `links` through
    dedup_links(). If nothing survives, NOTHING is appended — no hollow header
    (per requirement (b)). Returns the number of links emitted."""
    kept = dedup_links(links, seen)
    if not kept:
        return 0
    bar = rule * 50
    lines.append(bar)
    lines.append(title)
    lines.append(bar)
    lines.append("")
    if note:
        lines.append(note)
        lines.append("")
    for label, url in kept:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")
    return len(kept)


# State abbreviation → full name (lower-hyphen), for sites whose location filter
# is a path segment using the spelled-out state (idcrawl). Editable.
STATE_NAMES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "DC": "district-of-columbia", "FL": "florida", "GA": "georgia",
    "HI": "hawaii", "ID": "idaho", "IL": "illinois", "IN": "indiana",
    "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
    "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan",
    "MN": "minnesota", "MS": "mississippi", "MO": "missouri", "MT": "montana",
    "NE": "nebraska", "NV": "nevada", "NH": "new-hampshire", "NJ": "new-jersey",
    "NM": "new-mexico", "NY": "new-york", "NC": "north-carolina",
    "ND": "north-dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon",
    "PA": "pennsylvania", "RI": "rhode-island", "SC": "south-carolina",
    "SD": "south-dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington",
    "WV": "west-virginia", "WI": "wisconsin", "WY": "wyoming",
}


def people_search_links(first, surname, full_name, city="", state=""):
    """Curated people-search links with each site's REAL location parameter baked
    in (Mode 3) — no name-only URLs for sites that support location filtering.
    Returns (primary, secondary): two lists of (label, url).

    PRIMARY  = free sources with confirmed real data (start here).
    SECONDARY = hit-or-miss free aggregators worth a look.

    BANNED_SITES are simply never listed here. All modules call this one function
    so location handling stays consistent across the whole report.

    ── LOCATION SCHEMES (⚑ = pattern I could not verify live per the no-scrape
    rule; flagged for you to confirm against real output; centralized here so a
    correction is a one-line edit):
      TruePeopleSearch  citystatezip=City, ST      (matches prior working code)
      FamilyTreeNow     &state=ST                  (param already in codebase)
      ZabaSearch        /state/ path               (param already in codebase)
      411.com           /ST path                   (param already in codebase)
      Nuwber            &city=&state=              (param already in codebase)
      FastPeopleSearch  _city-st path suffix       ⚑
      ThatsThem         /city-st path segment      ⚑
      IDCrawl           /<state-full-name> segment ⚑ (your named example)
      USPhoneBook       — location scheme unconfirmed → kept name-only ⚑
      SearchPeopleFree  — location scheme unconfirmed → kept name-only ⚑
      PeekYou           — location scheme unconfirmed → kept name-only ⚑
      SortedByName      — name index, no location support → name-only (expected)
    """
    st = (state or "").strip()
    st_l = st.lower()
    city_h = city.strip().replace(" ", "-").lower()
    fn_plus = full_name.replace(" ", "+")
    fn_url = full_name.replace(" ", "-").lower()
    first_url = first.strip().replace(" ", "-").lower()
    sur_url = surname.strip().replace(" ", "-").lower()
    csz = ", ".join(w for w in [city.strip(), st] if w)          # "Silver City, NM"
    csz_plus = csz.replace(" ", "+")

    # PRIMARY — confirmed reliable free sources, each carrying location.
    tps = f"https://www.truepeoplesearch.com/results?name={fn_plus}"
    if csz:
        tps += f"&citystatezip={csz_plus}"
    fps = f"https://www.fastpeoplesearch.com/name/{first_url}-{sur_url}"
    if city_h and st_l:
        fps += f"_{city_h}-{st_l}"          # ⚑ /name/first-last_city-st
    tt = f"https://thatsthem.com/name/{first_url}-{sur_url}"
    if city_h and st_l:
        tt += f"/{city_h}-{st_l}"           # ⚑ /name/first-last/city-st
    ftn = f"https://www.familytreenow.com/search/people/results?first={first}&last={surname}"
    if st:
        ftn += f"&state={st}"

    primary = [
        ("TruePeopleSearch ★ confirmed reliable", tps),
        ("FastPeopleSearch",                       fps),
        ("ThatsThem ★ 100% free",                  tt),
        ("FamilyTreeNow ★ best free",              ftn),
    ]

    # SECONDARY — hit-or-miss free aggregators. Kept broadly (drop only obvious
    # junk); location applied where the site's scheme is known.
    idc = f"https://www.idcrawl.com/name/{first_url}-{sur_url}"
    if st and STATE_NAMES.get(st.upper()):
        idc += f"/{STATE_NAMES[st.upper()]}"     # ⚑ /name/first-last/new-mexico
    zaba = f"https://www.zabasearch.com/people/{first_url}+{sur_url}/{st}/" if st \
        else f"https://www.zabasearch.com/people/{first_url}+{sur_url}/"
    foureleven = f"https://www.411.com/name/{first_url}-{sur_url}/{st}" if st \
        else f"https://www.411.com/name/{first_url}-{sur_url}"
    nuwber = f"https://nuwber.com/search?firstName={first}&lastName={surname}"
    if city.strip():
        nuwber += f"&city={city.strip().replace(' ', '+')}"
    if st:
        nuwber += f"&state={st}"
    addresses = f"https://www.addresses.com/people/{first_url}+{sur_url}/{st}/" if st \
        else f"https://www.addresses.com/people/{first_url}+{sur_url}/"

    secondary = [
        ("IDCrawl (social+records)",   idc),
        ("ZabaSearch (aliases+hist)",  zaba),
        ("411.com",                    foureleven),
        ("Nuwber",                     nuwber),
        ("Addresses.com",              addresses),
        ("USPhoneBook",                f"https://www.usphonebook.com/{first_url}-{sur_url}"),
        ("SearchPeopleFree",           f"https://www.searchpeoplefree.com/find/{first_url}-{sur_url}"),
        ("PeekYou (social+arrests)",   f"https://www.peekyou.com/{first_url}_{sur_url}"),
        ("SortedByName",               f"https://www.sortedbyname.com/search?q={fn_plus}"),
    ]
    return primary, secondary


def build_person_dorks(p, extra=None, dob="", seen=None):
    """LAYER 2 — assembly. Weaves the subject's entered fields into the curated
    patterns above, grouped under category headers. Name-based categories emit
    BOTH surname orderings when both surnames are present. Returns a list of
    output lines (dork + its Google search URL) to extend a module's `lines`,
    preserving the joined-text return format. Executes nothing — text only.

    `seen` is an optional report-wide set of already-emitted URLs; pass the
    module's set so dork URLs are de-duplicated against the rest of the report."""
    ex = extra or {}
    orderings = [o for o in (p.get("name_orderings") or [p.get("primary_name", "")]) if o]
    surnames = [s for s in p.get("surnames", []) if s]
    state = p.get("state", "")
    city = p.get("city", "")
    location_part = p.get("location_part", "")
    loc = location_part or state or "New Mexico"
    phone_digits = re.sub(r"\D", "", (ex.get("phone") or ""))[-10:]
    email = (ex.get("email") or "").strip()
    employer = (ex.get("employer") or "").strip()
    username = (ex.get("username") or "").strip()
    year = dork_year(dob)
    spanish = bool(ex.get("spanish"))

    primary_name = p.get("primary_name", "")
    if seen is None:
        seen = set()
    out = []

    # ── assembly helpers ─────────────────────────────────────────────────────
    # Every dork is a (dork_string, url) tuple from google_dork(); emission
    # dedups on the URL (report-wide) and drops a category whose dorks all
    # vanished — no hollow headers.
    def _dedup(raw):
        items = []
        for it in raw:
            if not it:
                continue
            d, u = it
            if not d or u in seen:
                continue
            seen.add(u)
            items.append((d, u))
        return items

    def build_blocks(fn, targets):
        multi = len(targets) > 1
        blocks = []
        for nf in targets:
            items = _dedup(fn(nf))
            if items:
                blocks.append((nf if multi else None, items))
        return blocks

    def flush(title, blocks, note="", extra=None):
        extra_items = _dedup(extra or [])
        if not blocks and not extra_items:
            return
        out.append("-" * 50)
        out.append(title)
        out.append("-" * 50)
        if note:
            out.append(note)
        out.append("")
        for sub, items in blocks:
            if sub is not None:
                out.append(f"── {sub} ──")
                out.append("")
            for d, u in items:
                out.append(f"  {d}")
                out.append(f"  {u}")
                out.append("")
        for d, u in extra_items:
            out.append(f"  {d}")
            out.append(f"  {u}")
            out.append("")

    def dork_place(name, concept=""):
        # name + optional loose concept OR-block + location. Mode 6: the city
        # (when present) is the single quoted phrase; the state is ALWAYS loose
        # (never quoted); concepts stay loose. Falls back to loose "New Mexico"
        # only when no location was entered at all.
        terms = f"({concept})" if concept else ""
        if state:
            terms = (terms + " " + state).strip()
        if city:
            return google_dork(name, phrase=city, terms=terms)
        if not state:
            terms = (terms + " New Mexico").strip()
        return google_dork(name, terms=terms)

    def loose_place(base=""):
        # loose (unquoted) location tokens, for dorks whose one quoted-phrase
        # slot is already spent (employer / phone / intext).
        bits = [base] if base else []
        if city:
            bits.append(city)
        if state:
            bits.append(state)
        return " ".join(bits).strip()

    # ── Identity & Address ──────────────────────────────────────────────────
    def _identity(nf):
        return [dork_place(nf), dork_place(nf, ADDRESS_LOOSE)]
    flush("IDENTITY & ADDRESS", build_blocks(_identity, orderings))

    # ── Phone (multi-format) ────────────────────────────────────────────────
    fmts = phone_formats(phone_digits)
    if fmts:
        pretty = fmts[1] if len(fmts) > 1 else fmts[0]      # "(575) 628-8535"
        # Exact-match OR alternatives of ONE number — broadening, not the Mode 6
        # over-constraint (that rule targets AND-ed quoted phrases).
        phone_or = " OR ".join(f'"{f}"' for f in fmts)
        def _phone_name(nf):
            return [google_dork(nf, phrase=pretty)]         # name + the number
        phone_extra = [
            google_dork("", terms=phone_or),                # the number, all formats
            google_dork("", phrase=pretty, terms=f"({ADDRESS_LOOSE})"),
        ]
        flush("PHONE (MULTI-FORMAT)",
              build_blocks(_phone_name, orderings), extra=phone_extra)

    # ── Employment ──────────────────────────────────────────────────────────
    def _employment(nf):
        ds = []
        if employer:                       # specific employer from the form field
            ds.append(google_dork(nf, phrase=employer, terms=state))
        ds.append(dork_place(nf, EMPLOYMENT_LOOSE))
        return ds
    flush("EMPLOYMENT", build_blocks(_employment, orderings))

    # ── Crash & Incident (incl. NM news/media coverage) ─────────────────────
    crash_or = " OR ".join(CRASH_INCIDENT_TERMS)             # loose concepts
    def _crash(nf):
        return [dork_place(nf, crash_or),
                google_dork(nf, sites=NM_NEWS_DOMAINS)]      # NM news/media
    flush("CRASH & INCIDENT", build_blocks(_crash, orderings))

    # ── Social ──────────────────────────────────────────────────────────────
    def _social(nf):
        return [google_dork(nf, sites=SOCIAL_DOMAINS),
                google_dork(nf, terms=EMAIL_PROVIDER_DORK)]
    social_extra = []
    if username:                                   # exact handle when provided
        social_extra.append(google_dork(username))
        social_extra.append(google_dork(username, sites=SOCIAL_DOMAINS))
    if email:
        social_extra.append(google_dork(email))
    flush("SOCIAL", build_blocks(_social, orderings), extra=social_extra)

    # ── Spanish-Language / Family (TOGGLE-GATED) ────────────────────────────
    # Emitted ONLY when the Spanish toggle is on — never inferred from the name.
    if spanish:
        span_or = " OR ".join(SPANISH_TERMS)                # loose concepts
        span_targets = [s for s in (surnames or [primary_name]) if s]
        def _span(sn):
            return [dork_place(sn, span_or)]
        flush("SPANISH-LANGUAGE / FAMILY",
              build_blocks(_span, span_targets),
              note="Shown because the Spanish-language toggle is on. "
                   "Targets obituaries, funeral notices, and family life events.")

    # ── Precision / Narrowing (intext: / inurl:) ────────────────────────────
    # intext: co-occurrence dorks — built ONLY when the identifier is on file
    # (DOB-year, phone), plus a single-intext address co-occurrence when a state
    # is known. Mode 6: at most ONE intext: per dork; state never quoted.
    def _precision(nf):
        ds = []
        if year:
            ds.append(google_dork(nf, intext=year, terms=loose_place()))
        if len(phone_digits) == 10:
            ph = phone_formats(phone_digits)[1]
            ds.append(google_dork(nf, intext=ph, terms=state))
        if state:
            ds.append(google_dork(nf, intext="address", terms=state))
        # inurl: fallbacks (operators, not intext) — slices of URL_PATH_KEYWORDS.
        ds.append(google_dork(nf, terms=f"({id_path})"))
        ds.append(google_dork(nf, terms=f"({obit_path})"))
        return ds
    id_path     = " OR ".join(f"inurl:{k}" for k in URL_PATH_KEYWORDS[0:4])   # profile/person/record/detail
    obit_path   = " OR ".join(f"inurl:{k}" for k in URL_PATH_KEYWORDS[4:7])   # obituary/obituaries/memorial
    arrest_path = " OR ".join(f"inurl:{k}" for k in URL_PATH_KEYWORDS[7:11])  # inmate/offender/booking/arrest
    arrest_extra = [google_dork(sn, terms=f"({arrest_path})")
                    for sn in (surnames or [primary_name]) if sn]
    flush("PRECISION / NARROWING",
          build_blocks(_precision, orderings),
          note="Escalate here when broad dorks return too much noise. intext:/"
               "inurl: narrow hard and can over-filter — drop an operator if you "
               "get too few results.",
          extra=arrest_extra)

    return out


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: PEOPLE SEARCH
# All sources verified free June 2026
# ══════════════════════════════════════════════════════════════════════════════

def module_people_search(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "people"})
    p = resolve_person(target, extra)
    name_part = p["primary_name"]
    location_part = p["location_part"]
    state = p["state"]
    city = p["city"]
    first = p["first"]
    orderings = p["name_orderings"]
    surnames = p["surnames"]

    name_plus = name_part.replace(" ", "+")
    loc_plus = location_part.replace(" ", "+")
    dob_full = dob_is_full_date(dob)

    # Pair each full-name ordering with a distinct surname so both surname
    # orderings AND both surnames (paternal + maternal) get searched — databases
    # index Hispanic subjects under either surname, so searching only the paternal
    # misses records filed under the maternal. Single-surname / legacy free-text
    # names produce exactly one variant (today's behavior).
    variants = []
    for i, nf in enumerate(orderings):
        sn = surnames[i] if i < len(surnames) else ""
        variants.append((nf, sn))
    if not variants:
        variants = [(name_part, "")]

    seen = set()   # report-wide URL dedup across every section below

    lines = []
    lines.append(f"TARGET:   {name_part}")
    if location_part:
        lines.append(f"LOCATION: {location_part}")
    if dob:
        lines.append(f"DOB:      {dob}")
    if ssn:
        masked = ssn[:3] + "-**-****" if len(ssn) >= 9 else "***-**-****"
        lines.append(f"SSN:      {masked}")
    if oln:
        lines.append(f"OLN:      {oln}")
    lines.append("")

    # People-finder sites, curated and grouped so reliable sources come first.
    # Location is baked into every URL by people_search_links() (Mode 3);
    # BANNED_SITES are filtered and URLs de-duplicated report-wide.
    def render_group(title, group_index, note=""):
        blocks = []
        for nf, surname in variants:
            links = people_search_links(first, surname, nf, city, state)[group_index]
            kept = dedup_links(links, seen)
            if kept:
                blocks.append((nf, surname, kept))
        if not blocks:
            return
        lines.append("=" * 50)
        lines.append(title)
        lines.append("=" * 50)
        lines.append("")
        if note:
            lines.append(note)
            lines.append("")
        for nf, surname, kept in blocks:
            if len(variants) > 1:
                tag = f"  (surname: {surname})" if surname else ""
                lines.append(f"── {nf}{tag} ──")
                lines.append("")
            for name, url in kept:
                lines.append(f"[{name}]")
                lines.append(f"  {url}")
                lines.append("")

    render_group("PRIMARY SOURCES (start here) — FREE, REAL DATA", 0)
    render_group("SECONDARY / HIT-OR-MISS — FREE AGGREGATORS", 1)

    social = [
        ("LinkedIn",  f"https://www.linkedin.com/search/results/people/?keywords={name_plus}"),
        ("Facebook",  f"https://www.facebook.com/search/people/?q={name_plus}"),
        ("Twitter/X", f"https://twitter.com/search?q=%22{name_plus}%22&f=user"),
        ("Instagram", f"https://www.instagram.com/explore/search/keyword/?q={name_plus}"),
        ("TikTok",    f"https://www.tiktok.com/search?q={name_plus}"),
        ("YouTube",   f"https://www.youtube.com/results?search_query={name_plus}"),
        ("Reddit",    f"https://www.reddit.com/search/?q=%22{name_part}%22&type=user"),
    ]
    emit_section(lines, "SOCIAL MEDIA", social, seen)

    courts = [
        ("NM Courts (CourtLook)",   "https://caselookup.nmcourts.gov/caselookup/app"),
        ("PACER Federal Courts",    "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("CourtListener (Free)",    f"https://www.courtlistener.com/?q={name_plus}&type=p"),
        ("OpenSanctions Watchlist", f"https://www.opensanctions.org/search/?q={name_plus}"),
        ("VINE Offender Search NM", "https://vinelink.vineapps.com/search/NM/Person"),
    ]
    emit_section(lines, "COURT & PUBLIC RECORDS", courts, seen)

    # GOOGLE DORKS — all routed through google_dork() so the Mode 6 quoting rule
    # holds (name quoted once, state never quoted, no stacked intext:). Emitted
    # per name ordering; de-duplicated and header-suppressed when empty.
    def dork_set(nf):
        loc = google_dork(nf, phrase=city, terms=state) if city \
            else google_dork(nf, terms=(state or "New Mexico"))
        return [
            loc,
            google_dork(nf, terms="address phone"),
            google_dork(nf, sites=["familytreenow.com"]),
            google_dork(nf, sites=["truepeoplesearch.com"]),
            google_dork(nf, sites=["linkedin.com"]),
            google_dork(nf, terms="arrest OR mugshot"),
            google_dork(nf, terms="court OR lawsuit OR case"),
            google_dork(nf, terms="obituary"),
            google_dork(nf, terms="email OR contact"),
        ]
    dork_blocks = []
    for nf in orderings:
        kept = [(d, u) for d, u in dork_set(nf) if u not in seen and not seen.add(u)]
        if kept:
            dork_blocks.append((nf, kept))
    if dork_blocks:
        lines.append("=" * 50)
        lines.append("GOOGLE DORKS")
        lines.append("=" * 50)
        lines.append("")
        for nf, kept in dork_blocks:
            if len(orderings) > 1:
                lines.append(f"── DORKS FOR: {nf} ──")
                lines.append("")
            for dork, url in kept:
                lines.append(f"  {dork}")
                lines.append(f"  {url}")
                lines.append("")

    try:
        san_url = ("https://api.opensanctions.org/search/default?q="
                   + urllib.parse.quote_plus(name_part) + "&schema=Person")
        san_data = http_get_json(san_url, timeout=10)
        results = san_data.get("results", [])
        lines.append("=" * 50)
        lines.append("LIVE SANCTIONS / WATCHLIST CHECK")
        lines.append("=" * 50)
        lines.append("")
        if results:
            lines.append(f"⚠ WARNING: {len(results)} MATCH(ES) FOUND")
            for r in results[:5]:
                lines.append(f"  • {r.get('caption','?')} — Score: {r.get('score','?')}")
        else:
            lines.append("✓ No matches found on sanctions/watchlists")
        lines.append("")
    except Exception as e:
        log_err("OpenSanctions live check", e)

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "people", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: PUBLIC RECORDS
# ══════════════════════════════════════════════════════════════════════════════

def module_public_records(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "public_records"})
    p = resolve_person(target, extra)
    name_part = p["primary_name"]
    first = p["first"]
    last = p["last_primary"]
    state = p["state"]
    city = p["city"]
    orderings = p["name_orderings"]
    surnames = p["surnames"]
    name_plus = name_part.replace(" ", "+")

    # Variant pairs (full-name ordering + surname) so the people-finder block
    # searches both surname orderings and under both surnames when present.
    variants = []
    for i, nf in enumerate(orderings):
        sn = surnames[i] if i < len(surnames) else ""
        variants.append((nf, sn))
    if not variants:
        variants = [(name_part, last)]

    seen = set()   # report-wide URL dedup across every section below

    lines = []
    lines.append(f"TARGET: {name_part}")
    if dob:
        lines.append(f"DOB:    {dob}")
    if ssn:
        masked = ssn[:3] + "-**-****" if len(ssn) >= 9 else "***-**-****"
        lines.append(f"SSN:    {masked}")
    if oln:
        lines.append(f"OLN:    {oln}")
    lines.append("")

    # Curated people-finder sites, reliable-first, location baked in (Mode 3),
    # BANNED_SITES filtered, URLs de-duplicated report-wide. JudyRecords is a
    # court-case index (not a people aggregator) and lives in the criminal block.
    def render_group(title, group_index):
        blocks = []
        for nf, surname in variants:
            links = people_search_links(first, surname, nf, city, state)[group_index]
            kept = dedup_links(links, seen)
            if kept:
                blocks.append((nf, surname, kept))
        if not blocks:
            return
        lines.append("=" * 50)
        lines.append(title)
        lines.append("=" * 50)
        lines.append("")
        for nf, surname, kept in blocks:
            if len(variants) > 1:
                tag = f"  (surname: {surname})" if surname else ""
                lines.append(f"── {nf}{tag} ──")
                lines.append("")
            for name, url in kept:
                lines.append(f"[{name}]")
                lines.append(f"  {url}")
                lines.append("")

    render_group("PRIMARY SOURCES (start here) — FREE PEOPLE & ADDRESS", 0)
    render_group("SECONDARY / HIT-OR-MISS — FREE AGGREGATORS", 1)

    criminal = [
        ("JudyRecords ★ 740M US Cases FREE",   f"https://www.judyrecords.com/search?q={name_plus}"),
        ("Trellis.law (State Courts Free)",     f"https://trellis.law/person/{first}-{last}"),
        ("NM Courts (CourtLook)",               "https://caselookup.nmcourts.gov/caselookup/app"),
        ("PACER Federal Courts",                "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("CourtListener (Free Federal)",        f"https://www.courtlistener.com/?q={name_plus}&type=p"),
        ("VINE Offender Search NM",             "https://vinelink.vineapps.com/search/NM/Person"),
        ("NM Corrections Inmate",               "https://www.cd.nm.gov/divisions/oid/offender-search/"),
        ("JailBase (Arrest Bookings) ★ FREE",   f"https://www.jailbase.com/search/?name_searched={name_plus}"),
        ("ArrestFacts",                         f"https://arrestfacts.com/search?name={name_plus}"),
        ("BustedMugshots",                      f"https://bustedmugshots.com/search?name={name_plus}"),
        ("MugshotSearch",                       f"https://www.mugshots.com/search?q={name_plus}"),
        ("OpenSanctions Watchlist",             f"https://www.opensanctions.org/search/?q={name_plus}"),
        ("Sex Offender Registry NM",            "https://www.nmsexoffender.dps.nm.gov/"),
        ("Sex Offender Registry National",      f"https://www.nsopw.gov/Search/Results?firstName={first}&lastName={last}"),
    ]
    emit_section(lines, "ARREST & CRIMINAL RECORDS", criminal, seen)

    vital = [
        ("FamilySearch (Free)",      f"https://www.familysearch.org/search/record/results?q.givenName={first}&q.surname={last}"),
        ("Ancestry (limited free)",  f"https://www.ancestry.com/search/?name={first}_{last}"),
        ("FindAGrave",               f"https://www.findagrave.com/memorial/search?firstname={first}&lastname={last}"),
        ("BillionGraves",            f"https://billiongraves.com/search/results/#firstname={first}&lastname={last}"),
        ("Legacy.com Obituaries",    f"https://www.legacy.com/obituaries/search?keyword={name_plus}"),
        ("NamUs Missing Persons",    "https://www.namus.gov/MissingPersons/Search#/results"),
    ]
    emit_section(lines, "VITAL RECORDS & GENEALOGY", vital, seen)

    licenses = [
        ("NM License Lookup",        "https://www.rld.nm.gov/licensing-and-regulation/"),
        ("NM Medical Board",         "https://www.nmmb.state.nm.us/"),
        ("NM Bar Association",       "https://www.nmbar.org/"),
        ("NPPES (Medical NPI)",      f"https://npiregistry.cms.hhs.gov/search?search_type=ind&first_name={first}&last_name={last}"),
        ("BLS License Lookup",       "https://www.careeronestop.org/Toolkit/Credentials/find-licenses.aspx"),
    ]
    emit_section(lines, "PROFESSIONAL LICENSES", licenses, seen)

    try:
        cl_url = ("https://www.courtlistener.com/api/rest/v3/people/?name_last="
                  + urllib.parse.quote_plus(last) + "&name_first="
                  + urllib.parse.quote_plus(first) + "&format=json")
        data = http_get_json(cl_url, timeout=10)
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
    except Exception as e:
        log_err("CourtListener live check", e)

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "public_records", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: PROPERTY RECORDS
# ══════════════════════════════════════════════════════════════════════════════

def module_property(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "property"})
    p = resolve_person(target, extra)
    name_part = p["primary_name"]
    location_part = p["location_part"]
    state = p["state"]
    first = p["first"]
    last = p["last_primary"]
    orderings = p["name_orderings"]
    surnames = p["surnames"]
    name_plus = name_part.replace(" ", "+")

    variants = []
    for i, nf in enumerate(orderings):
        sn = surnames[i] if i < len(surnames) else ""
        variants.append((nf, sn))
    if not variants:
        variants = [(name_part, last)]

    seen = set()   # report-wide URL dedup across every section below

    lines = []
    lines.append(f"TARGET: {name_part}")
    if location_part:
        lines.append(f"LOCATION: {location_part}")
    lines.append("")

    nm_counties = [
        ("NETR — ALL 33 NM COUNTIES ★ START HERE",    "https://publicrecords.netronline.com/state/NM"),
        ("Bernalillo County — Owner Name Search",      "https://assessor.bernco.gov/public.access/search/commonsearch.aspx?mode=owner"),
        ("Santa Fe County — Parcel Map Search",        "https://assessor.santafecountynm.gov/map.php"),
        ("Dona Ana County — Property Search",          "https://assessor.donaanacounty.org/"),
        ("Sandoval County — Property Search",          "https://www.sandovalcountynm.gov/assessor/property-search/"),
        ("Taos County — Property Search",              "https://www.taoscounty.org/assessor"),
        ("San Juan County — Property Search",          "https://www.sjcounty.net/departments/assessor"),
        ("Bernalillo County Clerk — Deeds/Liens",      "https://www.berncoclerk.gov/recording-and-filing/public-records-search/"),
    ]
    emit_section(lines, "NEW MEXICO PROPERTY RECORDS", nm_counties, seen,
                 note="NOTE: Use NETR to find the correct direct search link for any "
                      "NM county.\nMost county assessors require clicking through a "
                      "disclaimer before searching.")

    # National owner-name search (both surname orderings). PropWire and County
    # Office are BANNED_SITES (Mode 1 — ad-filled owner-teaser pages) and are cut;
    # what remains is FamilyTreeNow address history plus the NETR/Zillow routers.
    ftn_state = f"&state={state}" if state else ""
    nat_blocks = []
    for nf, surname in variants:
        owner = [
            ("FamilyTreeNow — Address Hist",
             f"https://www.familytreenow.com/search/people/results?first={first}&last={surname}{ftn_state}"),
        ]
        kept = dedup_links(owner, seen)
        if kept:
            nat_blocks.append((nf, surname, kept))
    static_national = dedup_links([
        ("NETR — All 50 States Router",   "https://publicrecords.netronline.com/"),
        ("Zillow — Ownership Check",      f"https://www.zillow.com/homes/{location_part.replace(' ','-') or 'new-mexico'}_rb/"),
    ], seen)
    if nat_blocks or static_national:
        lines.append("=" * 50)
        lines.append("NATIONAL PROPERTY DATABASES — FREE")
        lines.append("=" * 50)
        lines.append("")
        for nf, surname, kept in nat_blocks:
            if len(variants) > 1:
                tag = f"  (surname: {surname})" if surname else ""
                lines.append(f"── OWNER SEARCH VARIANT: {nf}{tag} ──")
                lines.append("")
            for name, url in kept:
                lines.append(f"[{name}]")
                lines.append(f"  {url}")
                lines.append("")
        for name, url in static_national:
            lines.append(f"[{name}]")
            lines.append(f"  {url}")
            lines.append("")

    tax = [
        ("NM Taxation & Revenue",     "https://tap.state.nm.us/tap/_/"),
        ("Federal Tax Liens (PACER)", "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("UCC Filings NM",            "https://portal.sos.state.nm.us/BFS/online/UCCFilings/SearchUCC"),
        ("Bankruptcy Search",         "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
    ]
    emit_section(lines, "TAX & LIENS — FREE", tax, seen)

    # Property dorks routed through google_dork() (Mode 6 quoting rule).
    def prop_dorks(nf):
        return [
            google_dork(nf, terms="property owner New Mexico"),
            google_dork(nf, terms="real estate deed"),
            google_dork(nf, terms="assessor parcel"),
            google_dork(nf, terms="foreclosure lien"),
        ]
    dork_blocks = []
    for nf in orderings:
        kept = [(d, u) for d, u in prop_dorks(nf) if u not in seen and not seen.add(u)]
        if kept:
            dork_blocks.append((nf, kept))
    if dork_blocks:
        lines.append("=" * 50)
        lines.append("GOOGLE DORKS FOR PROPERTY")
        lines.append("=" * 50)
        lines.append("")
        for nf, kept in dork_blocks:
            if len(orderings) > 1:
                lines.append(f"── DORKS FOR: {nf} ──")
                lines.append("")
            for dork, url in kept:
                lines.append(f"  {dork}")
                lines.append(f"  {url}")
                lines.append("")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "property", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: SKIP TRACE
# All sources verified free June 2026 — paywall tools removed
# ══════════════════════════════════════════════════════════════════════════════

def module_skip_trace(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "skip_trace"})
    p = resolve_person(target, extra)
    name_part = p["primary_name"]
    location_part = p["location_part"]
    first = p["first"]
    last = p["last_primary"]
    state = p["state"]
    city = p["city"]
    orderings = p["name_orderings"]
    surnames = p["surnames"]
    name_plus = name_part.replace(" ", "+")
    name_url = name_part.replace(" ", "-").lower()
    city_plus = city.replace(" ", "+")

    ex = extra or {}
    phone_raw = (ex.get("phone") or "").strip()
    phone_clean = re.sub(r"\D", "", phone_raw)[-10:] if phone_raw else ""
    email = (ex.get("email") or "").strip()
    employer = (ex.get("employer") or "").strip()
    employer_plus = employer.replace(" ", "+")

    # Full-name ordering + surname pairs so people-search queries run under both
    # surname orderings and both surnames when paternal + maternal are present.
    variants = []
    for i, nf in enumerate(orderings):
        sn = surnames[i] if i < len(surnames) else ""
        variants.append((nf, sn))
    if not variants:
        variants = [(name_part, last)]

    seen = set()   # report-wide URL dedup across every section below

    lines = []
    lines.append(f"TARGET:   {name_part}")
    if location_part:
        lines.append(f"LOCATION: {location_part}")
    if dob:
        lines.append(f"DOB:      {dob}")
    if ssn:
        masked = ssn[:3] + "-**-****" if len(ssn) >= 9 else "***-**-****"
        lines.append(f"SSN:      {masked}")
    if oln:
        lines.append(f"OLN:      {oln}")
    lines.append("")
    lines.append("⚠ DPPA: Law firms qualify under 18 U.S.C. § 2721(b) for litigation & process serving.")
    lines.append("")

    if dob:
        lines.append(f"★ DOB PROVIDED: {dob} — use to disambiguate same-name results.")
        lines.append("  Filter results to subjects matching this date of birth.")
        lines.append("")
    if oln:
        lines.append(f"★ OLN PROVIDED: {oln} — run through NM MVD and state DMV records.")
        lines.append("  The OLN / driver's license number is often found in the court")
        lines.append("  system (traffic citations, criminal complaints, case dockets).")
        lines.append("")

    # Curated people-finder sites, reliable-first, location baked in (Mode 3),
    # BANNED_SITES filtered, URLs de-duplicated report-wide.
    def render_group(title, group_index, note=""):
        blocks = []
        for nf, surname in variants:
            links = people_search_links(first, surname, nf, city, state)[group_index]
            kept = dedup_links(links, seen)
            if kept:
                blocks.append((nf, surname, kept))
        if not blocks:
            return
        lines.append("=" * 50)
        lines.append(title)
        lines.append("=" * 50)
        lines.append("")
        if note:
            lines.append(note)
            lines.append("")
        for nf, surname, kept in blocks:
            if len(variants) > 1:
                tag = f"  (surname: {surname})" if surname else ""
                lines.append(f"── {nf}{tag} ──")
                lines.append("")
            for name, url in kept:
                lines.append(f"[{name}]")
                lines.append(f"  {url}")
                lines.append("")

    render_group("PRIMARY SOURCES (start here) — FREE, FULL RESULTS", 0,
                 note="Each result page also lists the subject's relatives & "
                      "associates — use them to locate the subject indirectly.")
    render_group("SECONDARY / HIT-OR-MISS — FREE AGGREGATORS", 1)

    lines.append("=" * 50)
    lines.append("VOTER REGISTRATION (GOVERNMENT-VERIFIED ADDRESS)")
    lines.append("=" * 50)
    lines.append("")
    lines.append("Voter registration = most reliable free address source. The state")
    lines.append("lookups are interactive portals — see MANUAL LOOKUPS below.")
    lines.append("")
    if dob:
        lines.append(f"★ DOB {dob} — enter in the state voter portal to confirm identity.")
        lines.append("")
    lines.append("TIP: FEC political donations (see Tier 5) also give name + address + employer.")
    lines.append("")
    # Pre-fillable Google search for voter-registration web mentions (Mode 6:
    # name quoted, one extra quoted phrase, location loose, state never quoted).
    gv_dork, gv_url = google_dork(name_part, phrase="voter registration",
                                  terms=" ".join(w for w in [city, state] if w))
    if gv_url not in seen:
        seen.add(gv_url)
        lines.append("[Google — Voter Registration Mentions]")
        lines.append(f"  {gv_url}")
        lines.append("")

    # State-specific voter portals — all 50 states + DC
    voter_portals = {
        "AL": ("AL Voter Status",        "https://myinfo.alabamavotes.gov/VoterView/RegistrantSearch.do"),
        "AK": ("AK Voter Search",        "https://myvoterinformation.alaska.gov/"),
        "AZ": ("AZ Voter Registration",  "https://my.arizona.vote/VoterView/RegistrantSearch.do"),
        "AR": ("AR Voter Status",        "https://www.voterview.ar-nova.org/VoterView/RegistrantSearch.do"),
        "CA": ("CA Voter Status",        "https://voterstatus.sos.ca.gov/"),
        "CO": ("CO Voter Portal",        "https://www.sos.state.co.us/voter/pages/pub/olvr/findVoterReg.xhtml"),
        "CT": ("CT Voter Lookup",        "https://portaldir.ct.gov/sots/LookUpRegistration.aspx"),
        "DE": ("DE Voter Search",        "https://ivote.vote.org/voterinfo"),
        "DC": ("DC Voter Search",        "https://www.dcboe.org/Voters/Register-To-Vote/Check-Voter-Registration-Status"),
        "FL": ("FL Voter Lookup",        "https://registration.elections.myflorida.com/CheckVoterStatus"),
        "GA": ("GA Voter Status",        "https://mvp.sos.ga.gov/s/"),
        "HI": ("HI Voter Search",        "https://olvr.hawaii.gov/"),
        "ID": ("ID Voter Lookup",        "https://elections.sos.idaho.gov/ElectionLink/RobotsHome.aspx"),
        "IL": ("IL Voter Lookup",        "https://www.elections.il.gov/votinginformation/RegistrationLookup.aspx"),
        "IN": ("IN Voter Search",        "https://indianavoters.in.gov/"),
        "IA": ("IA Voter Registration",  "https://sos.iowa.gov/elections/voterreg/reglookup.aspx"),
        "KS": ("KS Voter Status",        "https://myvoteinfo.voteks.org/VoterView/RegistrantSearch.do"),
        "KY": ("KY Voter Search",        "https://vrsws.sos.ky.gov/VIC/"),
        "LA": ("LA Voter Search",        "https://voterportal.sos.la.gov/"),
        "ME": ("ME Voter Lookup",        "https://www.maine.gov/sos/cec/elec/voter-info/voterregcheck.html"),
        "MD": ("MD Voter Search",        "https://voterservices.elections.maryland.gov/VoterSearch"),
        "MA": ("MA Voter Lookup",        "https://www.sec.state.ma.us/ovr/"),
        "MI": ("MI Voter Info",          "https://mvic.sos.state.mi.us/"),
        "MN": ("MN Voter Status",        "https://mnvotes.sos.state.mn.us/VoterStatus.aspx"),
        "MS": ("MS Voter Lookup",        "https://www.sos.ms.gov/elections-voting/voter-registration-information"),
        "MO": ("MO Voter Search",        "https://voteroutreach.sos.mo.gov/VoterSearch/Search"),
        "MT": ("MT Voter Lookup",        "https://app.mt.gov/voterinfo/"),
        "NE": ("NE Voter Status",        "https://www.votercheck.necvr.ne.gov/"),
        "NV": ("NV Voter Status",        "https://www.nvsos.gov/voters/register-to-vote"),
        "NH": ("NH Voter Search",        "https://app.sos.nh.gov/Public/AbsenteeBallot.aspx"),
        "NJ": ("NJ Voter Status",        "https://voter.svrs.nj.gov/registration-check"),
        "NM": ("NM Voter Portal",        "https://voterportal.servis.sos.nm.gov/WhereToVote.aspx"),
        "NY": ("NY Voter Status",        "https://voterlookup.elections.ny.gov/"),
        "NC": ("NC Voter Lookup",        "https://vt.ncsbe.gov/RegLkup/"),
        "ND": ("ND Voter Portal",        "https://vip.sos.nd.gov/PortalList.aspx"),
        "OH": ("OH Voter Search",        "https://voterlookup.ohiosos.gov/voterlookup.aspx"),
        "OK": ("OK Voter Search",        "https://www.ok.gov/elections/Voter_Info/Voter_Search/index.html"),
        "OR": ("OR Voter Status",        "https://sos.oregon.gov/voting/pages/myvote.aspx"),
        "PA": ("PA Voter Status",        "https://www.pavoterservices.pa.gov/pages/voterregistrationstatus.aspx"),
        "RI": ("RI Voter Lookup",        "https://vote.sos.ri.gov/"),
        "SC": ("SC Voter Status",        "https://www.scvotes.gov/vote/voterregistrationstatus"),
        "SD": ("SD Voter Lookup",        "https://vip.sdsos.gov/viplogin.aspx"),
        "TN": ("TN Voter Lookup",        "https://tnmap.tn.gov/voterlookup/"),
        "TX": ("TX Voter Search",        "https://teamrv-mvp.sos.texas.gov/MVP/mvp.do"),
        "UT": ("UT Voter Status",        "https://votesearch.utah.gov/voter-search/search/search-by-name/voter-info"),
        "VT": ("VT Voter Lookup",        "https://mvp.sec.state.vt.us/"),
        "VA": ("VA Voter Lookup",        "https://vote.elections.virginia.gov/VoterInformation"),
        "WA": ("WA Voter Status",        "https://voter.votewa.gov/WhereToVote.aspx"),
        "WV": ("WV Voter Search",        "https://ovr.sos.wv.gov/Register/Landing"),
        "WI": ("WI Voter Lookup",        "https://myvote.wi.gov/en-us/"),
        "WY": ("WY Voter Search",        "https://sos.wyo.gov/elections/"),
    }

    # Interactive lookups that CANNOT be pre-filled from a URL — presented
    # honestly as manual steps (Mode 5). VoterRecords.com and the state voter
    # portal are stateful search forms; a link only reaches their front door,
    # not the subject.
    state_portal = voter_portals.get(state.upper() if state else "NM")
    manual = [
        ("VoterRecords.com — all states (type the name on the site)",
         "https://voterrecords.com/"),
    ]
    if state_portal:
        manual.append((f"{state_portal[0]} — official state portal (form; type the name)",
                       state_portal[1]))
    else:
        manual.append(("State Voter Portal Finder", "https://www.usa.gov/voter-registration-card"))
    emit_section(lines, "MANUAL LOOKUPS — SEARCH BY HAND (cannot be pre-filled)",
                 manual, seen,
                 note="These are interactive search portals. A URL only opens the "
                      "front page — open each and search by hand.")

    verify = [
        ("USPS Address Lookup",           "https://tools.usps.com/zip-code-lookup.htm?byaddress"),
        ("Melissa Address Check",         "https://www.melissa.com/v2/lookups/addresscheck/"),
        ("NETR Property Records",          "https://publicrecords.netronline.com/"),
    ]
    emit_section(lines, "TIER 4 — ADDRESS VERIFICATION (FREE)", verify, seen)

    # Google helper links routed through google_dork() (Mode 6: no quoted state,
    # single quoted phrase, no stacked intext:). NM SOS business search is a
    # business registry — it does NOT index people by personal name, so it is
    # generated ONLY from an actual employer/business name (below), never here.
    _, li_state_url = google_dork(name_part, sites=["linkedin.com"], terms=state)
    _, emp_dork_url = google_dork(name_part, terms="employer OR works OR employed")
    employment = [
        ("LinkedIn People Search",         f"https://www.linkedin.com/search/results/people/?keywords={name_plus}&origin=GLOBAL_SEARCH_HEADER"),
        ("Google LinkedIn + State",        li_state_url),
        ("Google Employer Dork",           emp_dork_url),
        ("FEC Political Donations ★",      f"https://www.fec.gov/data/receipts/individual-contributions/?contributor_name={name_plus}"),
        ("OpenSecrets Donor Search ★",     f"https://www.opensecrets.org/donor-lookup/results?name={name_plus}"),
        ("NPPES Medical NPI (National)",   f"https://npiregistry.cms.hhs.gov/search?search_type=ind&first_name={first}&last_name={last}"),
        ("BLS License Lookup (National)",  f"https://www.careeronestop.org/Toolkit/Credentials/find-licenses.aspx?keyword={name_plus}"),
        ("NM Contractor License",          "https://www.rld.nm.gov/licensing-and-regulation/"),
        ("NM Bar (if NM attorney)",        "https://www.nmbar.org/"),
    ]
    emit_section(lines, "TIER 5 — WORKPLACE & EMPLOYMENT (FREE)", employment, seen,
                 note="Employer address = alternative service of process location.")

    # Employer routed inline (business module stays COMPANY-only): search the
    # free-text employer against NM SOS + OpenCorporates and tie subject↔employer.
    # NM SOS business searches are correct HERE because they run on a business
    # name, not the subject's personal name.
    if employer:
        _, subj_emp_url = google_dork(name_part, phrase=employer)
        emp_links = [
            ("Employer — NM SOS Business Search",  f"https://portal.sos.state.nm.us/BFS/online/CorporationFormation/SearchBusinesses?SearchCriteria={employer_plus}"),
            ("Employer — NM SOS (new portal)",     f"https://enterprise.sos.nm.gov/search/business?searchType=byName&searchValue={employer_plus}"),
            ("Employer — OpenCorporates (NM)",     f"https://opencorporates.com/companies?q={employer_plus}&jurisdiction_code=us_nm"),
            ("Employer — LinkedIn Company",        f"https://www.linkedin.com/search/results/companies/?keywords={employer_plus}"),
            ("Subject @ Employer (Google)",        subj_emp_url),
        ]
        kept = dedup_links(emp_links, seen)
        if kept:
            lines.append(f"★ EMPLOYER PROVIDED: {employer}")
            lines.append("  Employer is often known to the client/victim — ask them if unsure.")
            lines.append("  Employer address = alternative service-of-process location.")
            lines.append("")
            for name, url in kept:
                lines.append(f"[{name}]")
                lines.append(f"  {url}")
                lines.append("")

    # Phone routed inline into skip-trace reverse-lookup links (the live
    # pattern-analysis path stays in the Phone module — not replicated here).
    if phone_clean:
        phone_links = [
            ("TruePeopleSearch (phone)",   f"https://www.truepeoplesearch.com/results?phoneno={phone_clean}"),
            ("ThatsThem (phone)",          f"https://thatsthem.com/phone/{phone_clean}"),
            ("FastPeopleSearch (phone)",   f"https://www.fastpeoplesearch.com/phone/{phone_clean}"),
            ("USPhoneBook (phone)",        f"https://www.usphonebook.com/{phone_clean}"),
            ("NumLookup (carrier/owner)",  f"https://www.numlookup.com/?number={phone_clean}"),
            ("SpyDialer (voicemail name)", f"https://www.spydialer.com/default.aspx?phone={phone_clean}"),
        ]
        emit_section(lines, "PHONE (PROVIDED) — REVERSE LOOKUP", phone_links, seen,
                     note=f"★ PHONE PROVIDED: {phone_clean} — reverse-lookup for "
                          "name/address confirmation.")

    # Email routed inline into skip-trace (the live account-detection path stays
    # in the Email module; breach_leak does the live breach check on the email).
    if email:
        email_enc = urllib.parse.quote(email, safe="")
        email_links = [
            ("ThatsThem (email)",          f"https://thatsthem.com/email/{email_enc}"),
            ("IntelTechniques Email",      f"https://inteltechniques.com/tools/Email.html"),
            ("Have I Been Pwned",          f"https://haveibeenpwned.com/account/{email_enc}"),
            ("EmailRep.io",                f"https://emailrep.io/{email_enc}"),
        ]
        emit_section(lines, "EMAIL (PROVIDED) — IDENTITY & REVERSE LOOKUP",
                     email_links, seen,
                     note=f"★ EMAIL PROVIDED: {email} — confirms identity and "
                          "surfaces linked accounts.")

    try:
        san_url = ("https://api.opensanctions.org/search/default?q="
                   + urllib.parse.quote_plus(name_part) + "&schema=Person")
        san_data = http_get_json(san_url, timeout=10)
        results = san_data.get("results", [])
        lines.append("=" * 50)
        lines.append("LIVE SANCTIONS / WATCHLIST CHECK")
        lines.append("=" * 50)
        lines.append("")
        if results:
            lines.append(f"⚠ WARNING: {len(results)} MATCH(ES) FOUND")
            for r in results[:5]:
                lines.append(f"  • {r.get('caption','?')} — Score: {r.get('score','?')}")
        else:
            lines.append("✓ No matches found on sanctions/watchlists")
        lines.append("")
    except Exception as e:
        log_err("OpenSanctions live check", e)

    lines.append("=" * 50)
    lines.append("SKIP TRACE GOOGLE DORKS")
    lines.append("=" * 50)
    lines.append("")

    # Caseload-tuned, category-grouped PERSON dorks. Reference constants and
    # assembly live in build_person_dorks() near the top of the dork logic so
    # this stays tunable over time. `seen` is threaded so dork URLs de-duplicate
    # against the links already emitted above.
    lines.extend(build_person_dorks(p, extra, dob=dob, seen=seen))

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "skip_trace", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: SOCIAL MEDIA SEARCH
# ══════════════════════════════════════════════════════════════════════════════

def module_social_media(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "social_media"})

    p = resolve_person(target, extra)
    name_quoted = p["primary_name"]
    parts = name_quoted.split()
    first = p["first"] or (parts[0] if parts else target)
    last = p["last_primary"] or (parts[-1] if len(parts) > 1 else "")
    name_plus = name_quoted.replace(" ", "+")
    city = p["city"] or "Albuquerque"

    ex = extra or {}
    username = (ex.get("username") or "").strip()
    # Exact handle when provided; else fall back to a first+last guess for the
    # handle-based platforms (Snapchat/Venmo/Cash App).
    handle = username.lower() if username else f"{first.lower()}{last.lower()}"

    lines = []
    lines.append(f"TARGET: {name_quoted}")
    if username:
        lines.append(f"USERNAME: {username}")
    lines.append("")

    lines.append("=" * 50)
    lines.append("FACEBOOK INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    fb = [
        ("People Search",    f"https://www.facebook.com/search/people/?q={name_plus}"),
        ("Posts mentioning", f"https://www.facebook.com/search/posts/?q={name_plus}"),
        ("Photos tagged",    f"https://www.facebook.com/search/photos/?q={name_plus}"),
        ("Check-ins",        f"https://www.facebook.com/search/places/?q={name_plus}"),
        ("Groups",           f"https://www.facebook.com/search/groups/?q={name_plus}"),
        ("Events",           f"https://www.facebook.com/search/events/?q={name_plus}"),
        ("Marketplace",      f"https://www.facebook.com/marketplace/search/?query={name_plus}"),
        ("Sowsearch (Deep)", f"https://sowsearch.info/search?q={name_plus}"),
        ("Google FB Search", google_dork(name_quoted, sites=["facebook.com"])[1]),
    ]
    for label, url in fb:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("INSTAGRAM INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    ig = [
        ("Profile Search",   f"https://www.instagram.com/explore/search/keyword/?q={name_plus}"),
        ("Hashtag Search",   f"https://www.instagram.com/explore/tags/{name_plus.replace('+','')}/"),
        ("Google IG Search", google_dork(name_quoted, sites=["instagram.com"])[1]),
    ]
    for label, url in ig:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("TWITTER/X INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    tw = [
        ("People Search",  f"https://twitter.com/search?q=%22{name_plus}%22&f=user"),
        ("Recent Posts",   f"https://twitter.com/search?q=%22{name_plus}%22&f=live"),
        ("Top Posts",      f"https://twitter.com/search?q=%22{name_plus}%22&f=top"),
        ("Near Location",  f"https://twitter.com/search?q=%22{name_plus}%22+near%3A%22{city or 'Albuquerque'}%22"),
    ]
    for label, url in tw:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("LINKEDIN INTELLIGENCE")
    lines.append("=" * 50)
    lines.append("")
    li = [
        ("People Search",    f"https://www.linkedin.com/search/results/people/?keywords={name_plus}"),
        ("Posts Search",     f"https://www.linkedin.com/search/results/content/?keywords={name_plus}"),
        ("Google LI Search", google_dork(name_quoted, sites=["linkedin.com/in"])[1]),
    ]
    for label, url in li:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("TIKTOK / YOUTUBE / REDDIT")
    lines.append("=" * 50)
    lines.append("")
    other = [
        ("TikTok User",    f"https://www.tiktok.com/search/user?q={name_plus}"),
        ("YouTube Channel",f"https://www.youtube.com/results?search_query={name_plus}&sp=EgIQAg%253D%253D"),
        ("Reddit User",    f"https://www.reddit.com/search/?q=%22{name_plus}%22&type=user"),
        ("Reddit Posts",   f"https://www.reddit.com/search/?q=%22{name_plus}%22"),
    ]
    for label, url in other:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("OTHER PLATFORMS")
    lines.append("=" * 50)
    lines.append("")
    misc = [
        ("Snapchat",    f"https://www.snapchat.com/add/{handle}"),
        ("Pinterest",   f"https://www.pinterest.com/search/people/?q={name_plus}"),
        ("Nextdoor",    "https://nextdoor.com/find-neighbors/"),
        ("Meetup",      f"https://www.meetup.com/find/?keywords={name_plus}"),
        ("Venmo",       f"https://venmo.com/{handle}"),
        ("Cash App",    f"https://cash.app/${handle}"),
    ]
    for label, url in misc:
        lines.append(f"[{label}]")
        lines.append(f"  {url}")
        lines.append("")

    try:
        ddg_url = ("https://api.duckduckgo.com/?q="
                   + urllib.parse.quote_plus(f"{name_quoted} social media")
                   + "&format=json&no_html=1")
        ddg_data = http_get_json(ddg_url, timeout=10)
        if ddg_data.get("Abstract"):
            lines.append("=" * 50)
            lines.append("PUBLIC PROFILE SUMMARY")
            lines.append("=" * 50)
            lines.append("")
            lines.append(ddg_data["Abstract"])
            if ddg_data.get("AbstractURL"):
                lines.append(f"Source: {ddg_data['AbstractURL']}")
            lines.append("")
    except Exception as e:
        log_err("DuckDuckGo social lookup", e)

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "social_media", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: SOCIAL FOOTPRINT
# ══════════════════════════════════════════════════════════════════════════════

def module_social_footprint(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "social_footprint"})
    p = resolve_person(target, extra)
    name_part = p["primary_name"]
    location_part = p["location_part"]
    state = p["state"]
    first = p["first"]
    last = p["last_primary"]
    name_plus = name_part.replace(" ", "+")
    loc_plus = location_part.replace(" ", "+")

    ex = extra or {}
    username = (ex.get("username") or "").strip()
    email = (ex.get("email") or "").strip()

    # When an exact handle is provided, feed it in directly and SKIP name-guessed
    # permutations. Permutations are kept only when no username is given.
    handle_for_tools = username.lower() if username else f"{first.lower()}{last.lower()}"
    if username:
        username_variants = [username]
    elif first and last:
        username_variants = [
            f"{first.lower()}{last.lower()}",
            f"{first.lower()}.{last.lower()}",
            f"{first.lower()}_{last.lower()}",
            f"{first.lower()}{last.lower()[:3]}",
            f"{first.lower()[0]}{last.lower()}",
        ]
    else:
        username_variants = []

    lines = []
    lines.append(f"TARGET:   {name_part}")
    if location_part:
        lines.append(f"LOCATION: {location_part}")
    if username:
        lines.append(f"USERNAME: {username} (exact handle — permutations skipped)")
    lines.append("")

    lines.append("=" * 50)
    lines.append("DIRECT PROFILE ATTEMPTS — USERNAME VARIATIONS")
    lines.append("=" * 50)
    lines.append("")
    lines.append("Click each to check if profile exists.")
    lines.append("")

    if username_variants:
        for uname in username_variants[:4]:
            lines.append(f"Username: {uname}")
            direct = [
                ("Facebook",   f"https://www.facebook.com/{uname}"),
                ("Instagram",  f"https://www.instagram.com/{uname}/"),
                ("Twitter/X",  f"https://twitter.com/{uname}"),
                ("TikTok",     f"https://www.tiktok.com/@{uname}"),
                ("LinkedIn",   f"https://www.linkedin.com/in/{uname}"),
            ]
            for platform, url in direct:
                lines.append(f"  [{platform}]  {url}")
            lines.append("")

    lines.append("=" * 50)
    lines.append("REAL-TIME SOCIAL SEARCH — FREE TOOLS")
    lines.append("=" * 50)
    lines.append("")
    # IDCrawl carries location (Mode 3) via its state-name path segment.
    idcrawl_sf = f"https://www.idcrawl.com/name/{first.lower()}-{last.lower()}"
    if state and STATE_NAMES.get(state.upper()):
        idcrawl_sf += f"/{STATE_NAMES[state.upper()]}"
    realtime = [
        ("Social Searcher ★ FREE",        f"https://www.social-searcher.com/social-buzz/?q={name_plus}"),
        ("Social Catfish (reverse ID)",   f"https://socialcatfish.com/search/?q={name_plus}"),
        ("PeekYou (social+arrests)",      f"https://www.peekyou.com/{first.lower()}_{last.lower()}"),
        ("Sowsearch (FB Deep)",           f"https://sowsearch.info/search?q={name_plus}"),
        ("Boardreader (forums)",          f"https://boardreader.com/s/{name_plus}.html"),
        ("WhatsMyName (usernames)",       f"https://whatsmyname.app/?q={handle_for_tools}"),
        ("IDCrawl (social+records)",      idcrawl_sf),
        ("Epieos (email→social)",         f"https://epieos.com/?q={name_plus}&t=name"),
    ]
    for name, url in realtime:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("LINKEDIN DEEP SEARCH")
    lines.append("=" * 50)
    lines.append("")
    # Google LinkedIn dorks routed through google_dork() (Mode 6: name quoted
    # once, state/location loose — never quoted).
    _, li_exact = google_dork(name_part, sites=["linkedin.com/in"])
    _, li_loc = google_dork(name_part, sites=["linkedin.com"],
                            terms=(location_part or "New Mexico"))
    li = [
        ("LinkedIn People Search",     f"https://www.linkedin.com/search/results/people/?keywords={name_plus}"),
        ("LinkedIn + Location filter", f"https://www.linkedin.com/search/results/people/?keywords={name_plus}&geoUrn=%5B%22102095887%22%5D"),
        ("Google LI Profile (exact)",  li_exact),
        ("Google LI + Location",       li_loc),
    ]
    for name, url in li:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("REVERSE IMAGE & FACE SEARCH — FREE")
    lines.append("=" * 50)
    lines.append("")
    lines.append("Upload subject photo to find additional profiles.")
    lines.append("")
    face = [
        ("Yandex ★ BEST for faces",  "https://yandex.com/images/"),
        ("PimEyes (face search)",     "https://pimeyes.com/en"),
        ("Lenso.ai (face search)",    "https://lenso.ai/en"),
        ("Google Reverse Image",      "https://images.google.com/"),
        ("TinEye",                    "https://tineye.com/"),
        ("Bing Visual Search",        "https://www.bing.com/images/search?view=detailv2&iss=sbi"),
    ]
    for name, url in face:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("SOCIAL FOOTPRINT DORKS")
    lines.append("=" * 50)
    lines.append("")
    # Name-based social dorks run under BOTH surname orderings when present
    # (consistent with the other person modules).
    for nf in p["name_orderings"]:
        if len(p["name_orderings"]) > 1:
            lines.append(f"── DORKS FOR: {nf} ──")
            lines.append("")
        dorks = [
            f'"{nf}" site:facebook.com',
            f'"{nf}" site:instagram.com',
            f'"{nf}" site:twitter.com',
            f'"{nf}" site:linkedin.com',
            f'"{nf}" "{location_part}" social media',
            f'"{nf}" @gmail.com OR @yahoo.com OR @hotmail.com',
        ]
        for dork in dorks:
            encoded = dork.replace(" ", "+").replace('"', '%22')
            lines.append(f"  {dork}")
            lines.append(f"  https://www.google.com/search?q={encoded}")
            lines.append("")

    # Identifier-specific dorks (not name-ordering dependent).
    id_dorks = []
    if username:
        id_dorks.append(f'"{username}"')
        id_dorks.append(f'"{username}" site:instagram.com OR site:twitter.com OR site:tiktok.com')
    if email:
        id_dorks.append(f'"{email}"')
    for dork in id_dorks:
        encoded = dork.replace(" ", "+").replace('"', '%22')
        lines.append(f"  {dork}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "social_footprint", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: HIT & RUN / VEHICLE INVESTIGATION
# PLATE and LOCATION types only — not PERSON
# ══════════════════════════════════════════════════════════════════════════════

def module_hit_and_run(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "hit_and_run"})

    target_clean = target.upper().strip()
    parts = target_clean.split()
    NM_STATES = ["NM","TX","AZ","CO","CA","FL","NY","IL","OH","GA","NC","MI","PA","WA","OR"]

    if len(parts) >= 2 and parts[-1] in NM_STATES:
        plate = parts[0].replace("-", "").replace(",", "").strip()
        state = parts[-1]
    elif len(parts) == 1 and 3 <= len(target_clean) <= 8 and target_clean.replace("-","").replace(",","").isalnum():
        plate = target_clean.replace("-", "").replace(",", "").strip()
        state = "NM"
    else:
        plate = target_clean.replace(" ", "").replace("-", "").replace(",", "").strip()
        state = "NM"

    is_vin = len(plate) == 17

    lines = []
    lines.append(f"TARGET:  {target}")
    lines.append(f"PARSED:  {'VIN' if is_vin else 'Plate'}={plate}  State={state}")
    lines.append("")
    lines.append("⚠ NOTE: Free tools return make/model/theft data only.")
    lines.append("  Owner name/address requires state MVD DPPA request.")
    lines.append("")

    lines.append("=" * 50)
    lines.append("STEP 1 — IDENTIFY THE VEHICLE FROM PHOTO/VIDEO (FREE)")
    lines.append("=" * 50)
    lines.append("")
    lines.append("If you have a photo or partial image of the vehicle:")
    lines.append("")
    image_tools = [
        ("Carnet.ai — ID make/model from photo ★",   "https://carnet.ai/"),
        ("Remini — clean blurry/dark images",         "https://app.remini.ai/"),
        ("LetsEnhance — upscale low-res image",       "https://letsenhance.io/"),
        ("Google Reverse Image Search",               "https://images.google.com/"),
        ("Yandex Reverse Image (better for vehicles)","https://yandex.com/images/"),
        ("TinEye — find where image appears online",  "https://tineye.com/"),
    ]
    for name, url in image_tools:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("STEP 2 — VIN & PLATE LOOKUP (FREE)")
    lines.append("=" * 50)
    lines.append("")

    if is_vin:
        vin_links = [
            ("NHTSA VIN Decoder ★ FREE — specs/recalls",    f"https://vpic.nhtsa.dot.gov/decoder/Car/{plate}/0"),
            ("Driving-Tests.org VIN — 100% free no signup", f"https://driving-tests.org/vin-decoder/?vin={plate}"),
            ("EpicVIN — free basic decode",                 f"https://epicvin.com/vin-decoder?vin={plate}"),
            ("VinFreeCheck — free specs",                   f"https://www.vinfreecheck.com/?vin={plate}"),
            ("NICB VINCheck ★ FREE — stolen/salvage",      "https://www.nicb.org/vincheck"),
            ("NHTSA Recalls by VIN",                        f"https://www.nhtsa.gov/vehicle/{plate}///complaints"),
            ("NMVTIS Title Check",                          "https://www.vehiclehistory.gov/"),
        ]
    else:
        vin_links = [
            ("NHTSA VIN Decoder ★ FREE",                    "https://vpic.nhtsa.dot.gov/decoder/"),
            ("Driving-Tests.org VIN — 100% free no signup", "https://driving-tests.org/vin-decoder/"),
            ("EpicVIN Plate Lookup — free basic",           f"https://epicvin.com/license-plate-lookup?plate={plate}&state={state}"),
            ("Faxvin Plate Search — free decode",           f"https://www.faxvin.com/license-plate-lookup/{state.lower()}/{plate}"),
            ("VehicleHistory.com Plate",                    f"https://www.vehiclehistory.com/license-plate-search?plate={plate}&state={state}"),
            ("NICB VINCheck ★ FREE — stolen/salvage",      "https://www.nicb.org/vincheck"),
            ("NMVTIS Title Check",                          "https://www.vehiclehistory.gov/"),
            ("NHTSA Recalls",                               "https://www.nhtsa.gov/recalls"),
        ]
    for name, url in vin_links:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("STEP 3 — SOCIAL MEDIA PLATE SEARCH (FREE)")
    lines.append("=" * 50)
    lines.append("")
    lines.append("People post crash photos, road rage clips, and dashcam footage.")
    lines.append("")
    social_plate = [
        ("Facebook Posts",                f"https://www.facebook.com/search/posts/?q={plate}"),
        ("Instagram",                     f"https://www.instagram.com/explore/search/keyword/?q={plate}"),
        ("Twitter/X Live",                f"https://twitter.com/search?q=%22{plate}%22&f=live"),
        ("Reddit",                        f"https://www.reddit.com/search/?q=%22{plate}%22"),
        ("YouTube",                       f"https://www.youtube.com/results?search_query=%22{plate}%22"),
        ("Google Images Plate",           f"https://www.google.com/search?tbm=isch&q=%22{plate}%22+New+Mexico"),
        ("Nextdoor — local witness posts","https://nextdoor.com/"),
    ]
    for name, url in social_plate:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("STEP 4 — WITNESS & DASHCAM SOURCING (FREE)")
    lines.append("=" * 50)
    lines.append("")
    witness = [
        ("r/NewMexico — local hit & run reports",     "https://www.reddit.com/r/newmexico/search/?q=hit+and+run&sort=new"),
        ("r/Albuquerque",                             "https://www.reddit.com/r/Albuquerque/search/?q=hit+and+run&sort=new"),
        ("Google News — ABQ hit and run",             f"https://www.google.com/search?q=%22hit+and+run%22+%22albuquerque%22&tbm=nws"),
        ("ABQ Journal Search",                        f"https://www.abqjournal.com/?s=hit+run"),
        ("Waze Incident Map",                         "https://www.waze.com/livemap"),
    ]
    for name, url in witness:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("STEP 5 — OWNER IDENTIFICATION")
    lines.append("=" * 50)
    lines.append("")
    lines.append("Once make/model/plate confirmed:")
    lines.append("  → NM MVD DPPA request for registered owner (staff handles)")
    lines.append("  → Run owner name through SKIP TRACE module")
    lines.append("  → Run owner name through PEOPLE SEARCH module")
    lines.append("  → NM Courts prior incidents:")
    lines.append("    https://caselookup.nmcourts.gov/caselookup/app")
    lines.append("")

    lines.append("=" * 50)
    lines.append("GOOGLE DORKS")
    lines.append("=" * 50)
    lines.append("")
    dorks = [
        f'"{plate}" New Mexico accident OR crash OR "hit and run"',
        f'"{plate}" NM plate dashcam OR witness OR footage',
        f'"{plate}" site:facebook.com',
        f'"{plate}" site:reddit.com',
        f'"{target}" "hit and run" Albuquerque OR "New Mexico"',
        f'"{target}" accident OR crash "New Mexico"',
    ]
    for dork in dorks:
        encoded = dork.replace(" ", "+").replace('"', '%22')
        lines.append(f"  {dork}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "hit_and_run", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: PHOTO FORENSICS
# ══════════════════════════════════════════════════════════════════════════════

def module_photo_forensics(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "photo_forensics"})
    from urllib.parse import quote
    lines = []
    lines.append(f"TARGET: {target}")
    lines.append("")
    is_url = target.startswith("http")
    target_encoded = quote(target, safe='') if is_url else target

    lines.append("=" * 50)
    lines.append("REVERSE IMAGE SEARCH — FREE")
    lines.append("=" * 50)
    lines.append("")

    if is_url:
        rev = [
            ("Google Reverse Image",    f"https://images.google.com/searchbyimage?image_url={target_encoded}"),
            ("TinEye",                  f"https://tineye.com/search?url={target_encoded}"),
            ("Yandex (Best for faces)", f"https://yandex.com/images/search?url={target_encoded}&rpt=imageview"),
            ("Bing Visual Search",      f"https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:{target_encoded}"),
            ("Lenso.ai (Face Search)",  f"https://lenso.ai/en?url={target_encoded}"),
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
    lines.append("PHOTO METADATA EXTRACTION — FREE")
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
    lines.append("VIDEO FORENSICS — FREE")
    lines.append("=" * 50)
    lines.append("")
    video = [
        ("InVID WeVerify",      "https://www.invid-project.eu/tools-and-services/invid-verification-plugin/"),
        ("YouTube DataViewer",  "https://citizenevidence.amnestyusa.org/"),
        ("TrueMedia.org",       "https://www.truemedia.org/"),
    ]
    for name, url in video:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("GEOLOCATION FROM PHOTOS — FREE")
    lines.append("=" * 50)
    lines.append("")
    geo = [
        ("SunCalc (shadow/time analysis)", "https://www.suncalc.org/"),
        ("Google Maps Street View",        "https://www.google.com/maps"),
        ("Bing Maps Bird's Eye",           "https://www.bing.com/maps"),
        ("Google Earth Web",               "https://earth.google.com/web/"),
        ("Overpass Turbo (OpenStreetMap)", "https://overpass-turbo.eu/"),
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
        img_path = os.path.join(tempfile.gettempdir(), "fivet_img.jpg")
        try:
            dl_req = urllib.request.Request(target, headers={"User-Agent": DEFAULT_UA})
            with urllib.request.urlopen(dl_req, timeout=15) as r:
                img_bytes = r.read(10 * 1024 * 1024)  # cap at 10 MB
            with open(img_path, "wb") as f:
                f.write(img_bytes)
            out, _, _ = run_cmd(["exiftool", img_path], timeout=15)
            out = "\n".join(out.splitlines()[:40])
            if out:
                lines.append(out)
            else:
                lines.append("No metadata extracted — image may have metadata stripped.")
        except Exception as e:
            log_err("photo_forensics image fetch", e)
            lines.append("Could not fetch image for automated extraction.")
        lines.append("")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "photo_forensics", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: GEOLOCATION
# ══════════════════════════════════════════════════════════════════════════════

def module_geolocation(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "geolocation"})
    lines = []
    lines.append(f"TARGET: {target}")
    lines.append("")
    loc_plus = target.replace(" ", "+")

    lines.append("=" * 50)
    lines.append("MAP INTELLIGENCE — FREE")
    lines.append("=" * 50)
    lines.append("")
    maps = [
        ("Google Maps",        f"https://www.google.com/maps/search/{loc_plus}"),
        ("Google Street View", f"https://www.google.com/maps?q={loc_plus}&layer=c"),
        ("Google Earth Web",   f"https://earth.google.com/web/search/{loc_plus}"),
        ("Bing Maps",          f"https://www.bing.com/maps?q={loc_plus}"),
        ("OpenStreetMap",      f"https://www.openstreetmap.org/search?query={loc_plus}"),
        ("Apple Maps",         f"https://maps.apple.com/?q={loc_plus}"),
    ]
    for name, url in maps:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("SATELLITE & HISTORICAL IMAGERY — FREE")
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
    lines.append("SPECIALIZED LOCATION TOOLS — FREE")
    lines.append("=" * 50)
    lines.append("")
    special = [
        ("Wigle.net (WiFi Networks)", "https://wigle.net/search#fullSearch"),
        ("Overpass Turbo",            "https://overpass-turbo.eu/"),
        ("SunCalc (Sun Position)",    "https://www.suncalc.org/"),
        ("CalcMaps (Distance/Area)",  "https://www.calcmaps.com/map-distance/"),
    ]
    for name, url in special:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    if re.match(r'^\d+\.\d+\.\d+\.\d+$', target):
        try:
            data = http_get_json(f"https://ipapi.co/{urllib.parse.quote(target)}/json/")
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
        except Exception as e:
            log_err("ipapi.co geolocation", e)

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "geolocation", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: USERNAME SEARCH
# ══════════════════════════════════════════════════════════════════════════════

def module_username_search(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "username_search"})
    lines = []
    lines.append(f"TARGET USERNAME: {target}")
    lines.append("")

    lines.append("=" * 50)
    lines.append("AUTOMATED SCANNER")
    lines.append("=" * 50)
    lines.append("")
    out, _, _ = run_cmd(["python3", "-m", "sherlock", target, "--timeout", "8"], timeout=120)
    if out and "not found" not in out.lower():
        lines.append("[SHERLOCK — 300+ PLATFORMS]")
        lines.append(out)
        lines.append("")
    out2, _, rc2 = run_cmd(["python3", "-m", "maigret", target, "--top-sites", "50"], timeout=120)
    if out2 and rc2 == 0:
        lines.append("[MAIGRET — FULL DOSSIER]")
        lines.append(out2[:2000])
        lines.append("")

    lines.append("=" * 50)
    lines.append("MANUAL USERNAME SEARCH — FREE")
    lines.append("=" * 50)
    lines.append("")
    sites = [
        ("WhatsMyName ★ 732 platforms, no install",  f"https://whatsmyname.app/?q={target}"),
        ("WhatsMyName.io (official)",                f"https://whatsmyname.io/?q={target}"),
        ("Sherlock Online ★ browser-based",          f"https://sherlock-osint.com/?username={target}"),
        ("Forensic OSINT Username Search",           f"https://www.forensicosint.com/free-tools/username-search?username={target}"),
        ("IDCrawl ★ FREE",                           f"https://www.idcrawl.com/{target}"),
        ("UserSearch.org",                           f"https://usersearch.org/results_normal.php?q={target}"),
        ("Namechk",                                  f"https://namechk.com/{target}"),
        ("Instant Username",                         f"https://instantusername.com/#/{target}"),
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
        ("Twitch",         f"https://www.twitch.tv/{target}"),
        ("Snapchat",       f"https://www.snapchat.com/add/{target}"),
        ("Venmo",          f"https://venmo.com/{target}"),
        ("Cash App",       f"https://cash.app/${target}"),
        ("Telegram",       f"https://t.me/{target}"),
        ("Patreon",        f"https://www.patreon.com/{target}"),
        ("Linktree",       f"https://linktr.ee/{target}"),
        ("Chess.com ★",    f"https://www.chess.com/member/{target}"),
        ("SoundCloud",     f"https://soundcloud.com/{target}"),
        ("Spotify",        f"https://open.spotify.com/user/{target}"),
        ("Steam",          f"https://steamcommunity.com/id/{target}"),
        ("Xbox Gamertag",  f"https://xboxgamertag.com/search/{target}"),
        ("PSN Profiles",   f"https://psnprofiles.com/{target}"),
        ("Flickr",         f"https://www.flickr.com/people/{target}"),
        ("Medium",         f"https://medium.com/@{target}"),
        ("Substack",       f"https://{target}.substack.com"),
        ("About.me",       f"https://about.me/{target}"),
        ("Gravatar",       f"https://gravatar.com/{target}"),
    ]
    for name, url in platforms:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "username_search", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: PHONE LOOKUP
# ══════════════════════════════════════════════════════════════════════════════

def phone_pattern_analysis(number):
    """
    Telespot-inspired pattern analysis.
    Runs 10 phone format variations through DuckDuckGo HTML endpoint,
    parses results, extracts names/locations/usernames with frequency counts.
    No API key required.
    """
    import re
    from urllib.parse import quote

    clean = number.replace("-","").replace("(","").replace(")","").replace(" ","").replace("+1","").strip()
    if len(clean) != 10:
        return None

    # Generate 10 format variations
    formats = [
        f"{clean}",
        f"{clean[:3]}-{clean[3:6]}-{clean[6:]}",
        f"({clean[:3]}) {clean[3:6]}-{clean[6:]}",
        f"({clean[:3]}){clean[3:6]}-{clean[6:]}",
        f"+1{clean}",
        f"+1-{clean[:3]}-{clean[3:6]}-{clean[6:]}",
        f"1-{clean[:3]}-{clean[3:6]}-{clean[6:]}",
        f"1{clean}",
        f"{clean[:3]}.{clean[3:6]}.{clean[6:]}",
        f"{clean[:3]} {clean[3:6]} {clean[6:]}",
    ]

    all_text = []
    searched = 0

    for fmt in formats[:6]:  # Cap at 6 to avoid rate limiting on Render
        try:
            html = http_post(
                "https://html.duckduckgo.com/html/",
                data={"q": f'"{fmt}"'},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=12,
            )
            matches = re.findall(r'class=.result__snippet.[^>]*>([^<]+)<', html)
            snippet = " ".join(matches[:15])
            if snippet:
                all_text.append(snippet)
                searched += 1
        except Exception as e:
            log_err(f"DuckDuckGo phone scrape ({fmt})", e)

    if not all_text:
        return None

    combined = " ".join(all_text).lower()

    # Name extraction — capitalized word pairs common in name contexts
    name_pattern = re.compile(r'\b([A-Z][a-z]{1,15})\s+([A-Z][a-z]{1,20})\b')
    raw_names = {}
    for chunk in all_text:
        for match in name_pattern.finditer(chunk):
            name = f"{match.group(1)} {match.group(2)}"
            # Filter common false positives
            skip = ['Search Results','Phone Number','White Pages','People Search',
                    'Real People','Find People','Public Records','United States',
                    'New Mexico','North America','South America','East Coast',
                    'West Coast','Phone Book','Reverse Lookup','Free Search']
            if name not in skip and len(name) > 6:
                raw_names[name] = raw_names.get(name, 0) + 1

    # Location extraction — city/state patterns
    us_states = ['Alabama','Alaska','Arizona','Arkansas','California','Colorado',
                 'Connecticut','Delaware','Florida','Georgia','Hawaii','Idaho',
                 'Illinois','Indiana','Iowa','Kansas','Kentucky','Louisiana',
                 'Maine','Maryland','Massachusetts','Michigan','Minnesota',
                 'Mississippi','Missouri','Montana','Nebraska','Nevada',
                 'New Hampshire','New Jersey','New Mexico','New York',
                 'North Carolina','North Dakota','Ohio','Oklahoma','Oregon',
                 'Pennsylvania','Rhode Island','South Carolina','South Dakota',
                 'Tennessee','Texas','Utah','Vermont','Virginia','Washington',
                 'West Virginia','Wisconsin','Wyoming','District of Columbia']
    state_abbrevs = ['AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID',
                     'IL','IN','IA','KS','KY','LA','ME','MD','MA','MI','MN','MS',
                     'MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK',
                     'OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV',
                     'WI','WY','DC']

    raw_locs = {}
    joined = " ".join(all_text)
    # Full state names: word-boundary, case-insensitive.
    for state in us_states:
        count = len(re.findall(r'\b' + re.escape(state) + r'\b', joined, re.IGNORECASE))
        if count > 0:
            raw_locs[state] = count
    for abbr in state_abbrevs:
        count = len(re.findall(r'\b' + abbr + r'\b', " ".join(all_text)))
        if count > 0:
            existing = raw_locs.get(abbr, 0)
            raw_locs[abbr] = existing + count

    # City extraction — look for "city, ST" patterns
    city_pattern = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?),\s*([A-Z]{2})\b')
    for chunk in all_text:
        for match in city_pattern.finditer(chunk):
            city_state = f"{match.group(1)}, {match.group(2)}"
            raw_locs[city_state] = raw_locs.get(city_state, 0) + 2  # weight city+state higher

    # Username extraction — @handle patterns
    username_pattern = re.compile(r'@([a-zA-Z0-9_]{3,20})')
    raw_usernames = {}
    for chunk in all_text:
        for match in username_pattern.finditer(chunk):
            uname = f"@{match.group(1)}"
            raw_usernames[uname] = raw_usernames.get(uname, 0) + 1

    # Score confidence
    top_names = sorted(raw_names.items(), key=lambda x: x[1], reverse=True)[:5]
    top_locs = sorted(raw_locs.items(), key=lambda x: x[1], reverse=True)[:5]
    top_users = sorted(raw_usernames.items(), key=lambda x: x[1], reverse=True)[:5]

    max_name_count = top_names[0][1] if top_names else 0
    if max_name_count >= 8:
        confidence = "HIGH"
        conf_pct = min(95, 60 + max_name_count * 3)
    elif max_name_count >= 4:
        confidence = "MEDIUM"
        conf_pct = 40 + max_name_count * 4
    elif max_name_count >= 2:
        confidence = "LOW"
        conf_pct = 20 + max_name_count * 5
    else:
        confidence = "INSUFFICIENT DATA"
        conf_pct = 0

    return {
        "confidence": confidence,
        "confidence_pct": conf_pct,
        "names": top_names,
        "locations": top_locs,
        "usernames": top_users,
        "formats_searched": searched,
    }


def module_phone(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "phone"})
    clean = target.replace("-","").replace("(","").replace(")","").replace(" ","").replace("+1","").strip()
    formatted = f"({clean[:3]}) {clean[3:6]}-{clean[6:]}" if len(clean) == 10 else target
    phone_plus1 = f"+1{clean}" if len(clean) == 10 else target

    lines = []
    lines.append(f"TARGET:    {formatted}")
    lines.append(f"CLEANED:   {clean}")
    lines.append("")

    # Run Telespot-inspired pattern analysis
    if len(clean) == 10:
        try:
            analysis = phone_pattern_analysis(clean)
            if analysis and analysis["confidence"] != "INSUFFICIENT DATA":
                lines.append("=" * 50)
                lines.append("PATTERN ANALYSIS — LIVE CROSS-ENGINE CORRELATION")
                lines.append("=" * 50)
                lines.append("")
                lines.append(f"Confidence:  {analysis['confidence']} ({analysis['confidence_pct']}%)")
                lines.append(f"Formats searched: {analysis['formats_searched']} variations across DuckDuckGo")
                lines.append("")
                if analysis["names"]:
                    lines.append("NAMES FOUND:")
                    for name, count in analysis["names"]:
                        star = " ★" if count == analysis["names"][0][1] else ""
                        lines.append(f"  {name} — {count}x{star}")
                    lines.append("")
                if analysis["locations"]:
                    lines.append("LOCATIONS:")
                    for loc, count in analysis["locations"]:
                        star = " ★" if count == analysis["locations"][0][1] else ""
                        lines.append(f"  {loc} — {count}x{star}")
                    lines.append("")
                if analysis["usernames"]:
                    lines.append("USERNAMES:")
                    for uname, count in analysis["usernames"]:
                        lines.append(f"  {uname} — {count}x")
                    lines.append("")
                lines.append("NOTE: Cross-reference top names through Skip Trace module.")
                lines.append("")
            else:
                lines.append("Pattern analysis: insufficient public data for this number.")
                lines.append("")
        except Exception as e:
            log_err("phone pattern analysis", e)
            lines.append(f"Pattern analysis unavailable: {str(e)}")
            lines.append("")

    ipqs_key = os.environ.get("IPQS_API_KEY", "")
    nv_key = os.environ.get("NUMVERIFY_API_KEY", "")

    if ipqs_key:
        try:
            ipqs_url = ("https://www.ipqualityscore.com/api/json/phone/"
                        + urllib.parse.quote(ipqs_key, safe="")
                        + "/" + urllib.parse.quote(clean, safe=""))
            ipqs_data = http_get_json(ipqs_url, timeout=10)
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
        except Exception as e:
            log_err("IPQS phone lookup", e)

    if nv_key:
        try:
            nv_url = ("http://apilayer.net/api/validate?access_key="
                      + urllib.parse.quote_plus(nv_key)
                      + "&number=" + urllib.parse.quote_plus(clean)
                      + "&country_code=US&format=1")
            nv_data = http_get_json(nv_url, timeout=10)
            if nv_data.get("valid"):
                lines.append("=== CARRIER INTELLIGENCE (NUMVERIFY) ===")
                lines.append(f"Line Type:    {nv_data.get('line_type', 'N/A')}")
                lines.append(f"Carrier:      {nv_data.get('carrier', 'N/A')}")
                lines.append(f"Location:     {nv_data.get('location', 'N/A')}")
                lines.append("")
        except Exception as e:
            log_err("Numverify phone lookup", e)

    if not ipqs_key and not nv_key:
        lines.append("Add IPQS_API_KEY or NUMVERIFY_API_KEY to Render env vars for live carrier data.")
        lines.append("")

    lines.append("=" * 50)
    lines.append("FREE REVERSE LOOKUP SITES")
    lines.append("=" * 50)
    lines.append("")
    lines.append("⚠ NOTE: SpyDialer calls the number silently to retrieve voicemail.")
    lines.append("  Target may see a missed call. Use intentionally.")
    lines.append("")
    sites = [
        ("SPYDIALER ★ FREE — name via voicemail",   f"https://www.spydialer.com/default.aspx?phone={clean}"),
        ("NUMLOOKUP ★ FREE — owner name, carrier",   f"https://www.numlookup.com/?number={clean}"),
        ("ANYWHO (free directory)",                 f"https://www.anywho.com/reverse-lookup/{clean}"),
        ("TRUEPEOPLESEARCH ★ FREE",                  f"https://www.truepeoplesearch.com/results?phoneno={clean}"),
        ("THATSTHEM ★ FREE",                         f"https://thatsthem.com/phone/{clean}"),
        ("FASTPEOPLESEARCH",                          f"https://www.fastpeoplesearch.com/phone/{clean}"),
        ("FONEFINDER (carrier lookup)",              f"https://fonefinder.net/findphone.php?areacode={clean[:3]}&exchange={clean[3:6]}&thenumber={clean[6:]}"),
        ("USPHONEBOOK",                              f"https://www.usphonebook.com/{clean}"),
        ("411.COM",                                  f"https://www.411.com/phone/{clean}"),
        ("TRUECALLER (community ID)",                f"https://www.truecaller.com/search/us/{clean}"),
    ]
    for name, url in sites:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("SPAM & REPORT DATABASES — FREE")
    lines.append("=" * 50)
    lines.append("")
    spam_sites = [
        ("800NOTES",       f"https://800notes.com/Phone.aspx/{clean}"),
        ("CALLERCENTER",   f"https://callercenter.com/{clean}"),
        ("NOMOROBO",       f"https://www.nomorobo.com/lookup/{clean}"),
        ("SPAMCALLS",      f"https://spamcalls.net/en/search?n={clean}"),
        ("WHOCALLEDUS",    f"https://whocalledus.com/calls/{clean}/"),
    ]
    for name, url in spam_sites:
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


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: EMAIL INVESTIGATION
# ══════════════════════════════════════════════════════════════════════════════

def module_email_investigate(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "email_investigate"})
    lines = []
    lines.append(f"TARGET EMAIL: {target}")
    lines.append("")

    out, err, rc = run_cmd(["python3", "-m", "holehe", target, "--only-used"], timeout=120)
    if out and "holehe" not in out.lower() and "error" not in out.lower():
        lines.append("=" * 50)
        lines.append("HOLEHE — ACCOUNT DETECTION (120+ SITES)")
        lines.append("=" * 50)
        lines.append("")
        lines.append(out)
        lines.append("")

    try:
        rep_url = "https://emailrep.io/" + urllib.parse.quote(target, safe="")
        data = http_get_json(rep_url, timeout=10)
        details = data.get("details", {})
        lines.append("=" * 50)
        lines.append("EMAIL REPUTATION — FREE")
        lines.append("=" * 50)
        lines.append("")
        lines.append(f"Reputation:    {data.get('reputation', 'N/A')}")
        lines.append(f"Suspicious:    {data.get('suspicious', False)}")
        lines.append(f"Blacklisted:   {details.get('blacklisted', False)}")
        lines.append(f"Data Breach:   {details.get('data_breach', False)}")
        lines.append(f"Disposable:    {details.get('disposable', False)}")
        lines.append(f"Free Provider: {details.get('free_provider', False)}")
        lines.append(f"Profiles:      {', '.join(details.get('profiles', [])) or 'None detected'}")
        lines.append("")
    except Exception as e:
        log_err("emailrep reputation", e)

    try:
        domain = target.split("@")[1]
        dns_out, _, _ = run_cmd(["dig", "+short", "A", domain])
        mx_out, _, _ = run_cmd(["dig", "+short", "MX", domain])
        if dns_out or mx_out:
            lines.append("=" * 50)
            lines.append(f"EMAIL DOMAIN INTEL: {domain}")
            lines.append("=" * 50)
            lines.append("")
            if dns_out:
                lines.append(f"Domain IP:   {dns_out.split()[0]}")
            if mx_out:
                lines.append(f"Mail Server: {mx_out}")
            lines.append("")
    except Exception as e:
        log_err("email domain DNS", e)

    lines.append("=" * 50)
    lines.append("FREE LOOKUP SITES")
    lines.append("=" * 50)
    lines.append("")
    # Generate MD5 hash of email for Gravatar lookup
    import hashlib
    email_hash = hashlib.md5(target.lower().strip().encode()).hexdigest()
    lines.append("TIP: Gravatar — if this email has a Gravatar account, the avatar URL")
    lines.append("  returns their photo. Reverse image search it for more leads.")
    lines.append(f"  Avatar direct: https://www.gravatar.com/avatar/{email_hash}?d=404")
    lines.append("")

    sites = [
        ("GRAVATAR ★ FREE — photo+name+socials",  f"https://gravatar.com/{email_hash}"),
        ("GRAVATAR AVATAR CHECK",                  f"https://www.gravatar.com/avatar/{email_hash}?d=404"),
        ("TRUEPEOPLESEARCH ★ FREE",                f"https://www.truepeoplesearch.com/results?emailaddress={target}"),
        ("THATSTHEM ★ FREE",                       f"https://thatsthem.com/email/{target}"),
        ("EMAILREP (reputation)",                  f"https://emailrep.io/{target}"),
        ("HUNTER.IO (verify)",                     f"https://hunter.io/email-verifier/{target}"),
        ("EPIEOS (social lookup)",                 f"https://epieos.com/?q={target}&t=email"),
        ("GHunt (Google account OSINT)",           f"https://www.google.com/search?q=%22{target}%22+site:accounts.google.com"),
    ]
    lines.append("NOTE: Breach/leak data for this email is covered in the")
    lines.append("      dedicated Breach & Leak module — run it alongside this one.")
    lines.append("")
    for name, url in sites:
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
        f'"{target}" resume OR CV OR "contact me"',
        f'"{target}" inurl:profile OR inurl:account',
    ]
    for dork in dorks:
        encoded = dork.replace(" ", "+").replace('"', '%22')
        lines.append(f"  {dork}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "email_investigate", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: BREACH & LEAK
# Standalone breach intelligence — live XposedOrNot API (free, no key required)
# plus verified free breach-checking resources and unique leak-focused dorks.
# Available for PERSON, EMAIL, USERNAME target types.
# ══════════════════════════════════════════════════════════════════════════════

def module_breach_leak(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "breach_leak"})
    lines = []

    ex = extra or {}
    provided_email = (ex.get("email") or "").strip()

    # The breach check runs against an email. Use the target when it is itself an
    # email; otherwise fall back to an email supplied alongside a PERSON search.
    if "@" in target and "." in target.split("@")[-1]:
        email_subject = target
    else:
        email_subject = provided_email
    is_email = bool(email_subject) and "@" in email_subject and "." in email_subject.split("@")[-1]

    lines.append(f"TARGET: {target}")
    if email_subject and email_subject != target:
        lines.append(f"EMAIL:  {email_subject}")
    lines.append("")

    # ── LIVE API CALL — XposedOrNot (genuinely free, no key, no signup) ──
    if is_email:
        try:
            xon_url = ("https://api.xposedornot.com/v1/check-email/"
                       + urllib.parse.quote(email_subject, safe=""))
            data = http_get_json(xon_url, timeout=10)
            lines.append("=" * 50)
            lines.append("LIVE BREACH CHECK — XPOSEDORNOT (FREE API)")
            lines.append("=" * 50)
            lines.append("")
            if data.get("status") == "success" and data.get("breaches"):
                breach_list = data["breaches"][0] if isinstance(data["breaches"][0], list) else data["breaches"]
                lines.append(f"⚠ FOUND IN {len(breach_list)} BREACH(ES):")
                for b in breach_list:
                    lines.append(f"  • {b}")
                lines.append("")
                lines.append("This confirms the email is real and has been used to register")
                lines.append("accounts on these specific platforms — useful for identity confirmation")
                lines.append("and finding additional accounts tied to the same subject.")
            elif data.get("Error") == "Not found":
                lines.append("✓ No breaches found for this email in XposedOrNot's database.")
            else:
                lines.append("No conclusive result returned.")
            lines.append("")
        except Exception as e:
            log_err("XposedOrNot breach check", e)
            lines.append(f"XposedOrNot live check unavailable: {str(e)}")
            lines.append("")
    else:
        lines.append("NOTE: Live XposedOrNot API check requires an email address.")
        lines.append("If you have a suspected email for this subject, run it through")
        lines.append("the Email Investigation module or re-run this module with the email.")
        lines.append("")

    # ── FREE BREACH CHECK SITES ──
    lines.append("=" * 50)
    lines.append("FREE BREACH CHECK SITES")
    lines.append("=" * 50)
    lines.append("")

    if is_email:
        breach_sites = [
            ("XposedOrNot ★ FREE — no signup",       f"https://xposedornot.com/"),
            ("Have I Been Pwned ★ FREE (site only)",  f"https://haveibeenpwned.com/account/{email_subject}"),
            ("Mozilla Monitor ★ FREE",                 f"https://monitor.mozilla.org/"),
            ("BreachDirectory ★ FREE",                 f"https://breachdirectory.org/"),
            ("LeakCheck.net (1 free lookup)",          f"https://leakcheck.net/"),
        ]
    else:
        breach_sites = [
            ("XposedOrNot ★ FREE — no signup",       "https://xposedornot.com/"),
            ("Mozilla Monitor ★ FREE",                 "https://monitor.mozilla.org/"),
            ("BreachDirectory ★ FREE",                 "https://breachdirectory.org/"),
            ("LeakCheck.net (1 free lookup)",          "https://leakcheck.net/"),
        ]

    for name, url in breach_sites:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("PAID / PROFESSIONAL TOOLS (for reference — not verified free)")
    lines.append("=" * 50)
    lines.append("")
    lines.append("These require payment for full results — use only if free sources")
    lines.append("are insufficient and the case justifies the cost:")
    lines.append("")
    paid_note = [
        ("DeHashed — per-query pricing",   "https://www.dehashed.com/"),
        ("Intelligence X — paid full results", "https://intelx.io/"),
        ("LeakCheck Pro — subscription",   "https://leakcheck.io/"),
    ]
    for name, url in paid_note:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    # ── UNIQUE BREACH-FOCUSED DORKS — not duplicated anywhere else in FIVE T ──
    lines.append("=" * 50)
    lines.append("BREACH & LEAK DORKS — UNIQUE TO THIS MODULE")
    lines.append("=" * 50)
    lines.append("")
    lines.append("These target leak/dump-specific patterns not covered by")
    lines.append("People Search, Skip Trace, or the general Dorks module.")
    lines.append("")

    dorks = [
        f'"{target}" "combolist" OR "combo list"',
        f'"{target}" "database dump" OR "db dump"',
        f'"{target}" intext:password site:pastebin.com',
        f'"{target}" site:rentry.co OR site:controlc.com',
        f'"{target}" "leaked" filetype:txt OR filetype:csv OR filetype:sql',
        f'"{target}" site:scylla.sh OR site:snusbase.com',
        f'"{target}" "stealer log" OR "stealer logs"',
        f'"{target}" site:intelx.io',
    ]
    # When an email was supplied alongside a name target, add email-keyed leak dorks.
    if email_subject and email_subject != target:
        dorks.append(f'"{email_subject}" "combolist" OR "database dump"')
        dorks.append(f'"{email_subject}" intext:password')
    for dork in dorks:
        encoded = dork.replace(" ", "+").replace('"', '%22')
        lines.append(f"  {dork}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("HOW TO USE THIS MODULE")
    lines.append("=" * 50)
    lines.append("")
    lines.append("1. If subject's email is known — confirms it's real and active")
    lines.append("2. Breach names reveal OTHER platforms subject has accounts on")
    lines.append("   (e.g. found in 'LinkedIn2021' breach = confirms LinkedIn account)")
    lines.append("3. Cross-reference breach platform names with Social Footprint module")
    lines.append("4. A subject with zero breaches across a common email is unusual —")
    lines.append("   may indicate a newer or rarely-used address")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "breach_leak", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: LICENSE PLATE LOOKUP
# ══════════════════════════════════════════════════════════════════════════════

def module_plate_lookup(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "plate_lookup"})
    target_clean = target.upper().strip()
    parts = target_clean.split()
    states = ["NM","TX","AZ","CO","CA","FL","NY","IL","OH","GA","NC","MI","PA","WA","OR"]
    if len(parts) >= 2 and parts[-1] in states:
        plate = parts[0].replace("-", "").replace(",", "").strip()
        state = parts[-1]
    else:
        plate = target_clean.replace(" ", "").replace("-", "").replace(",", "").strip()
        state = "NM"

    lines = []
    lines.append(f"TARGET PLATE: {plate}")
    lines.append(f"STATE:        {state}")
    lines.append(f"NOTE: DPPA permissible purpose required. Law firms qualify.")
    lines.append("")

    lines.append("=" * 50)
    lines.append("FREE VEHICLE LOOKUP")
    lines.append("=" * 50)
    lines.append("")
    free = [
        ("VehicleHistory.com",              f"https://www.vehiclehistory.com/license-plate-search?plate={plate}&state={state}"),
        ("Faxvin Plate Search",             f"https://www.faxvin.com/license-plate-lookup/{state.lower()}/{plate}"),
        ("NICB VINCheck (stolen check)",    "https://www.nicb.org/vincheck"),
        ("NHTSA Recalls",                   "https://www.nhtsa.gov/recalls"),
        ("NMVTIS Vehicle History",          "https://www.vehiclehistory.gov/"),
    ]
    for name, url in free:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append(f"{state} MVD RECORDS REQUEST")
    lines.append("=" * 50)
    lines.append("")
    lines.append("Submit DPPA request to state MVD for registered owner info.")
    lines.append("")
    mvd_links = {
        "NM": ("NM MVD", "https://www.mvd.newmexico.gov/", "(888) 683-4636"),
        "AZ": ("AZ MVD", "https://www.azdot.gov/motor-vehicles", "(602) 712-7355"),
        "TX": ("TX DMV", "https://www.txdmv.gov/", "(888) 368-4689"),
        "CO": ("CO DMV", "https://dmv.colorado.gov/", "(303) 205-5600"),
        "CA": ("CA DMV", "https://www.dmv.ca.gov/", "(800) 777-0133"),
    }
    mvd_name, mvd_url, mvd_phone = mvd_links.get(state, ("State MVD", "https://www.vehiclehistory.gov/", "Check state DMV website"))
    lines.append(f"[{mvd_name} (DPPA request)]")
    lines.append(f"  {mvd_url}")
    lines.append(f"  Phone: {mvd_phone}")
    lines.append("")

    lines.append("=" * 50)
    lines.append("SOCIAL MEDIA PLATE SEARCH")
    lines.append("=" * 50)
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
    lines.append("  • Litigation or investigation in anticipation of litigation")
    lines.append("  • Service of process")
    lines.append("  • Licensed private investigator research")
    lines.append("  • Insurance claims investigation")
    lines.append("  • Locating missing persons or witnesses")
    lines.append("")
    lines.append("Cite: 18 U.S.C. § 2721(b)")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "plate_lookup", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: BUSINESS SEARCH
# ══════════════════════════════════════════════════════════════════════════════

def module_business(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "business"})
    name_plus = target.replace(" ", "+")
    name_url = target.replace(" ", "-").lower()

    lines = []
    lines.append(f"TARGET: {target}")
    lines.append("")

    try:
        oc_url = ("https://api.opencorporates.com/v0.4/companies/search?q="
                  + urllib.parse.quote_plus(target)
                  + "&jurisdiction_code=us_nm&format=json")
        data = http_get_json(oc_url, timeout=10)
        companies = data.get("results", {}).get("companies", [])
        if companies:
            lines.append("=" * 50)
            lines.append("OPENCORPORATES — NM RESULTS (FREE)")
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
        log_err("OpenCorporates search", e)
        lines.append(f"OpenCorporates: {str(e)}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("SECRETARY OF STATE — FREE")
    lines.append("=" * 50)
    lines.append("")
    sos = [
        ("NM SOS Business Search",   f"https://portal.sos.state.nm.us/BFS/online/CorporationFormation/SearchBusinesses?SearchCriteria={name_plus}"),
        ("NM SOS (alternate)",       "https://businessportal.sos.nm.gov/"),
        ("AZ SOS",                   f"https://ecorp.azcc.gov/BusinessSearch/BusinessSearch?SearchTerm={name_plus}"),
        ("CO SOS",                   f"https://www.sos.state.co.us/biz/BusinessEntityCriteriaExt.do?nameTyp=ENT&entityName={name_plus}"),
        ("TX SOS",                   "https://mycpa.cpa.state.tx.us/coa/Index.html"),
    ]
    for name, url in sos:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("FEDERAL DATABASES — FREE")
    lines.append("=" * 50)
    lines.append("")
    federal = [
        ("SAM.gov (Federal Contractors)", f"https://sam.gov/search/?keywords={name_plus}&sort=relevanceScore&index=ei&is_active=true&page=1"),
        ("SEC EDGAR (Public Companies)",  f"https://www.sec.gov/cgi-bin/browse-edgar?company={name_plus}&CIK=&type=&dateb=&owner=include&count=40&search_text=&action=getcompany"),
        ("OpenCorporates All States",     f"https://opencorporates.com/companies?q={name_plus}&jurisdiction_code=us"),
        ("PACER Business Search",         "https://pcl.uscourts.gov/pcl/pages/search/findParty.jsf"),
        ("BBB Albuquerque",               f"https://www.bbb.org/search?find_text={name_plus}&find_loc=Albuquerque%2C+NM"),
    ]
    for name, url in federal:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("BUSINESS INTELLIGENCE — FREE")
    lines.append("=" * 50)
    lines.append("")
    intel = [
        ("LinkedIn Company",     f"https://www.linkedin.com/search/results/companies/?keywords={name_plus}"),
        ("Yelp Business",        f"https://www.yelp.com/search?find_desc={name_plus}&find_loc=Albuquerque%2C+NM"),
        ("Google Business",      f"https://www.google.com/search?q={name_plus}+Albuquerque+NM+business"),
        ("Bizapedia NM",         "https://www.bizapedia.com/nm/"),
        ("Corporationwiki",      f"https://www.corporationwiki.com/search/results?term={name_plus}"),
        ("OpenCorporates Officers", f"https://opencorporates.com/officers?q={name_plus}"),
    ]
    for name, url in intel:
        lines.append(f"[{name}]")
        lines.append(f"  {url}")
        lines.append("")

    try:
        ddg_url = ("https://api.duckduckgo.com/?q="
                   + urllib.parse.quote_plus(f"{target} company")
                   + "&format=json&no_html=1")
        ddg_data = http_get_json(ddg_url, timeout=10)
        if ddg_data.get("Abstract"):
            lines.append("=" * 50)
            lines.append("PUBLIC BUSINESS SUMMARY")
            lines.append("=" * 50)
            lines.append("")
            lines.append(ddg_data["Abstract"])
            if ddg_data.get("AbstractURL"):
                lines.append(f"Source: {ddg_data['AbstractURL']}")
            lines.append("")
    except Exception as e:
        log_err("DuckDuckGo business lookup", e)

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "business", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: WHOIS
# ══════════════════════════════════════════════════════════════════════════════

def module_whois(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "whois"})
    api_key = os.environ.get("WHOIS_API_KEY", "at_free")
    try:
        whois_url = ("https://www.whoisxmlapi.com/whoisserver/WhoisService?apiKey="
                     + urllib.parse.quote_plus(api_key)
                     + "&domainName=" + urllib.parse.quote_plus(target)
                     + "&outputFormat=JSON")
        data = http_get_json(whois_url)
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
    except Exception as e:
        log_err("WhoisXML API (falling back to whois CLI)", e)
        out, err, _ = run_cmd(["whois", target])
        out = "\n".join(out.splitlines()[:40])
        result = out if out else f"WHOIS lookup failed for {target}"
    emit(job_id, "module_done", {"module": "whois", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: DNS RECORDS
# ══════════════════════════════════════════════════════════════════════════════

def module_dns(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "dns"})
    lines = []
    for rtype in ["A", "AAAA", "MX", "NS", "TXT", "CNAME"]:
        out, _, _ = run_cmd(["dig", "+short", rtype, target])
        if out:
            lines.append(f"[{rtype}] {out}")
    result = "\n".join(lines) if lines else "No DNS records found."
    emit(job_id, "module_done", {"module": "dns", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: PORT SCAN
# ══════════════════════════════════════════════════════════════════════════════

def module_nmap(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "nmap"})
    common_ports = {
        21:"FTP",22:"SSH",25:"SMTP",53:"DNS",80:"HTTP",
        110:"POP3",143:"IMAP",443:"HTTPS",445:"SMB",
        3306:"MySQL",3389:"RDP",8080:"HTTP-Alt",8443:"HTTPS-Alt"
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


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: GEOIP
# ══════════════════════════════════════════════════════════════════════════════

def module_geoip(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "geoip"})
    try:
        ip = socket.gethostbyname(target)
        data = http_get_json(f"https://ipapi.co/{urllib.parse.quote(ip)}/json/")
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


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: SHODAN
# ══════════════════════════════════════════════════════════════════════════════

def module_shodan(target, job_id, dob="", ssn="", oln="", extra=None):
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


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: VIRUSTOTAL
# ══════════════════════════════════════════════════════════════════════════════

def module_virustotal(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "virustotal"})
    api_key = os.environ.get("VT_API_KEY", "")
    if not api_key:
        result = "Add VT_API_KEY to Render Environment Variables."
    else:
        vt_url = ("https://www.virustotal.com/api/v3/domains/"
                  + urllib.parse.quote(target, safe=""))
        out = ""
        try:
            out = http_get(vt_url, headers={"x-apikey": api_key})
            data = json.loads(out)
            attrs = data.get("data", {}).get("attributes", {})
            stats = attrs.get("last_analysis_stats", {})
            cats = attrs.get("categories", {})
            result = (
                f"Malicious:   {stats.get('malicious', 0)}\n"
                f"Suspicious:  {stats.get('suspicious', 0)}\n"
                f"Harmless:    {stats.get('harmless', 0)}\n"
                f"Reputation:  {attrs.get('reputation', 'N/A')}\n"
                f"Categories:  {', '.join(set(cats.values())) if cats else 'N/A'}"
            )
        except Exception as e:
            log_err("VirusTotal domain lookup", e)
            result = out[:500] if out else "VirusTotal lookup failed."
    emit(job_id, "module_done", {"module": "virustotal", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE: GOOGLE DORKS
# ══════════════════════════════════════════════════════════════════════════════

def module_google_dorks(target, job_id, dob="", ssn="", oln="", extra=None):
    emit(job_id, "module_start", {"module": "dorks"})

    lines = []
    lines.append(f"TARGET: {target}")
    lines.append("")
    lines.append("Advanced dork operators — these go beyond what other modules generate.")
    lines.append("Copy each into Google. Open multiple tabs for parallel searching.")
    lines.append("")

    lines.append("=" * 50)
    lines.append("IDENTITY & CONTACT")
    lines.append("=" * 50)
    lines.append("")
    identity = [
        f'"{target}" filetype:pdf',
        f'"{target}" filetype:doc OR filetype:docx',
        f'"{target}" inurl:about OR inurl:contact OR inurl:staff',
        f'"{target}" inurl:profile OR inurl:user OR inurl:member',
        f'"{target}" "@gmail.com" OR "@yahoo.com" OR "@hotmail.com" OR "@outlook.com"',
        f'"{target}" phone OR "cell" OR "mobile" OR "tel:"',
    ]
    for d in identity:
        encoded = d.replace(" ", "+").replace('"', '%22').replace("'", "%27")
        lines.append(f"  {d}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("LEGAL & COURT")
    lines.append("=" * 50)
    lines.append("")
    legal = [
        f'"{target}" court OR lawsuit OR plaintiff OR defendant OR judgment',
        f'"{target}" "case number" OR "docket" OR "filing"',
        f'"{target}" arrest OR indicted OR convicted OR "criminal record"',
        f'"{target}" bankruptcy OR "chapter 7" OR "chapter 13"',
        f'"{target}" lien OR "tax lien" OR "mechanic lien"',
        f'"{target}" divorce OR "dissolution of marriage"',
    ]
    for d in legal:
        encoded = d.replace(" ", "+").replace('"', '%22').replace("'", "%27")
        lines.append(f"  {d}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("EMPLOYMENT & BUSINESS")
    lines.append("=" * 50)
    lines.append("")
    employment = [
        f'"{target}" employer OR "works at" OR "employed by" OR "job title"',
        f'"{target}" CEO OR owner OR president OR director OR manager',
        f'"{target}" "LLC" OR "Inc" OR "Corp" OR "DBA"',
        f'"{target}" resume OR CV OR "curriculum vitae"',
        f'"{target}" site:linkedin.com',
        f'"{target}" site:glassdoor.com',
    ]
    for d in employment:
        encoded = d.replace(" ", "+").replace('"', '%22').replace("'", "%27")
        lines.append(f"  {d}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("PROPERTY & ASSETS")
    lines.append("=" * 50)
    lines.append("")
    assets = [
        f'"{target}" property OR deed OR parcel OR assessor',
        f'"{target}" "real estate" OR "home owner" OR mortgage',
        f'"{target}" vehicle OR "license plate" OR VIN OR registration',
        f'"{target}" boat OR watercraft OR aircraft OR "FAA"',
    ]
    for d in assets:
        encoded = d.replace(" ", "+").replace('"', '%22').replace("'", "%27")
        lines.append(f"  {d}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("SOCIAL & FORUMS")
    lines.append("=" * 50)
    lines.append("")
    social = [
        f'"{target}" site:reddit.com',
        f'"{target}" site:nextdoor.com',
        f'"{target}" site:quora.com',
        f'"{target}" site:medium.com',
        f'"{target}" site:pastebin.com OR site:ghostbin.com OR site:rentry.co',
        f'"{target}" site:github.com',
    ]
    for d in social:
        encoded = d.replace(" ", "+").replace('"', '%22').replace("'", "%27")
        lines.append(f"  {d}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("NEWS & MEDIA")
    lines.append("=" * 50)
    lines.append("")
    news = [
        f'"{target}" site:abqjournal.com',
        f'"{target}" site:krqe.com OR site:koat.com OR site:kob.com',
        f'"{target}" accident OR crash OR injury OR "hit and run"',
        f'"{target}" obituary OR memorial OR "passed away"',
        f'"{target}" arrested OR charged OR sentenced',
    ]
    for d in news:
        encoded = d.replace(" ", "+").replace('"', '%22').replace("'", "%27")
        lines.append(f"  {d}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("CACHED & ARCHIVED")
    lines.append("=" * 50)
    lines.append("")
    lines.append("  Wayback Machine (archived pages):")
    lines.append(f"  https://web.archive.org/web/*/{target.replace(' ', '+')}")
    lines.append("")
    lines.append("  Google Cache search:")
    lines.append(f"  https://www.google.com/search?q=cache:{target.replace(' ', '+')}")
    lines.append("")
    lines.append("  Cached profiles / removed pages:")
    cached = [f'cache:"{target}" profile OR bio OR about']
    for d in cached:
        encoded = d.replace(" ", "+").replace('"', '%22').replace("'", "%27")
        lines.append(f"  {d}")
        lines.append(f"  https://www.google.com/search?q={encoded}")
        lines.append("")

    result = "\n".join(lines)
    emit(job_id, "module_done", {"module": "dorks", "result": result})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODULE REGISTRY
# sherlock is NOT listed standalone — runs inside username_search
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
    "breach_leak":       module_breach_leak,
    "plate_lookup":      module_plate_lookup,
    "geolocation":       module_geolocation,
    "business":          module_business,
    "whois":             module_whois,
    "dns":               module_dns,
    "nmap":              module_nmap,
    "geoip":             module_geoip,
    "shodan":            module_shodan,
    "virustotal":        module_virustotal,
    "dorks":             module_google_dorks,
}

DOMAIN_IP_MODULES = {"whois", "dns", "nmap", "geoip", "shodan", "virustotal"}
PERSON_ONLY_MODULES = {"people", "public_records", "property", "skip_trace",
                       "social_media", "social_footprint", "photo_forensics"}
# breach_leak is intentionally NOT in PERSON_ONLY_MODULES since it also
# serves EMAIL and USERNAME target types


def run_investigation(job_id, target, target_type, selected_modules, dob="", ssn="", oln="", extra=None):
    try:
        cutoff = time.time() - 3600
        stale = [jid for jid, j in list(jobs.items())
                 if datetime.fromisoformat(j["started"]).timestamp() < cutoff]
        for jid in stale:
            del jobs[jid]

        tt = (target_type or "PERSON").upper()

        if tt == "PERSON":
            selected_modules = [m for m in selected_modules if m not in DOMAIN_IP_MODULES]
        elif tt in ("DOMAIN", "IP"):
            selected_modules = [m for m in selected_modules if m not in PERSON_ONLY_MODULES]

        threads = []
        for mod_id in selected_modules:
            fn = MODULE_MAP.get(mod_id)
            if fn:
                t = threading.Thread(target=fn, args=(target, job_id, dob, ssn, oln),
                                     kwargs={"extra": extra}, daemon=True)
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
# Stateless auth: tokens are HMAC-SHA256 signed payloads (username, role, name,
# issued-at). No server-side session store — a token is valid on any instance
# as long as the signature verifies and it hasn't expired.
import secrets, hmac, hashlib, base64

SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    print("[AUTH][WARNING] SECRET_KEY env var is NOT set — using a randomly "
          "generated key. All tokens will be invalidated on every restart/"
          "redeploy and will NOT be shared across instances. Set SECRET_KEY in "
          "the environment (e.g. Render env vars) for stable auth.")
_SECRET_KEY_BYTES = SECRET_KEY.encode()

# How long a token stays valid (seconds). Override with TOKEN_TTL_SECONDS.
TOKEN_TTL_SECONDS = int(os.environ.get("TOKEN_TTL_SECONDS", 12 * 3600))


def _b64u_encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64u_decode(s):
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def make_token(username, role, name, issued_at=None):
    if issued_at is None:
        issued_at = int(time.time())
    payload = {"u": username, "r": role, "n": name, "iat": issued_at}
    payload_b64 = _b64u_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_SECRET_KEY_BYTES, payload_b64.encode(), hashlib.sha256).digest()
    return payload_b64 + "." + _b64u_encode(sig)


def verify_session_token(token):
    """Return a session dict {username, role, name, created} for a valid,
    unexpired, correctly-signed token; otherwise None."""
    if not token or "." not in token:
        return None
    payload_b64, _, sig_b64 = token.partition(".")
    try:
        expected = hmac.new(_SECRET_KEY_BYTES, payload_b64.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64u_decode(sig_b64), expected):
            return None
        payload = json.loads(_b64u_decode(payload_b64))
    except Exception:
        return None
    iat = payload.get("iat", 0)
    if not isinstance(iat, (int, float)) or (time.time() - iat) > TOKEN_TTL_SECONDS:
        return None
    return {
        "username": payload.get("u", ""),
        "role": payload.get("r", ""),
        "name": payload.get("n", ""),
        "created": iat,
    }


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

# ── Persistent Audit Log ────────────────────────────────────────────────────
# Primary: Supabase (persistent, SOC 2 compliant, survives deploys)
# Fallback: local file (used only if Supabase env vars are not set)
import urllib.request
import urllib.error

AUDIT_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fivet_audit_log.json")
AUDIT_LOG_MAX_ENTRIES = 5000
_audit_lock = threading.Lock()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


def _supabase_request(method, path, body=None, params=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url += f"?{qs}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal" if method == "POST" else "return=representation",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else []


def load_audit_log():
    if SUPABASE_ENABLED:
        try:
            entries = _supabase_request(
                "GET", "audit_log",
                params={"order": "timestamp.desc", "limit": AUDIT_LOG_MAX_ENTRIES}
            )
            # Normalize field name: Supabase uses 'timestamp', frontend expects it too
            return list(reversed(entries))  # oldest first, matches old file-based ordering
        except Exception as e:
            print(f"[AUDIT] Supabase read failed, falling back to file: {e}")
    # Fallback: local file
    try:
        if os.path.exists(AUDIT_LOG_FILE):
            with open(AUDIT_LOG_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def save_audit_log(log_list):
    # Only used by file fallback path
    try:
        with open(AUDIT_LOG_FILE, "w") as f:
            json.dump(log_list[-AUDIT_LOG_MAX_ENTRIES:], f)
    except Exception as e:
        print(f"[AUDIT WRITE ERROR] {e}")


def _write_audit_entry(entry):
    if SUPABASE_ENABLED:
        try:
            _supabase_request("POST", "audit_log", body=entry)
            return
        except Exception as e:
            print(f"[AUDIT] Supabase write failed, falling back to file: {e}")
    # Fallback: local file
    with _audit_lock:
        log_list = load_audit_log()
        log_list.append(entry)
        save_audit_log(log_list)


def append_audit_entry(entry):
    # Write on a background daemon thread so a slow or unreachable audit
    # backend never blocks the login/search request path.
    threading.Thread(target=_write_audit_entry, args=(entry,), daemon=True).start()


def log_auth_event(username, action, detail, ip="unknown", name="", target=""):
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "username": username,
        "name": name,
        "action": action,
        "detail": detail,
        "target": target,
        "ip": ip,
    }
    print(f"[AUTH] {action} | user={username} | {detail} | ip={ip} | time={entry['timestamp']}")
    append_audit_entry(entry)

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.json
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    users = get_users()
    user = users.get(username)
    if user and user["password"] == password:
        token = make_token(username, user["role"], user["name"])
        log_auth_event(username, "LOGIN_SUCCESS", f"User {user['name']} authenticated", ip, name=user["name"])
        return jsonify({"success": True, "token": token, "username": username,
                        "role": user["role"], "name": user["name"]})
    else:
        log_auth_event(username, "LOGIN_FAILED", "Invalid credentials", ip)
        return jsonify({"success": False, "error": "Invalid credentials"}), 401

@app.route("/api/auth/verify", methods=["POST"])
def verify_token():
    data = request.json
    token = data.get("token", "")
    session = verify_session_token(token)
    if session:
        return jsonify({"valid": True, "username": session["username"],
                        "role": session["role"], "name": session["name"]})
    return jsonify({"valid": False}), 401

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    data = request.json
    token = data.get("token", "")
    session = verify_session_token(token)
    if session:
        log_auth_event(session["username"], "LOGOUT", "User logged out", name=session.get("name",""))
    return jsonify({"success": True})

@app.route("/api/auth/audit", methods=["POST"])
def get_audit():
    data = request.json or {}
    token = data.get("token", "")
    session = verify_session_token(token)
    if not session or session.get("role") != "admin":
        return jsonify({"error": "Unauthorized — admin access required"}), 403
    log_list = load_audit_log()
    # Most recent first
    log_list = list(reversed(log_list))
    return jsonify({"success": True, "count": len(log_list), "entries": log_list})

@app.route("/api/investigate", methods=["GET", "POST"])
def investigate():
    if request.method == "POST":
        data = request.json or {}
    else:
        data = request.args
    target = data.get("target", "").strip()
    target_type = data.get("type", "PERSON")
    modules_param = data.get("modules", "")
    dob = data.get("dob", "").strip()
    ssn = data.get("ssn", "").strip()
    oln = data.get("oln", "").strip()
    city = data.get("city", "").strip()
    state = data.get("state", "").strip()
    token = data.get("token", "")

    # Optional enrichment fields (all optional; a name-only search still works).
    # Bundled into a single `extra` dict threaded through to the modules rather
    # than widening every module signature. Modules that don't use it ignore it.
    extra = {
        "first":    data.get("first", "").strip(),
        "middle":   data.get("middle", "").strip(),
        "paternal": data.get("paternal", "").strip(),
        "maternal": data.get("maternal", "").strip(),
        "phone":    data.get("phone", "").strip(),
        "email":    data.get("email", "").strip(),
        "username": data.get("username", "").strip(),
        "employer": data.get("employer", "").strip(),
        "spanish":  data.get("spanish", "") in ("1", "true", "True", "on", "yes"),
        "city":     city,
        "state":    state,
    }

    # If city/state are supplied separately, compose a canonical
    # "Name, City ST" string so parse_name_location's comma branch handles it.
    if target and "," not in target and (city or state):
        loc = " ".join(p for p in [city, state] if p).strip()
        if loc:
            target = f"{target}, {loc}"
    if isinstance(modules_param, str) and modules_param:
        selected_modules = modules_param.split(",")
    elif isinstance(modules_param, list):
        selected_modules = modules_param
    else:
        selected_modules = list(MODULE_MAP.keys())
    if not target:
        return jsonify({"error": "No target provided"}), 400

    # Require a valid token — investigations must be attributable to a user.
    session = verify_session_token(token)
    if not session:
        return jsonify({"error": "Authentication required"}), 401
    searcher_username = session["username"]
    searcher_name = session["name"]
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    log_auth_event(
        searcher_username, "SEARCH",
        f"Type={target_type} | Modules={len(selected_modules)} | DOB={'yes' if dob else 'no'} | SSN={'yes' if ssn else 'no'} | OLN={'yes' if oln else 'no'}",
        ip, name=searcher_name, target=target
    )

    job_id = "job_" + secrets.token_urlsafe(16)
    new_job(job_id, owner=searcher_username)
    threading.Thread(
        target=run_investigation,
        args=(job_id, target, target_type, selected_modules, dob, ssn, oln),
        kwargs={"extra": extra},
        daemon=True
    ).start()
    return jsonify({"job_id": job_id})

@app.route("/api/stream/<job_id>")
def stream(job_id):
    # If a token is supplied, it must belong to the user who created the job.
    # (Optional: when no token is supplied, the stream is not gated here.)
    token = request.args.get("token", "")
    if token:
        session = verify_session_token(token)
        job = jobs.get(job_id)
        owner = job.get("owner") if job else None
        if owner and owner != "unknown":
            if not session or session.get("username") != owner:
                return jsonify({"error": "Forbidden — token does not match job owner"}), 403

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
    tools = {t: tool_available(t) for t in ["whois", "dig"]}
    return jsonify({
        "status": "ok",
        "tools": tools,
        "audit_backend": "supabase" if SUPABASE_ENABLED else "local_file (not persistent across deploys)"
    })

@app.route("/")
def index():
    return "FIVE T OSINT Backend running. Connect your frontend."

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
