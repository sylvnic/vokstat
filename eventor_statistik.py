#!/usr/bin/env python3
"""
Hämtar klubbens resultat från Eventor (Svenska Orienteringsförbundet) och
räknar antal starter och antal unika deltagare per månad.

Skapar två filer som kan läggas ut på hemsidan:
  statistik.json  - rådata för statistiken
  statistik.html  - färdig, fristående tabell med staplar

Användning:
  export EVENTOR_API_KEY="din-nyckel"
  python eventor_statistik.py --from-year 2025
  python eventor_statistik.py --from-year 2025 --debug   # sparar rå-XML i ./raw

Kräver: pip install requests
"""
import argparse
import calendar
import json
import os
import sys
from datetime import date
from xml.etree import ElementTree as ET

import requests

BASE_URL = "https://eventor.orientering.se/api/"
MONTHS_SV = ["jan", "feb", "mar", "apr", "maj", "jun",
             "jul", "aug", "sep", "okt", "nov", "dec"]


def local(tag):
    """Tagg utan XML-namnrymd."""
    return tag.rsplit("}", 1)[-1]


def find_child(el, name):
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def find_text(el, *path):
    for name in path:
        if el is None:
            return None
        el = find_child(el, name)
    return el.text.strip() if el is not None and el.text else None


def get(session, endpoint, **params):
    r = session.get(BASE_URL + endpoint, params=params, timeout=60)
    r.raise_for_status()
    return r.content


def get_own_org_id(session):
    """Klubbens organisations-ID utifrån API-nyckeln."""
    root = ET.fromstring(get(session, "organisation/apiKey"))
    for el in root.iter():
        if local(el.tag) == "OrganisationId" and el.text:
            return el.text.strip()
    sys.exit("Kunde inte läsa organisations-ID från organisation/apiKey.")


def parse_results(xml_bytes):
    """Returnerar (antal starter, mängd med unika person-id)."""
    root = ET.fromstring(xml_bytes)
    starts = 0
    people = set()
    for pr in root.iter():
        if local(pr.tag) != "PersonResult":
            continue
        result = find_child(pr, "Result")
        status = find_text(result, "Status") if result is not None else None
        if status == "DidNotStart":
            continue  # anmäld men startade inte
        person = find_child(pr, "Person")
        pid = find_text(person, "Id") or find_text(person, "PersonId")
        if not pid:  # reserv: namn
            given = find_text(person, "Name", "Given") or ""
            family = find_text(person, "Name", "Family") or ""
            pid = f"{given} {family}".strip() or None
        starts += 1
        if pid:
            people.add(pid)
    return starts, people


def month_range(from_year, today):
    y, m = from_year, 1
    while (y, m) <= (today.year, today.month):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def build_html(rows, club_label):
    max_starts = max((r["starter"] for r in rows), default=1) or 1
    body = []
    for r in rows:
        y, m = r["manad"].split("-")
        label = f"{MONTHS_SV[int(m) - 1]} {y}"
        w = round(100 * r["starter"] / max_starts)
        body.append(
            f"<tr><td>{label}</td><td>{r['starter']}</td><td>{r['unika']}</td>"
            f"<td><div class='bar' style='width:{w}%'></div></td></tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tävlingsstatistik</title>
<style>
 body{{font-family:system-ui,sans-serif;margin:0;padding:1rem;color:#222}}
 table{{border-collapse:collapse;width:100%;max-width:720px}}
 th,td{{padding:.4rem .6rem;text-align:left;border-bottom:1px solid #ddd}}
 th:nth-child(2),th:nth-child(3),td:nth-child(2),td:nth-child(3){{text-align:right}}
 td:last-child{{width:40%}}
 .bar{{background:#2e7d32;height:.9rem;border-radius:3px;min-width:2px}}
 small{{color:#666}}
</style></head><body>
<h2>Tävlingsstatistik {club_label}</h2>
<table>
<thead><tr><th>Månad</th><th>Starter</th><th>Unika deltagare</th><th></th></tr></thead>
<tbody>
{chr(10).join(body)}
</tbody></table>
<p><small>Källa: Eventor. Uppdaterad {date.today().isoformat()}.</small></p>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-year", type=int, default=date.today().year)
    ap.add_argument("--org", help="Organisations-ID (hämtas annars från API-nyckeln)")
    ap.add_argument("--out", default=".", help="Katalog för utfiler")
    ap.add_argument("--debug", action="store_true", help="Spara rå-XML i ./raw")
    args = ap.parse_args()

    key = os.environ.get("EVENTOR_API_KEY")
    if not key:
        sys.exit("Sätt miljövariabeln EVENTOR_API_KEY.")

    session = requests.Session()
    session.headers["ApiKey"] = key

    org = args.org or get_own_org_id(session)
    org_name = ""
    if args.debug:
        os.makedirs("raw", exist_ok=True)

    rows = []
    for y, m in month_range(args.from_year, date.today()):
        last = calendar.monthrange(y, m)[1]
        xml = get(
            session, "results/organisation",
            organisationIds=org,
            fromDate=f"{y}-{m:02d}-01",
            toDate=f"{y}-{m:02d}-{last:02d}",
            includeSplitTimes="false",
        )
        if args.debug:
            with open(f"raw/{y}-{m:02d}.xml", "wb") as f:
                f.write(xml)
        starts, people = parse_results(xml)
        rows.append({"manad": f"{y}-{m:02d}", "starter": starts, "unika": len(people)})
        print(f"{y}-{m:02d}: {starts} starter, {len(people)} unika", file=sys.stderr)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "statistik.json"), "w", encoding="utf-8") as f:
        json.dump({"uppdaterad": date.today().isoformat(), "manader": rows},
                  f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "statistik.html"), "w", encoding="utf-8") as f:
        f.write(build_html(rows, org_name))


if __name__ == "__main__":
    main()
