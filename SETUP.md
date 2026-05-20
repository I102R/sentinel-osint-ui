# SENTINEL OSINT Agent — Setup Guide

## What You're Building
- **Backend** (Replit): Python server running real OSINT tools
- **Frontend** (GitHub Pages or anywhere): The dashboard you open in any browser

---

## STEP 1 — Set Up the Backend on Replit

1. Go to https://replit.com and log in
2. Click **+ Create Repl**
3. Choose **Python** template
4. Name it: `sentinel-osint`
5. Delete the default `main.py` content
6. **Paste the contents of `backend/main.py`** into `main.py`
7. Create a new file called `requirements.txt` and paste the contents of `backend/requirements.txt`
8. Paste the contents of `backend/.replit` into the `.replit` file

### Install system tools (in Replit Shell tab):
```bash
pip install flask flask-cors requests
pip install theHarvester sherlock-project shodan
```

### Add API Keys (optional but recommended):
In Replit, click the **Secrets** tab (padlock icon) and add:
- `SHODAN_API_KEY`  → get free key at https://shodan.io
- `VT_API_KEY`      → get free key at https://virustotal.com
- `HIBP_API_KEY`    → get key at https://haveibeenpwned.com/API/Key

### Run it:
Click the green **Run** button. You'll see:
```
* Running on http://0.0.0.0:8080
```
Copy your Replit URL — it looks like:
`https://sentinel-osint.YOURNAME.replit.app`

Test it by visiting: `https://sentinel-osint.YOURNAME.replit.app/api/health`
You should see a JSON response showing which tools are installed.

---

## STEP 2 — Host the Frontend on GitHub Pages

1. Go to https://github.com and log in
2. Click **+ New repository**
3. Name it: `sentinel-osint-ui`
4. Set it to **Public**
5. Click **Create repository**
6. Upload `frontend/index.html` to the repo
7. Go to **Settings → Pages**
8. Under Source, select **main branch / root**
9. Click Save

Your dashboard will be live at:
`https://YOURUSERNAME.github.io/sentinel-osint-ui`

---

## STEP 3 — Connect Frontend to Backend

1. Open your GitHub Pages URL in any browser
2. In the **BACKEND URL** box at the bottom of the screen, paste your Replit URL:
   `https://sentinel-osint.YOURNAME.replit.app`
3. Click **TEST** — it should say "CONNECTED"
4. Your URL is saved in the browser automatically

---

## STEP 4 — Run Your First Investigation

1. Select your **Target Type** (Domain, IP, Person, Email, etc.)
2. Type the target in the search box
3. Hit **INVESTIGATE**
4. All 13 modules fire simultaneously and stream results back live

---

## Modules Reference

| Module         | What it does                                      | Requires         |
|----------------|---------------------------------------------------|------------------|
| WHOIS          | Domain/IP registration info                       | Nothing          |
| DNS Records    | A, MX, NS, TXT, CNAME records                    | Nothing          |
| Subdomain Enum | Certificate transparency via crt.sh              | Nothing          |
| Port Scan      | Top 20 ports via Nmap                            | nmap installed   |
| GeoIP          | Country, city, ISP for IP addresses              | Nothing          |
| theHarvester   | Emails, subdomains from Google/Bing/DuckDuckGo   | pip install      |
| Sherlock       | Username search across 300+ platforms            | pip install      |
| Shodan         | Internet-exposed devices and services            | Free API key     |
| VirusTotal     | Malware/reputation check for domains/IPs         | Free API key     |
| EmailRep       | Email reputation, breach indicators              | Nothing          |
| HaveIBeenPwned | Check email in known data breaches               | Paid API key     |
| Public Metadata| DuckDuckGo instant answers                       | Nothing          |
| Google Dorks   | Pre-built search queries for manual pivoting     | Nothing          |

---

## Keeping Replit Alive (Free Tier)

Free Replit instances sleep after inactivity. To keep yours awake:
- Use https://uptimerobot.com (free) to ping your `/api/health` endpoint every 5 minutes
- Or upgrade to Replit's Hacker plan ($7/mo) for always-on deployments

---

## Troubleshooting

**"Cannot reach backend"** — Make sure your Replit is running (hit the Run button)
**"nmap not found"** — Run `apt-get install nmap` in the Replit Shell
**"theHarvester error"** — Run `pip install theHarvester` in the Replit Shell
**CORS errors** — Already handled by flask-cors in the backend
**Modules timing out** — Normal for slow targets; results still stream in as they finish
