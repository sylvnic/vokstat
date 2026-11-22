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

Kräver: pip install requests
"""
import argparse
import calendar
import json
import os
import sys
import time
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


def status_of(result):
    """Status för ett resultat (både IOF-format och Eventors eget format)."""
    if result is None:
        return None
    s = find_text(result, "Status")
    if s:
        return s
    cs = find_child(result, "CompetitorStatus")
    return cs.get("value") if cs is not None else None


def date_of(resultlist, personresult):
    """Datum (yyyy-mm-dd) för tävlingen, eller None om det inte hittas."""
    ev = find_child(resultlist, "Event")
    for path in (("StartDate", "Date"), ("EventRace", "RaceDate", "Date"),
                 ("StartTime", "Date"), ("FinishDate", "Date")):
        d = find_text(ev, *path)
        if d:
            return d[:10]
    res = find_child(personresult, "Result")
    d = find_text(res, "StartTime", "Date") or find_text(res, "StartTime")
    return d[:10] if d else None


def parse_person_results(xml_bytes, wanted_id):
    """Returnerar lista med datum (yyyy-mm-dd) för varje start av personen."""
    root = ET.fromstring(xml_bytes)
    dates = []
    for rl in root.iter():
        if local(rl.tag) != "ResultList":
            continue
        for pr in rl.iter():
            if local(pr.tag) != "PersonResult":
                continue
            person = find_child(pr, "Person")
            pid = find_text(person, "PersonId") or find_text(person, "Id")
            if pid and wanted_id and pid != wanted_id:
                continue  # inte den efterfrågade personen
            if status_of(find_child(pr, "Result")) == "DidNotStart":
                continue
            d = date_of(rl, pr)
            if d:
                dates.append(d)
    return dates


def structure(root, limit=120):
    """Lista över vilka XML-taggar som finns (inga personuppgifter)."""
    paths = set()

    def walk(el, path):
        p = path + "/" + local(el.tag)
        attrs = ",".join(sorted(local(a) for a in el.attrib))
        paths.add(p + (f" [{attrs}]" if attrs else ""))
        for c in el:
            walk(c, p)

    walk(root, "")
    return sorted(paths)[:limit]


def list_member_ids(session, org):
    xml = get(session, f"persons/organisations/{org}")
    root = ET.fromstring(xml)
    ids = []
    for el in root.iter():
        if local(el.tag) == "Person":
            pid = find_text(el, "PersonId") or find_text(el, "Id")
            if pid:
                ids.append(pid)
    return sorted(set(ids))


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
    args = ap.parse_args()

    key = os.environ.get("EVENTOR_API_KEY")
    if not key:
        sys.exit("Sätt miljövariabeln EVENTOR_API_KEY.")

    session = requests.Session()
    session.headers["ApiKey"] = key

    org = args.org or get_own_org_id(session)
    members = list_member_ids(session, org)
    print(f"Organisation {org}: {len(members)} medlemmar hittades", file=sys.stderr)
    if not members:
        sys.exit("Inga medlemmar hittades.")

    today = date.today()
    months = {f"{y}-{m:02d}": {"starter": 0, "personer": set()}
              for y, m in month_range(args.from_year, today)}
    from_dt = f"{args.from_year}-01-01 00:00:00"
    to_dt = f"{today.isoformat()} 23:59:59"

    printed_structure = False
    n_ok = n_empty = n_undated = 0
    for pid in members:
        r = session.get(BASE_URL + "results/person", timeout=60, params={
            "personId": pid, "fromDate": from_dt, "toDate": to_dt,
            "includeSplitTimes": "false"})
        if r.status_code == 404:
            n_empty += 1
            continue
        r.raise_for_status()
        if not printed_structure:
            root = ET.fromstring(r.content)
            print("XML-struktur (första svaret):", file=sys.stderr)
            for p in structure(root):
                print("  " + p, file=sys.stderr)
            printed_structure = True
        dates = parse_person_results(r.content, pid)
        if not dates:
            n_empty += 1
            continue
        n_ok += 1
        for d in dates:
            ym = d[:7]
            if ym in months:
                months[ym]["starter"] += 1
                months[ym]["personer"].add(pid)
            else:
                n_undated += 1
        time.sleep(0.2)  # var snäll mot Eventor

    print(f"Medlemmar med resultat: {n_ok}, utan: {n_empty}, "
          f"starter utanför perioden: {n_undated}", file=sys.stderr)

    rows = [{"manad": k, "starter": v["starter"], "unika": len(v["personer"])}
            for k, v in sorted(months.items())]
    for row in rows:
        print(f"{row['manad']}: {row['starter']} starter, {row['unika']} unika",
              file=sys.stderr)
    if sum(r["starter"] for r in rows) == 0:
        sys.exit("Inga starter hittades - se XML-strukturen ovan.")

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "statistik.json"), "w", encoding="utf-8") as f:
        json.dump({"uppdaterad": today.isoformat(), "manader": rows},
                  f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "statistik.html"), "w", encoding="utf-8") as f:
        f.write(build_html(rows, ""))


if __name__ == "__main__":
    main()
