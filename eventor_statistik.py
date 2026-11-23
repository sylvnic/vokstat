#!/usr/bin/env python3
"""
Hämtar klubbens resultat från Eventor (Svenska Orienteringsförbundet) och
räknar antal starter och antal unika deltagare per månad, uppdelat på
tävlingsnivå och gren.

Skapar två filer som kan läggas ut på hemsidan:
  statistik.json  - data för statistiken
  statistik.html  - färdig sida med rullgardinsmenyer för urval

Användning:
  export EVENTOR_API_KEY="din-nyckel"
  python eventor_statistik.py --from-year 2025

Kräver: pip install requests
"""
import argparse
import json
import os
import sys
import time
from datetime import date
from xml.etree import ElementTree as ET

import requests

BASE_URL = "https://eventor.orientering.se/api/"

# Tävlingsnivå enligt Eventors dokumentation
LEVELS = {"1": "Mästerskap", "2": "Nationell tävling", "3": "Distriktstävling",
          "4": "Närtävling", "5": "Klubbtävling", "okand": "Okänd"}
# Gren (Eventors DisciplineId)
DISCIPLINES = {"1": "Orientering", "2": "MTB-orientering", "3": "Skidorientering",
               "4": "Precisionsorientering", "okand": "Okänd"}

# Statusar som betyder att personen INTE startade
NOT_STARTED = {"DidNotStart", "DidNotEnter", "Cancelled", "Inactive",
               "Active", "Moved", "MovedUp"}

LEVEL_TAGS = ("EventClassificationId", "ClassificationId")
DISCIPLINE_TAGS = ("DisciplineId",)


def local(tag):
    """Tagg utan XML-namnrymd."""
    return tag.rsplit("}", 1)[-1]


def find_child(el, name):
    if el is None:
        return None
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def find_text(el, *path):
    for name in path:
        if el is None:
            return None
        el = find_child(el, name)
    return el.text.strip() if el is not None and el.text and el.text.strip() else None


def find_any(el, names):
    """Första förekomst av någon av taggarna (text eller attributet value/id)."""
    if el is None:
        return None
    for sub in el.iter():
        if local(sub.tag) in names:
            t = (sub.text or "").strip()
            if t:
                return t
            v = sub.get("value") or sub.get("id")
            if v:
                return v
    return None


def get(session, endpoint, **params):
    r = session.get(BASE_URL + endpoint, params=params, timeout=120)
    r.raise_for_status()
    return r.content


def get_own_org_id(session):
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
    """Lista med en post per start: datum, event-id, nivå, gren."""
    root = ET.fromstring(xml_bytes)
    out = []
    for rl in root.iter():
        if local(rl.tag) != "ResultList":
            continue
        ev = find_child(rl, "Event")
        event_id = find_text(ev, "EventId")
        level = find_any(ev, LEVEL_TAGS)
        disc = find_any(ev, DISCIPLINE_TAGS)
        for pr in rl.iter():
            if local(pr.tag) != "PersonResult":
                continue
            person = find_child(pr, "Person")
            pid = find_text(person, "PersonId") or find_text(person, "Id")
            if pid and wanted_id and pid != wanted_id:
                continue  # inte den efterfrågade personen
            if status_of(find_child(pr, "Result")) in NOT_STARTED:
                continue  # startade inte
            d = date_of(rl, pr)
            if d:
                out.append({"date": d, "event_id": event_id,
                            "level": level, "disc": disc})
    return out


def load_event_info(session, from_dt, to_dt):
    """event-id -> (nivå, gren), hämtat med ett enda anrop."""
    xml = get(session, "events", fromDate=from_dt, toDate=to_dt)
    root = ET.fromstring(xml)
    print("XML-struktur för events (första 40 taggar):", file=sys.stderr)
    for p in structure(root, 40):
        print("  " + p, file=sys.stderr)
    info = {}
    for ev in root.iter():
        if local(ev.tag) != "Event":
            continue
        eid = find_text(ev, "EventId")
        if eid:
            info[eid] = (find_any(ev, LEVEL_TAGS), find_any(ev, DISCIPLINE_TAGS))
    return info


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
    root = ET.fromstring(get(session, f"persons/organisations/{org}"))
    ids = set()
    for el in root.iter():
        if local(el.tag) == "Person":
            pid = find_text(el, "PersonId") or find_text(el, "Id")
            if pid:
                ids.add(pid)
    return sorted(ids)


def month_range(from_year, today):
    y, m = from_year, 1
    while (y, m) <= (today.year, today.month):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def aggregate(records, month_keys):
    """Räknar starter/unika för varje kombination av nivå och gren."""
    for r in records:
        r["level"] = r["level"] if r["level"] in LEVELS else "okand"
        r["disc"] = r["disc"] if r["disc"] in DISCIPLINES else "okand"
    levels = sorted({r["level"] for r in records})
    discs = sorted({r["disc"] for r in records})
    level_opts = [["alla", "Alla"]] + [[k, LEVELS[k]] for k in levels]
    disc_opts = [["alla", "Alla"]] + [[k, DISCIPLINES[k]] for k in discs]
    data = {}
    for lk, _ in level_opts:
        data[lk] = {}
        for dk, _ in disc_opts:
            sel = [r for r in records
                   if (lk == "alla" or r["level"] == lk)
                   and (dk == "alla" or r["disc"] == dk)]
            rows = []
            for mk in month_keys:
                in_month = [r for r in sel if r["ym"] == mk]
                rows.append({"m": mk, "s": len(in_month),
                             "u": len({r["pid"] for r in in_month})})
            data[lk][dk] = rows
    return {"niva": level_opts, "gren": disc_opts, "data": data}


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tävlingsstatistik</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;padding:1rem;color:#222}
 .filters{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1rem}
 label{display:flex;flex-direction:column;font-size:.85rem;color:#555;gap:.2rem}
 select{font-size:1rem;padding:.3rem .5rem}
 table{border-collapse:collapse;width:100%;max-width:720px}
 th,td{padding:.4rem .6rem;text-align:left;border-bottom:1px solid #ddd}
 th:nth-child(2),th:nth-child(3),td:nth-child(2),td:nth-child(3){text-align:right}
 td:last-child{width:40%}
 .bar{background:#2e7d32;height:.9rem;border-radius:3px;min-width:2px}
 small{color:#666}
</style></head><body>
<h2>Tävlingsstatistik</h2>
<div class="filters">
 <label>Tävlingsnivå <select id="niva"></select></label>
 <label>Gren <select id="gren"></select></label>
</div>
<table>
<thead><tr><th>Månad</th><th>Starter</th><th>Unika deltagare</th><th></th></tr></thead>
<tbody id="rader"></tbody>
</table>
<p><small>Källa: Eventor. Endast personer som faktiskt startat. Uppdaterad __UPPDATERAD__.</small></p>
<script>
const DATA = __DATA__;
const MAN = ["jan","feb","mar","apr","maj","jun","jul","aug","sep","okt","nov","dec"];
const nivaSel = document.getElementById("niva");
const grenSel = document.getElementById("gren");
function fill(sel, opts) {
  opts.forEach(function (o) {
    const e = document.createElement("option");
    e.value = o[0]; e.textContent = o[1]; sel.appendChild(e);
  });
}
function render() {
  const rows = DATA.data[nivaSel.value][grenSel.value];
  const max = Math.max(1, ...rows.map(function (r) { return r.s; }));
  const tb = document.getElementById("rader");
  tb.innerHTML = "";
  rows.forEach(function (r) {
    const p = r.m.split("-");
    const tr = document.createElement("tr");
    tr.innerHTML = "<td>" + MAN[parseInt(p[1], 10) - 1] + " " + p[0] + "</td><td>" +
      r.s + "</td><td>" + r.u + "</td><td><div class='bar' style='width:" +
      Math.round(100 * r.s / max) + "%'></div></td>";
    tb.appendChild(tr);
  });
}
fill(nivaSel, DATA.niva);
fill(grenSel, DATA.gren);
nivaSel.onchange = render;
grenSel.onchange = render;
render();
</script>
</body></html>
"""


def build_html(payload, updated):
    data_json = json.dumps(payload, ensure_ascii=True).replace("</", "<\\/")
    return (HTML_TEMPLATE.replace("__DATA__", data_json)
            .replace("__UPPDATERAD__", updated))


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
    month_keys = [f"{y}-{m:02d}" for y, m in month_range(args.from_year, today)]
    from_dt = f"{args.from_year}-01-01 00:00:00"
    to_dt = f"{today.isoformat()} 23:59:59"

    records = []
    printed_structure = False
    n_ok = n_empty = 0
    for pid in members:
        r = session.get(BASE_URL + "results/person", timeout=120, params={
            "personId": pid, "fromDate": from_dt, "toDate": to_dt,
            "includeSplitTimes": "false"})
        if r.status_code == 404:
            n_empty += 1
            continue
        r.raise_for_status()
        if not printed_structure:
            print("XML-struktur (första svaret):", file=sys.stderr)
            for p in structure(ET.fromstring(r.content)):
                print("  " + p, file=sys.stderr)
            printed_structure = True
        starts = parse_person_results(r.content, pid)
        if not starts:
            n_empty += 1
        else:
            n_ok += 1
        for s in starts:
            s["pid"] = pid
            s["ym"] = s["date"][:7]
            records.append(s)
        time.sleep(0.2)  # var snäll mot Eventor

    outside = [r for r in records if r["ym"] not in month_keys]
    records = [r for r in records if r["ym"] in month_keys]
    print(f"Medlemmar med starter: {n_ok}, utan: {n_empty}, "
          f"starter utanför perioden: {len(outside)}", file=sys.stderr)

    # Saknas nivå/gren i resultatsvaret slår vi upp dem i tävlingslistan
    if any(r["level"] is None or r["disc"] is None for r in records):
        info = load_event_info(session, from_dt, to_dt)
        for r in records:
            lvl, dsc = info.get(r["event_id"], (None, None))
            r["level"] = r["level"] or lvl
            r["disc"] = r["disc"] or dsc

    payload = aggregate(records, month_keys)
    n_unknown = sum(1 for r in records if r["level"] == "okand")
    print(f"Starter totalt: {len(records)}, varav utan känd nivå: {n_unknown}",
          file=sys.stderr)
    for row in payload["data"]["alla"]["alla"]:
        print(f"{row['m']}: {row['s']} starter, {row['u']} unika", file=sys.stderr)
    if not records:
        sys.exit("Inga starter hittades - se XML-strukturen ovan.")

    os.makedirs(args.out, exist_ok=True)
    payload["uppdaterad"] = today.isoformat()
    with open(os.path.join(args.out, "statistik.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out, "statistik.html"), "w", encoding="utf-8") as f:
        f.write(build_html(payload, today.isoformat()))


if __name__ == "__main__":
    main()
