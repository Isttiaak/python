#!/usr/bin/env python3
"""Course enrollment on the Uttara University ERP, unattended (GitHub Actions)
or by hand.

Reads the MASTER sheet (one row per advisor: Employee ID, Acronym, CSV link,
Academic Year, Semester), stacks every advisor's student sheet into one roster
and enrolls each student in the term of the row that listed their sheet.
Students enrolled by an earlier run are skipped (erp_runs/enrollment_log.csv).

Settings (environment variables; on GitHub they come from repository secrets):
  MASTER_LINK    link to the master sheet                       (required)
  ERP_ID         ERP employee ID used to sign in                (required)
  ERP_PASSWORD   ERP password                                   (required)
  DRY_RUN        "true": search, pick section, preview — but don't click Enroll
  OUT_DIR        folder for the log, roster and screenshots     (default: erp_runs)

Exit code 1 means a person should look: a sheet link that couldn't be read, a
term that matches no ERP option, an enrollment error, or a failed sign-in.
"""
import asyncio, csv, io, os, re, sys
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import requests
from playwright.async_api import async_playwright

BASE_URL   = "https://erp.uttarauniversity.edu.bd"
LOGIN_URL  = f"{BASE_URL}/login"
ENROLL_URL = f"{BASE_URL}/acamgt/enroll/course"

VALID_ID_PATTERN       = re.compile(r"^\d{10}$")
CONTINUE_ANSWER        = "yes"
EXPECTED_ID_PREFIX     = ""      # e.g. "2251" -> flags anyone outside that cohort. "" = no check.
REQUIRE_SECTION_MATCH  = True    # True: skip a student whose section letter isn't on the page
DELAY_BETWEEN_STUDENTS = 2.0
PAGE_TIMEOUT_MS        = 30_000
MAX_ERRORS_IN_A_ROW    = 5       # stop the run: the ERP is down or its page changed

DRY_RUN   = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
OUT_DIR   = Path(os.environ.get("OUT_DIR") or "erp_runs"); OUT_DIR.mkdir(parents=True, exist_ok=True)
SHOTS_DIR = OUT_DIR / "screenshots"; SHOTS_DIR.mkdir(exist_ok=True)
LOG_PATH      = OUT_DIR / "enrollment_log.csv"
COMBINED_PATH = OUT_DIR / "combined_roster.csv"     # every student row from every sheet, stacked
LOG_FIELDS = ["timestamp", "student_id", "academic_year", "semester", "batch", "section",
              "advisor_emp_id", "advisor_acronym", "status", "detail"]

EMP_ID = PASSWORD = PAGE = None                     # set in main()

# Confirmed against the live page.
OVERRIDES = {
    "student_id":    'input[name="sid"]',
    "academic_year": 'select[name="acyear"]',   # select2 widget
    "semester":      'select[name="semid"]',    # select2 widget
}
FIELD_HINTS = {
    "student_id":    ["student id", "student_id", "studentid"],
    "academic_year": ["academic year", "acad_year", "session"],
    "semester":      ["semester", "term"],
}
BUTTON_HINTS = {
    "signin":     ["sign in", "signin", "log in", "login", "submit"],
    "by_student": ["by student", "student wise"],
    "search":     ["search", "find", "load", "go"],
    "preview":    ["preview"],
    "enroll":     ["enroll course", "enroll", "enrol", "save", "submit"],
    "confirm":    ["yes", "ok", "confirm", "proceed"],
}

def split_links(raw):
    """Every link in one cell. Copes with links separated by spaces, commas or
    new lines, and with links pasted back to back ('...sharinghttps://...').
    A link inside another link (google.com/url?q=https://...) stays whole."""
    raw = (raw or "").strip()
    links = re.findall(r"https?://.+?(?=(?<![=%])https?://|[\s,]|$)", raw)
    return links or [p.strip() for p in re.split(r"[,\n]+", raw) if p.strip()]

def csv_urls(link):
    """-> URLs to try for this link, best first. Raises ValueError for anything
    that is not a sheet link, Drive link, sheet ID, CSV URL or local file."""
    link = (link or "").strip().strip("\"'<>").rstrip(".,;)")
    m = re.search(r"[?&](?:q|url|continue)=(https?(?::|%3A)[^&\s]+)", link)
    if m and not re.search(r"/spreadsheets/|/d/", link.split("?")[0]):
        link = unquote(m.group(1))     # copied out of Gmail/Docs: google.com/url?q=<the real link>
    if re.search(r"docs\.google\.com/forms/|forms\.gle/", link):
        raise ValueError("this is the Google Form link, not its responses sheet "
                         "(Form -> Responses -> Link to Sheets, then share that sheet)")
    if re.search(r"docs\.google\.com/(document|presentation)/", link):
        raise ValueError("this is a Google Doc/Slides link, not a sheet")
    g   = re.search(r"[?&#]gid=(\d+)", link)
    gid = f"&gid={g.group(1)}" if g else ""

    m = re.search(r"/spreadsheets/(?:u/\d+/)?d/e/([A-Za-z0-9_-]+)", link)  # published to web
    if m:
        return [link.split("/pub")[0] + "/pub?output=csv" + gid]

    m = re.search(r"/spreadsheets/(?:u/\d+/)?d/([A-Za-z0-9_-]+)", link)    # normal share link
    if m:
        return [f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv{gid}"]

    m = (re.search(r"(?:drive|docs)\.google\.com/.*?/d/([A-Za-z0-9_-]{20,})", link)
         or re.search(r"(?:drive|docs)\.google\.com/.*?[?&]id=([A-Za-z0-9_-]{20,})", link))
    if m:                     # Drive link: a Google Sheet or an uploaded .csv — try both
        fid = m.group(1)
        return [f"https://docs.google.com/spreadsheets/d/{fid}/export?format=csv{gid}",
                f"https://drive.google.com/uc?export=download&id={fid}"]

    if link.lower().startswith("http"):                                 # direct CSV URL
        return [link]

    try: is_file = bool(link) and Path(link).exists()                   # local file
    except OSError: is_file = False                                     # (a long note isn't a path)
    if is_file:
        return [link]

    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", link):                       # bare sheet ID
        return [f"https://docs.google.com/spreadsheets/d/{link}/export?format=csv"]

    raise ValueError(f"not a sheet link, Drive link, sheet ID, CSV URL or file: {link[:120] or '(blank)'}")

def fetch_csv_text(urls):
    """Text of the first URL that gives back a CSV. Raises RuntimeError saying
    what to fix."""
    why = "nothing to read"
    for url in urls:
        if not url.lower().startswith("http"):
            return Path(url).read_text(encoding="utf-8-sig")
        try:
            r = requests.get(url, timeout=30)
        except Exception as e:
            why = f"download failed ({type(e).__name__}) — is Internet ON?"; continue
        head = r.content[:300].lstrip().lower()
        if r.status_code == 200 and r.content[:4] == b"PK\x03\x04":
            why = "it is an Excel file, not a CSV — open it in Google Sheets and share that sheet"
        elif r.status_code == 200 and not (head.startswith(b"<!doctype html") or b"<html" in head):
            return r.content.decode("utf-8-sig", errors="replace")
        else:
            why = "link is not readable as CSV — share it as 'Anyone with the link' (Viewer)"
    raise RuntimeError(why)

def read_csv_text(text):
    """-> (headers, rows). Completely blank rows are dropped; every row keeps its
    sheet row number under '_row' (row 1 is the header)."""
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for n, x in enumerate(reader, start=2):
        if any(isinstance(v, str) and v.strip() for v in x.values()):
            x["_row"] = n; rows.append(x)
    return reader.fieldnames or [], rows

def _norm(s): return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def find_column(headers, *cands):
    normed = {_norm(h): h for h in headers if _norm(h)}   # blank headers never match
    for c in cands:                                        # exact match wins
        if _norm(c) in normed: return normed[_norm(c)]
    for c in cands:                                        # then a header containing it
        cn = _norm(c)                                      # ('id' only as a whole word)
        for nh, orig in normed.items():
            if cn in nh and (len(cn) > 3 or re.search(rf"(?<![a-z]){cn}(?![a-z])", orig.lower())):
                return orig
    for c in cands:                                        # then a header inside it ('Sec')
        for nh, orig in normed.items():
            if len(nh) > 2 and nh in _norm(c): return orig
    return None

def clean_id(x):
    """'2251 081 001', '2251-081-001', '2251081001.0' -> '2251081001'."""
    return re.sub(r"\.0+$", "", re.sub(r"[\s-]", "", str(x or "")))

def section_letter(text):
    """'A', 'b', 'Section A', 'Sec-B', 'PC-A', 'A (Day)' -> the letter.
    '' when there isn't exactly one stand-alone letter ('AB', 'A-B', '61')."""
    found = re.findall(r"(?<![A-Za-z])[A-Za-z](?![A-Za-z])", text or "")
    return found[0].upper() if len(found) == 1 else ""

def same_term(a, b):
    return (_norm(a["year"]), _norm(a["semester"])) == (_norm(b["year"]), _norm(b["semester"]))

def load_master(text):
    headers, rows = read_csv_text(text)
    cols = {
        "emp_id":   find_column(headers, "insert your employee id", "employee id", "emp id"),
        "acronym":  find_column(headers, "insert your acronym", "acronym"),
        "email":    find_column(headers, "email address", "email"),
        "link":     find_column(headers, "insert the csv link", "csv link", "sheet link", "link"),
        "year":     find_column(headers, "academic year", "acyear"),
        "semester": find_column(headers, "semester"),
    }
    missing = [k for k in ("link", "year", "semester") if not cols[k]]
    if missing:
        raise SystemExit(f"Master sheet has no {'/'.join(missing)} column. Headers: {headers}")
    print("Master headers :", [h for h in headers if h.strip()])
    print("Mapped         :", {k: v for k, v in cols.items() if v})
    print(f"Master rows    : {len(rows)}")
    return rows, cols

def build_sources(rows, cols):
    """One source per CSV link in the master. A link submitted more than once is
    read once, with the term of its latest submission."""
    by_url, bad = {}, []
    for row in rows:
        get = lambda k: (row.get(cols[k]) or "").strip() if cols[k] else ""
        who = {"master_row": row["_row"], "emp_id": get("emp_id"),
               "acronym": get("acronym").upper(), "email": get("email"),
               "year": get("year"), "semester": get("semester")}
        who["tag"] = who["acronym"] or who["emp_id"] or who["email"] or f"master row {row['_row']}"
        links = split_links(get("link"))
        if not links:
            bad.append({**who, "link": "", "why": "no CSV link"}); continue
        if not who["year"] or not who["semester"]:
            keys = []
            for link in links:
                try: keys.append(csv_urls(link)[0])
                except ValueError: pass
            bad.append({**who, "link": " ".join(links), "keys": keys,
                        "why": "missing Academic Year or Semester"}); continue
        for link in links:
            try:
                urls = csv_urls(link)
            except ValueError as e:
                bad.append({**who, "link": link, "not_sheet": True, "why": str(e)}); continue
            src, prev = {**who, "link": link, "urls": urls, "note": ""}, by_url.get(urls[0])
            if prev:
                src["note"] = f"same link also in master row {prev['master_row']}"
                if not same_term(prev, src):
                    src["note"] += f" with a DIFFERENT term ({prev['year']} / {prev['semester']})"
                src["note"] += " — using this later row"
            by_url[urls[0]] = src
    return list(by_url.values()), bad

STUDENT_COLUMNS = {
    "id":       ("student id", "studentid", "students id", "id"),
    "continue": ("do you want to continue", "continue"),
    "batch":    ("batch",),
    "section":  ("section",),
    "email":    ("email",),
}

def load_students(sources):
    """Read every advisor's sheet and stack the rows into one list. Each row
    carries the advisor and term of the master row that listed its sheet."""
    sheets, failed = {}, []
    for n, src in enumerate(sources, 1):
        print(f"  [{n}/{len(sources)}] {src['tag']} (master row {src['master_row']}): ", end="")
        try:
            text = fetch_csv_text(src["urls"])
            headers, rows = read_csv_text(text)
            cols = {k: find_column(headers, *c) for k, c in STUDENT_COLUMNS.items()}
            if not cols["id"]:
                raise RuntimeError(f"no Student ID column (headers: {[h for h in headers if h.strip()]})")
        except Exception as e:
            print(f"FAILED — {e}"); failed.append({**src, "why": str(e)}); continue
        gaps = [k for k, v in cols.items() if not v]
        print(f"{len(rows)} rows" + (f"  (no column for: {', '.join(gaps)})" if gaps else ""))
        # The same sheet pasted as two different links (share-dialog copy vs the
        # address bar) downloads the same text: read it once, with the later row's term.
        key = text if rows else id(src)
        if key in sheets:
            old, src = sorted((sheets[key][0], src), key=lambda s: s["master_row"])
            src["note"] = "; ".join(filter(None, [src["note"],
                f"same sheet as master row {old['master_row']} — using this later row"]))
        sheets[key] = (src, rows, cols)

    records = []
    for src, rows, cols in sheets.values():
        src["count"] = len(rows)
        for row in rows:
            get = lambda k: (row.get(cols[k]) or "").strip() if cols[k] else ""
            records.append({"source": src, "row": row["_row"], "id": clean_id(get("id")),
                            "email": get("email"), "batch": get("batch"), "section": get("section"),
                            "continue": get("continue").lower() if cols["continue"] else None})
    return records, failed

def explain_not_read(not_read, sources):
    """Split the master entries that weren't read into (students missing, nothing
    lost): a Form link next to the real sheet link, or a resubmission without a
    term whose sheet was read from another row, costs no students."""
    read = [s for s in sources if "count" in s]
    missing, ignored = [], []
    for b in not_read:
        same_sheet = [s for s in read if s["urls"][0] in b.get("keys", ())]
        same_row   = [s for s in read if s["master_row"] == b["master_row"]]
        if same_sheet:
            b["covered"] = f"that sheet was read from master row {same_sheet[0]['master_row']}"
        elif same_row and b.get("not_sheet"):
            b["covered"] = "the sheet link in the same row was read"
        (ignored if b.get("covered") else missing).append(b)
    return missing, ignored

def audit_roster(records):
    accepted, rejected, seen = [], [], {}
    def reject(r, why): r["why"] = why; rejected.append(r)
    for r in records:
        sid, src, cont = r["id"], r["source"], r["continue"]
        sect = section_letter(r["section"])

        if cont is not None and not cont.startswith(CONTINUE_ANSWER):
            reject(r, f"answered '{cont or 'blank'}'"); continue
        if not VALID_ID_PATTERN.match(sid):
            reject(r, "malformed student ID"); continue
        if not r["batch"] or not r["section"]:
            reject(r, "missing batch or section"); continue
        if not sect:
            reject(r, f"section '{r['section']}' is not one letter"); continue

        p = seen.get(sid)
        if p and not same_term(p, src) and src["master_row"] > p["source"]["master_row"]:
            # A later master row lists this student under another term (a reused
            # master sheet): the later one wins, the earlier entry is dropped.
            accepted.remove(p); del seen[sid]
            reject(p["record"], f"moved to {src['year']} / {src['semester']} by "
                                f"{src['tag']}'s sheet (master row {src['master_row']})")
            p = None
        if p:
            note = "duplicate submission" + (
                f" (first at row {p['row']})" if p["source"] is src
                else f" (already in {p['source']['tag']}'s sheet, row {p['row']})")
            if p["email"] != r["email"]:
                note += f" | DIFFERENT EMAIL (first: {p['email']}, now: {r['email']})"
            if p["batch"] != r["batch"] or p["section"] != sect:
                note += f" | CONFLICTING SECTION ({p['batch']} {p['section']} vs {r['batch']} {sect})"
            if not same_term(p, src):
                note += (f" | DIFFERENT TERM ({p['year']} / {p['semester']} from the later master row"
                         f" {p['source']['master_row']} kept, not {src['year']} / {src['semester']})")
            reject(r, note); continue

        rec = {"row": r["row"], "id": sid, "email": r["email"], "batch": r["batch"], "section": sect,
               "year": src["year"], "semester": src["semester"],
               "emp_id": src["emp_id"], "acronym": src["acronym"], "source": src, "record": r}
        seen[sid] = rec; accepted.append(rec)
    return accepted, rejected

def write_combined(records, path=COMBINED_PATH):
    """All student rows from all sheets in one CSV, with what the audit decided
    (before login — the enrollment result per student is in enrollment_log.csv)."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["advisor_acronym", "advisor_emp_id", "academic_year", "semester", "sheet_row",
                    "student_id", "email", "batch", "section", "continue", "audit", "sheet_link"])
        for r in records:
            s = r["source"]
            w.writerow([s["acronym"], s["emp_id"], s["year"], s["semester"], r["row"], r["id"],
                        r["email"], r["batch"], r["section"], r["continue"] or "",
                        f"skipped: {r['why']}" if "why" in r else "accepted", s["link"]])

def print_audit(sources, not_read, ignored, accepted, rejected):
    line = "=" * 74
    print(line); print("ROSTER AUDIT"); print(line)
    read = [s for s in sources if "count" in s]
    print(f"\nSHEETS READ ({len(read)}):")
    for s in read:
        print(f"  {s['tag']:<12} master row {s['master_row']:<4} {s['year']} / {s['semester']}"
              f"  -> {s['count']} rows" + (f"\n  {'':<12} NOTE: {s['note']}" if s["note"] else ""))
    if not_read:
        print(f"\nNOT READ — THESE ADVISORS' STUDENTS ARE NOT INCLUDED ({len(not_read)}):")
        for s in not_read:
            print(f"  {s['tag']:<12} master row {s['master_row']:<4} {s['why']}")
            if s["link"]: print(f"  {'':<12} {s['link'][:150]}")
    if ignored:
        print(f"\nIGNORED — NO STUDENTS LOST ({len(ignored)}):")
        for s in ignored:
            print(f"  {s['tag']:<12} master row {s['master_row']:<4} {s['why']} — but {s['covered']}")
    if rejected:
        print(f"\nSKIPPED ({len(rejected)}):")
        for r in rejected:
            print(f"  {r['source']['tag']:<8} row {r['row']:>3}  {r['id'] or '(blank)':<12} "
                  f"{r['email']:<34} {r['why']}")
    print(f"\nWILL ENROLL ({len(accepted)}):")
    by = {}
    for r in accepted: by.setdefault((r["year"], r["semester"]), {}).setdefault(
        f"{r['batch']} {r['section']}", []).append(r)
    for (year, sem), secs in by.items():
        print(f"\n  {year} / {sem}  ({sum(map(len, secs.values()))} students)")
        for sec, sts in sorted(secs.items()):
            who = ", ".join(dict.fromkeys(s["source"]["tag"] for s in sts))
            print(f"\n    Section {sec}  ({len(sts)} students, from {who})")
            for s in sts: print(f"      {s['id']}  {s['email']}")

    print("\nMANUAL CHECK:")
    flagged = False
    terms = dict.fromkeys((_norm(r["year"]), _norm(r["semester"])) for r in accepted)
    if len(terms) > 1:
        print(f"  {len(terms)} different terms in one run (see WILL ENROLL) — make sure the "
              "master sheet doesn't still hold an old term's rows")
        flagged = True
    for r in accepted:
        if EXPECTED_ID_PREFIX and not r["id"].startswith(EXPECTED_ID_PREFIX):
            print(f"  {r['id']} (prefix {r['id'][:4]}) in batch {r['batch']} — different cohort")
            flagged = True
        if r["email"].endswith("uttarauniversity.edu.bd"):
            print(f"  {r['id']} submitted from a STAFF email ({r['email']})")
            flagged = True
    if not flagged: print("  (nothing flagged)")
    print(line)

async def shot(page, name="page", width=1000):
    p = SHOTS_DIR / f"{name}.png"
    await page.screenshot(path=str(p), full_page=True)
    print(f"  screenshot: {p}")
    return p

async def body_text(page):
    return (await page.inner_text("body")).lower()

def only_new(before, after):
    """Text present after an action but not before — avoids matching words that
    were already sitting in the nav bar or the page title."""
    old = set(before.splitlines())
    return "\n".join(l for l in after.splitlines() if l not in old)

async def find_control(page, key, tags=("select", "input", "textarea")):
    if key in OVERRIDES:
        loc = page.locator(OVERRIDES[key])
        if await loc.count():
            return loc.first
    for h in FIELD_HINTS.get(key, [key]):
        try:
            loc = page.get_by_label(re.compile(re.escape(h), re.I))
            if await loc.count() and await loc.first.is_visible():
                return loc.first
        except Exception: pass
    for h in FIELD_HINTS.get(key, [key]):
        for attr in ("name", "id", "placeholder"):
            for tag in tags:
                try:
                    loc = page.locator(f'{tag}[{attr}*="{h}" i]')
                    for i in range(min(await loc.count(), 5)):
                        if await loc.nth(i).is_visible():
                            return loc.nth(i)
                except Exception: continue
    return None

async def find_button(page, key):
    for word in BUTTON_HINTS.get(key, [key]):
        for role in ("button", "link", "tab", "radio"):
            try:
                loc = page.get_by_role(role, name=re.compile(word, re.I))
                for i in range(min(await loc.count(), 5)):
                    if await loc.nth(i).is_visible() and await loc.nth(i).is_enabled():
                        return loc.nth(i)
            except Exception: pass
        for sel in (f'input[type="submit"][value*="{word}" i]', f':text-is("{word}")'):
            try:
                loc = page.locator(sel)
                if await loc.count() and await loc.first.is_visible():
                    return loc.first
            except Exception: pass
    return None

async def click_section_letter(page, letter):
    """Click the one-click A/B/C letter button. Returns False if that letter is
    not offered — the page is left untouched."""
    letter = str(letter or "").strip().upper()[:1]
    if not letter: return False
    for role in ("button", "link", "radio", "option"):
        try:
            loc = page.get_by_role(role, name=re.compile(rf"^{re.escape(letter)}$"))
            for i in range(min(await loc.count(), 5)):
                if await loc.nth(i).is_visible() and await loc.nth(i).is_enabled():
                    await loc.nth(i).click(); return True
        except Exception: continue
    try:
        loc = page.locator(f':text-is("{letter}")')
        for i in range(min(await loc.count(), 5)):
            if await loc.nth(i).is_visible():
                await loc.nth(i).click(); return True
    except Exception: pass
    return False

async def read_options(control):
    if control is None: return []
    try:
        if (await control.evaluate("el => el.tagName.toLowerCase()")) != "select":
            return []
        return await control.evaluate(
            "el => Array.from(el.options).map(o => o.textContent.trim())"
            ".filter(t => t && !/^(select|choose|--)/i.test(t))")
    except Exception:
        return []

async def _select2_pick(page, control, target_text, name="field"):
    """select2 hides the real <select>, so select_option() fails actionability.
    Open the popup and click the option instead."""
    sel_id = await control.get_attribute("id")
    container = page.locator(f"#select2-{sel_id}-container") if sel_id else None
    if container is None or await container.count() == 0:
        container = control.locator("xpath=following-sibling::*[contains(@class,'select2')][1]")
    if await container.count() == 0:
        raise RuntimeError(f"No select2 widget found for '{name}'.")
    await container.first.click()
    await page.wait_for_timeout(250)
    option = page.get_by_role("option", name=re.compile(re.escape(target_text), re.I))
    if await option.count() == 0:
        option = page.locator(".select2-results__option", has_text=target_text)
    if await option.count() == 0:
        raise RuntimeError(f"select2 popup for '{name}' has no option '{target_text}'.")
    await option.first.click()

async def set_value(page, control, value, name="field"):
    if control is None:
        raise RuntimeError(f"Could not locate the '{name}' control.")
    if (await control.evaluate("el => el.tagName.toLowerCase()")) != "select":
        await control.fill(""); await control.fill(str(value)); return

    opts = await control.evaluate(
        "el => Array.from(el.options).map(o => ({v:o.value, t:o.textContent.trim()}))")
    want, target = _norm(value), None
    for o in opts:
        if _norm(o["t"]) == want or _norm(o["v"]) == want: target = o; break
    if target is None:
        for o in opts:
            if want and (want in _norm(o["t"]) or _norm(o["t"]) in want): target = o; break
    if target is None:
        raise RuntimeError(f"'{value}' is not an option for '{name}'. Available: {[o['t'] for o in opts]}")
    try:
        await control.select_option(value=target["v"], force=True)
    except Exception:
        await _select2_pick(page, control, target["t"], name)

def append_log(student, status, detail=""):
    new = not LOG_PATH.exists()
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new: w.writerow(LOG_FIELDS)
        w.writerow([datetime.now().isoformat(timespec="seconds"), student["id"],
                    student["year"], student["semester"], student["batch"], student["section"],
                    student["emp_id"], student["acronym"], status, detail])

def retire_old_log():
    """Logs from before the master sheet had no year/semester columns. Move such
    a log aside rather than mixing two formats in one file."""
    if not LOG_PATH.exists(): return
    with open(LOG_PATH, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f), [])
    if header != LOG_FIELDS:
        old = LOG_PATH.with_name(f"enrollment_log_old_{datetime.now():%Y%m%d_%H%M%S}.csv")
        LOG_PATH.rename(old)
        print(f"Old-format log moved to {old.name} — its students are not treated as done.")

def load_completed():
    """(student ID, academic year, semester) of every enrollment already done, so
    a rerun skips them — but the same student in a new term is still enrolled."""
    if not LOG_PATH.exists(): return set()
    with open(LOG_PATH, newline="", encoding="utf-8") as f:
        return {(r["student_id"], r["academic_year"], r["semester"]) for r in csv.DictReader(f)
                if r.get("status") in ("enrolled", "already")}

def match_option(value, opts):
    """ERP dropdown option for a master-sheet value -> (option, why_not).
    Exact text first, then the same words in any order ('Summer 2026' =
    '2026 Summer'), then one partial match. Never guesses between two options."""
    want = _norm(value)
    if not want: return None, "blank"
    words = lambda t: sorted(re.findall(r"[a-z]+|\d+", t.lower()))
    for hits in ([o for o in opts if _norm(o) == want],
                 [o for o in opts if words(o) == words(value)]):
        if len(hits) == 1: return hits[0], ""
    part = [o for o in opts if _norm(o) and (want in _norm(o) or _norm(o) in want)]
    if len(part) == 1: return part[0], ""
    return None, (f"could be any of {part}" if part else "no ERP option looks like it")

def ask_option(kind, value, opts, why):
    """Ask which ERP option a master-sheet value means. None = skip those students."""
    print(f"\nMaster sheet {kind} '{value}': {why}.")
    for i, o in enumerate(opts, 1): print(f"  {i}. {o}")
    if not sys.stdin.isatty():                 # unattended run (GitHub Actions): nobody to answer
        print("  Nobody to ask in an unattended run, so these students are skipped. Make the master\n"
              "  form's answer match one of the options above exactly.")
        return None
    print("  0. skip the students with this value")
    while True:
        try:
            c = input(f"Which ERP {kind} is '{value}'? (0-{len(opts)}): ").strip()
        except Exception:                      # no stdin, e.g. Kaggle "Save & Run All"
            print("  no input available — skipping them"); return None
        if c == "0": return None
        if c.isdigit() and 1 <= int(c) <= len(opts): return opts[int(c) - 1]
        print("  invalid choice")

def resolve_terms(students, year_opts, sem_opts):
    """Swap each student's master-sheet year/semester for the exact ERP option
    text. Students whose term can't be matched are returned separately."""
    found = {}
    for kind, key, opts in (("academic year", "year", year_opts), ("semester", "semester", sem_opts)):
        for value in dict.fromkeys(s[key] for s in students):
            opt, why = match_option(value, opts)
            found[key, value] = opt or ask_option(kind, value, opts, why)
            print(f"  {kind:<13} '{value}' -> {found[key, value] or 'NOT MATCHED — skipped'}")
    kept, dropped = [], []
    for s in students:
        y, m = found["year", s["year"]], found["semester", s["semester"]]
        (kept if y and m else dropped).append({**s, "year": y or s["year"], "semester": m or s["semester"]})
    return kept, dropped

async def login():
    await PAGE.goto(LOGIN_URL)
    await PAGE.locator('input:not([type="password"]):not([type="hidden"])').first.fill(EMP_ID)
    await PAGE.locator('input[type="password"]').first.fill(PASSWORD)
    btn = await find_button(PAGE, "signin")
    await (btn or PAGE.locator('button[type="submit"], input[type="submit"]').first).click()
    await PAGE.wait_for_load_state("networkidle")
    if "/login" in PAGE.url:
        await shot(PAGE, "login_failed")
        raise SystemExit("LOGIN FAILED — wrong credentials, a captcha, or the ERP blocks this server's IP.")

async def enroll_one(student, dry_run=True):
    await PAGE.goto(ENROLL_URL)
    await PAGE.wait_for_load_state("networkidle")
    if "/login" in PAGE.url:                  # the ERP session ran out mid-run: sign in again
        print("  session expired — signing in again")
        await login()
        await PAGE.goto(ENROLL_URL); await PAGE.wait_for_load_state("networkidle")

    # "By Student" is the default tab, so only click it if the ID field is missing.
    sid = await find_control(PAGE, "student_id", tags=("input",))
    if sid is None:
        url_before = PAGE.url
        by_student = await find_button(PAGE, "by_student")
        if by_student is not None:
            try:
                await by_student.click()
                await PAGE.wait_for_load_state("networkidle")
                await PAGE.wait_for_timeout(400)
            except Exception: pass
        sid = await find_control(PAGE, "student_id", tags=("input",))
        if sid is None and PAGE.url != url_before:
            await PAGE.goto(ENROLL_URL); await PAGE.wait_for_load_state("networkidle")
            sid = await find_control(PAGE, "student_id", tags=("input",))
    if sid is None:
        raise RuntimeError(f"No Student ID input at {PAGE.url}. Check OVERRIDES['student_id'].")

    await set_value(PAGE, sid, student["id"], "student ID")
    yc = await find_control(PAGE, "academic_year")
    if yc is not None: await set_value(PAGE, yc, student["year"], "academic year")
    sc = await find_control(PAGE, "semester")
    if sc is not None: await set_value(PAGE, sc, student["semester"], "semester")
    await PAGE.wait_for_timeout(700)

    search = await find_button(PAGE, "search")
    if search is not None: await search.click()
    else: await sid.press("Enter")
    await PAGE.wait_for_load_state("networkidle"); await PAGE.wait_for_timeout(600)

    body = await body_text(PAGE)
    if "already enrolled" in body or "already exists" in body: return "already"
    if "no record" in body or "not found" in body or "no student" in body: return "not_found"

    if student.get("section"):
        if not await click_section_letter(PAGE, student["section"]):
            if REQUIRE_SECTION_MATCH:
                return "no_section"
            print(f"  note: section '{student['section']}' not offered — left unchanged")
        await PAGE.wait_for_timeout(400)

    preview_btn = await find_button(PAGE, "preview")
    if preview_btn is not None:
        await preview_btn.click()
        await PAGE.wait_for_load_state("networkidle"); await PAGE.wait_for_timeout(600)

    enroll_btn = await find_button(PAGE, "enroll")
    if enroll_btn is None:
        raise RuntimeError("Student loaded but no Enroll Course button found.")
    if dry_run:
        return "dry_run"

    before = await body_text(PAGE)
    await enroll_btn.click()
    conf = await find_button(PAGE, "confirm")
    if conf is not None:
        try: await conf.click()
        except Exception: pass
    await PAGE.wait_for_load_state("networkidle"); await PAGE.wait_for_timeout(600)

    fresh = only_new(before, await body_text(PAGE)) or await body_text(PAGE)
    if "already" in fresh: return "already"
    if any(k in fresh for k in ("success", "enrolled successfully", "saved")): return "enrolled"
    raise RuntimeError("Clicked Enroll Course but saw no confirmation — verify by hand.")

def write_summary(lines):
    """Counts only (no student data) on the GitHub run page."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

async def main():
    global EMP_ID, PASSWORD, PAGE
    master_link = os.environ.get("MASTER_LINK", "").strip()
    EMP_ID      = os.environ.get("ERP_ID", "").strip()
    PASSWORD    = os.environ.get("ERP_PASSWORD", "")
    missing = [k for k, v in (("MASTER_LINK", master_link), ("ERP_ID", EMP_ID), ("ERP_PASSWORD", PASSWORD)) if not v]
    if missing:
        raise SystemExit(f"Missing setting(s): {', '.join(missing)} — add them as repository secrets.")
    print(f"{'DRY RUN — nothing will be enrolled' if DRY_RUN else 'LIVE RUN'}  ({datetime.now():%Y-%m-%d %H:%M})\n")

    try:
        master_text = fetch_csv_text(csv_urls(master_link))
    except (ValueError, RuntimeError, OSError) as e:
        raise SystemExit(f"Master sheet: {e}")
    master_rows, master_cols = load_master(master_text)
    sources, bad_rows = build_sources(master_rows, master_cols)
    print(f"\nReading {len(sources)} student sheet(s):")
    records, failed = load_students(sources)
    not_read, ignored = explain_not_read(bad_rows + failed, sources)
    accepted, rejected = audit_roster(records)
    write_combined(records)
    print(f"\nCombined roster ({len(records)} rows from {sum('count' in s for s in sources)} sheets): {COMBINED_PATH}\n")
    print_audit(sources, not_read, ignored, accepted, rejected)

    retire_old_log()
    done = load_completed()
    counts, term_skipped, todo, stopped = {}, [], [], False
    if accepted:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
            PAGE = await (await browser.new_context(viewport={"width": 1440, "height": 900})).new_page()
            PAGE.set_default_timeout(PAGE_TIMEOUT_MS)
            print(f"Signing in as {EMP_ID}")
            await login()
            await PAGE.goto(ENROLL_URL)
            await PAGE.wait_for_load_state("networkidle")
            year_opts = await read_options(await find_control(PAGE, "academic_year"))
            sem_opts  = await read_options(await find_control(PAGE, "semester"))
            if not year_opts or not sem_opts:
                await shot(PAGE, "enroll_page")
                raise SystemExit("Could not read the year/semester dropdowns — see screenshots/enroll_page.png.")

            # Academic Year / Semester come from the master sheet, matched to the ERP's own options.
            print("\nTerms from the master sheet:")
            students, term_skipped = resolve_terms(accepted, year_opts, sem_opts)
            if term_skipped:
                print(f"\n{len(term_skipped)} student(s) skipped — their Academic Year / Semester is not in the ERP:")
                for s in term_skipped: print(f"  {s['id']}  ({s['acronym'] or s['emp_id']}: {s['year']} / {s['semester']})")

            todo = [s for s in students if (s["id"], s["year"], s["semester"]) not in done]
            print(f"\n{len(students) - len(todo)} already done, {len(todo)} to go.\n")
            in_a_row = 0
            for n, s in enumerate(todo, 1):
                label = (f"[{n}/{len(todo)}] {s['id']} -> {s['batch']} {s['section']} "
                         f"({s['acronym'] or s['emp_id']}, {s['year']} / {s['semester']})")
                try:
                    st = await enroll_one(s, dry_run=DRY_RUN)
                    counts[st] = counts.get(st, 0) + 1; in_a_row = 0
                    print(f"{label}  {st.upper()}")
                    append_log(s, st)
                except Exception as e:
                    counts["error"] = counts.get("error", 0) + 1; in_a_row += 1
                    print(f"{label}  ERROR: {e}")
                    try: await PAGE.screenshot(path=str(SHOTS_DIR / f"fail_{s['id']}.png"), full_page=True)
                    except Exception: pass
                    append_log(s, "error", str(e)[:200])
                    if in_a_row >= MAX_ERRORS_IN_A_ROW:
                        stopped = True
                        print(f"\nSTOPPED after {in_a_row} errors in a row — the ERP is down or its page "
                              "changed. The rest are tried on the next run.")
                        break
                await asyncio.sleep(DELAY_BETWEEN_STUDENTS)
            await browser.close()

    print("\n" + "=" * 74)
    print("  ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "nothing processed")
    if not_read:
        print(f"NOT INCLUDED: {len(not_read)} sheet link(s) could not be read — "
              + ", ".join(f"{s['tag']} (master row {s['master_row']})" for s in not_read))
    if term_skipped:
        print(f"NOT INCLUDED: {len(term_skipped)} student(s) whose Academic Year / Semester is not in the ERP")
    print(f"Log: {LOG_PATH}")

    write_summary([
        f"### Enrollment {'(dry run) ' if DRY_RUN else ''}— {datetime.now():%Y-%m-%d %H:%M} UTC", "",
        "| | |", "|---|---|",
        f"| Students accepted from the sheets | {len(accepted)} |",
        f"| Done in earlier runs | {len(accepted) - len(term_skipped) - len(todo)} |",
        *(f"| {k} | {v} |" for k, v in sorted(counts.items())),
        f"| Sheet links not read | {len(not_read)} {', '.join(s['tag'] for s in not_read)} |",
        f"| Students with an unmatched term | {len(term_skipped)} |",
        *(["", "**Stopped early: too many errors in a row.**"] if stopped else []),
    ])
    return 1 if (counts.get("error") or not_read or term_skipped or stopped) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
