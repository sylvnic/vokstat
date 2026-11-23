#!/usr/bin/env python3
"""
Hämtar klubbens resultat från Eventor (Svenska Orienteringsförbundet) och
bygger en statistiksida med starter och unika deltagare per månad, per
tävlingstyp, samt en lista med antal startande per tävling.

Skapar två filer som kan läggas ut på hemsidan:
  statistik.json  - data för statistiken
  statistik.html  - färdig sida (diagram, kryssrutor, lista)

Användning:
  export EVENTOR_API_KEY="din-nyckel"
  python eventor_statistik.py --from-year 2025

Kräver: pip install requests
Filen fonts.css (typsnitt) ska ligga bredvid skriptet; saknas den används
datorns vanliga typsnitt i stället.
"""
import argparse
import json
import os
import random
import sys
import time
from datetime import date
from xml.etree import ElementTree as ET

import requests

BASE_URL = "https://eventor.orientering.se/api/"

# Tävlingstyp enligt Eventors klassificering (EventClassificationId)
LEVELS = {"1": "Mästerskap", "2": "Nationella", "3": "Distrikt",
          "4": "Närtävlingar", "5": "Klubbtävlingar", "6": "Internationella",
          "okand": "Övriga"}

# Statusar som betyder att personen INTE startade
NOT_STARTED = {"DidNotStart", "DidNotEnter", "Cancelled", "Inactive",
               "Active", "Moved", "MovedUp"}

LEVEL_TAGS = ("EventClassificationId", "ClassificationId")


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


def get_own_org(session):
    """Klubbens organisations-ID och namn utifrån API-nyckeln."""
    root = ET.fromstring(get(session, "organisation/apiKey"))
    for el in root.iter():
        if local(el.tag) == "Organisation":
            oid = find_text(el, "OrganisationId")
            if oid:
                return oid, find_text(el, "Name") or ""
    for el in root.iter():
        if local(el.tag) == "OrganisationId" and el.text:
            return el.text.strip(), ""
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
    """Lista med en post per start: datum, event-id, namn, nivå."""
    root = ET.fromstring(xml_bytes)
    out = []
    for rl in root.iter():
        if local(rl.tag) != "ResultList":
            continue
        ev = find_child(rl, "Event")
        event_id = find_text(ev, "EventId")
        name = find_text(ev, "Name")
        level = find_any(ev, LEVEL_TAGS)
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
                            "name": name, "level": level})
    return out


def load_event_info(session, from_dt, to_dt):
    """event-id -> (nivå, namn), hämtat med ett enda anrop."""
    root = ET.fromstring(get(session, "events", fromDate=from_dt, toDate=to_dt))
    print("XML-struktur för events (första 40 taggar):", file=sys.stderr)
    for p in structure(root, 40):
        print("  " + p, file=sys.stderr)
    info = {}
    for ev in root.iter():
        if local(ev.tag) != "Event":
            continue
        eid = find_text(ev, "EventId")
        if eid:
            info[eid] = (find_any(ev, LEVEL_TAGS), find_text(ev, "Name"))
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


def build_payload(records, years, club, updated):
    """Gör om starterna till kompakt data för webbsidan.

    Personer ersätts med slumpade löpnummer (inga namn eller Eventor-ID
    hamnar på den publika sidan).
    """
    for r in records:
        if r["level"] not in LEVELS:
            r["level"] = "okand"
    pids = sorted({r["pid"] for r in records})
    random.shuffle(pids)
    pidx = {p: i for i, p in enumerate(pids)}

    ev_index, events, starts = {}, [], set()
    for r in records:
        key = f'{r["event_id"] or r["name"]}|{r["date"]}'
        if key not in ev_index:
            ev_index[key] = len(events)
            events.append([r["name"] or "Tävling", r["date"], r["level"]])
        starts.add((ev_index[key], pidx[r["pid"]]))

    present = {e[2] for e in events}
    return {
        "klubb": club,
        "uppdaterad": updated,
        "ar": years,
        "niva": [[k, v] for k, v in LEVELS.items() if k in present],
        "e": events,
        "s": sorted(starts),
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tävlingsstatistik</title>
<style>
__FONTS__
:root{--bg:#f8f6ef;--ink:#0c3b24;--green:#064e2b;--orange:#ff4d00;--muted:#5d6f64;
 --line:#dedbcf;--card:#fdfcf8;--prev:#a9bbaf}
*{box-sizing:border-box}
body{margin:0;padding:1.25rem;background:var(--bg);color:var(--ink);
 font-family:"Barlow","Helvetica Neue",Arial,sans-serif;line-height:1.35}
h1,h3,.num,.card-title,.c{font-family:"Barlow Condensed","Arial Narrow","Roboto Condensed",sans-serif;
 font-weight:700;text-transform:uppercase}
.wrap{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,6fr);gap:2rem;
 max-width:1000px;margin:0 auto;align-items:start}
@media (max-width:720px){.wrap{grid-template-columns:1fr}}
.klubb{color:var(--orange);font-weight:600;letter-spacing:.18em;text-transform:uppercase;font-size:.78rem}
h1{font-size:clamp(2.3rem,6vw,3.5rem);line-height:.95;margin:.35rem 0 .9rem}
.intro{color:var(--muted);margin:0 0 1.2rem}
h3{font-size:1.15rem;letter-spacing:.04em;margin:1.4rem 0 .6rem}
.opts{display:grid;grid-template-columns:1fr 1fr;gap:.5rem}
.opt{display:flex;align-items:center;gap:.5rem;border:1px solid var(--line);background:var(--card);
 border-radius:6px;padding:.55rem .7rem;cursor:pointer}
.opt input{accent-color:var(--orange);width:1rem;height:1rem;margin:0}
select{font:inherit;padding:.5rem .7rem;border:1px solid var(--line);border-radius:6px;
 background:var(--card);color:var(--ink)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;
 box-shadow:0 1px 3px rgba(0,0,0,.08)}
.head{background:var(--green);color:#fff;padding:1rem 1.25rem;display:flex;
 justify-content:space-between;align-items:center;gap:1rem}
.head small{display:block;letter-spacing:.18em;text-transform:uppercase;font-size:.7rem;
 opacity:.8;font-weight:600}
.card-title{font-size:1.9rem;line-height:1.05;margin-top:.15rem}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);border-bottom:1px solid var(--line)}
.tile{padding:.8rem 1rem;border-right:1px solid var(--line)}
.tile:last-child{border-right:0}
.lab{font-size:.66rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:600}
.num{font-size:2.1rem;line-height:1.1}
.chart{padding:.8rem 1rem .4rem;border-bottom:1px solid var(--line)}
.legend{display:flex;gap:1rem;font-size:.78rem;color:var(--muted);margin-bottom:.3rem;flex-wrap:wrap}
.legend i{display:inline-block;width:.7rem;height:.7rem;border-radius:2px;margin-right:.35rem;vertical-align:-1px}
svg text{font-family:inherit}
.listhead{padding:.9rem 1.25rem .3rem}
.listhead h3{margin:0}
.list{max-height:300px;overflow-y:auto;padding:0 .5rem 0 1.25rem}
.row{display:grid;grid-template-columns:3.4rem 1fr 2.6rem;gap:.75rem;align-items:center;
 padding:.55rem 0;border-top:1px solid var(--line)}
.d{font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}
.n{font-size:.92rem;overflow-wrap:anywhere}
.track{height:.38rem;background:#e9e6da;border-radius:3px;margin-top:.25rem;overflow:hidden}
.fill{height:100%;background:var(--orange)}
.c{font-size:1.25rem;text-align:right}
.empty{padding:1rem 0;color:var(--muted)}
.foot{background:#ecebe2;color:var(--muted);font-size:.72rem;padding:.55rem 1.25rem}
</style></head><body>
<div class="wrap">
 <section>
  <div class="klubb" id="klubb"></div>
  <h1>Tävlingsstatistik</h1>
  <p class="intro">Siffrorna hämtas från Eventor varje måndag kl 9. Välj vad diagrammet ska visa.</p>
  <h3>Tävlingstyper</h3>
  <div class="opts" id="typer"></div>
  <h3>Visa</h3>
  <div class="opts">
   <label class="opt"><input type="radio" name="visa" value="s" checked> Starter</label>
   <label class="opt"><input type="radio" name="visa" value="u"> Unika deltagare</label>
  </div>
  <div id="arblock"><h3>År</h3><select id="ar"></select></div>
 </section>
 <section class="card">
  <div class="head">
   <div><small id="hklubb"></small><div class="card-title" id="htitel"></div></div>
   <svg width="40" height="40" viewBox="0 0 40 40" aria-hidden="true"><rect width="40" height="40" rx="4" fill="#fff"/><polygon points="40,0 40,40 0,40" fill="#ff4d00"/></svg>
  </div>
  <div class="tiles">
   <div class="tile"><div class="lab" id="l1"></div><div class="num" id="v1"></div></div>
   <div class="tile"><div class="lab" id="l2"></div><div class="num" id="v2"></div></div>
   <div class="tile"><div class="lab">Förändring</div><div class="num" id="v3"></div></div>
  </div>
  <div class="chart">
   <div class="legend" id="legend"></div>
   <svg id="diagram" viewBox="0 0 460 230" width="100%" role="img" aria-label="Staplar per månad"></svg>
  </div>
  <div class="listhead"><h3>Startade per tävling</h3></div>
  <div class="list" id="lista"></div>
  <div class="foot" id="foot"></div>
 </section>
</div>
<script>
const D = __DATA__;
const MAN = ["jan","feb","mar","apr","maj","jun","jul","aug","sep","okt","nov","dec"];
const el = function (id) { return document.getElementById(id); };
const fmt = function (n) { return n.toLocaleString("sv-SE"); };
const E = D.e.map(function (e) { return {n: e[0], d: e[1], l: e[2], y: parseInt(e[1].slice(0, 4), 10)}; });
const S = D.s;

el("klubb").textContent = D.klubb;
el("hklubb").textContent = D.klubb;

D.niva.forEach(function (o) {
  const lab = document.createElement("label");
  lab.className = "opt";
  const inp = document.createElement("input");
  inp.type = "checkbox"; inp.value = o[0]; inp.checked = true; inp.onchange = render;
  lab.appendChild(inp);
  lab.appendChild(document.createTextNode(o[1]));
  el("typer").appendChild(lab);
});
document.querySelectorAll("input[name=visa]").forEach(function (r) { r.onchange = render; });

const years = D.ar.slice().sort(function (a, b) { return b - a; });
years.forEach(function (y) {
  const o = document.createElement("option");
  o.value = y; o.textContent = y; el("ar").appendChild(o);
});
el("ar").onchange = render;
if (years.length < 2) { el("arblock").style.display = "none"; }

function agg(year, cutoff, levels) {
  const ms = new Array(12).fill(0), mp = [], all = new Set();
  for (let i = 0; i < 12; i++) { mp.push(new Set()); }
  let total = 0;
  S.forEach(function (p) {
    const e = E[p[0]];
    if (e.y !== year || !levels.has(e.l) || e.d.slice(5) > cutoff) { return; }
    const m = parseInt(e.d.slice(5, 7), 10) - 1;
    ms[m]++; mp[m].add(p[1]); all.add(p[1]); total++;
  });
  return {s: ms, u: mp.map(function (x) { return x.size; }), total: total, people: all.size};
}

function niceTicks(max) {
  if (max <= 0) { return {step: 1, top: 4}; }
  const raw = max / 4, pow = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / pow;
  const m = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10, step = m * pow;
  return {step: step, top: Math.ceil(max / step) * step};
}

function drawChart(cur, prev, year, nowMonth, unit) {
  const W = 460, H = 230, L = 40, R = 8, T = 16, B = 24, pw = W - L - R, ph = H - T - B;
  const hasPrev = prev !== null;
  const max = Math.max.apply(null, cur.concat(hasPrev ? prev : [0]));
  const nt = niceTicks(max);
  const gw = pw / 12, bw = hasPrev ? Math.min(11, gw * 0.32) : Math.min(18, gw * 0.5);
  let s = "";
  for (let v = 0; v <= nt.top; v += nt.step) {
    const y = T + ph - ph * v / nt.top;
    s += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y + '" y2="' + y + '" stroke="#e4e1d5"/>' +
         '<text x="' + (L - 6) + '" y="' + (y + 3) + '" text-anchor="end" font-size="10" fill="#5d6f64">' + fmt(v) + '</text>';
  }
  s += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + (T + ph) + '" y2="' + (T + ph) + '" stroke="#9aa89f"/>';
  for (let m = 0; m < 12; m++) {
    const cx = L + gw * m + gw / 2;
    if (hasPrev && prev[m] > 0) {
      const h = ph * prev[m] / nt.top;
      s += '<rect x="' + (cx - bw - 1) + '" y="' + (T + ph - h) + '" width="' + bw + '" height="' + h +
           '" rx="1.5" fill="#a9bbaf"><title>' + MAN[m] + " " + (year - 1) + ": " + prev[m] + " " + unit + "</title></rect>";
    }
    if (m <= nowMonth && cur[m] > 0) {
      const h = ph * cur[m] / nt.top, x = hasPrev ? cx + 1 : cx - bw / 2;
      s += '<rect x="' + x + '" y="' + (T + ph - h) + '" width="' + bw + '" height="' + h +
           '" rx="1.5" fill="#ff4d00"><title>' + MAN[m] + " " + year + ": " + cur[m] + " " + unit + "</title></rect>" +
           '<text x="' + (x + bw / 2) + '" y="' + (T + ph - h - 3) + '" text-anchor="middle" font-size="9.5" ' +
           'font-weight="600" fill="#0c3b24">' + cur[m] + "</text>";
    }
    s += '<text x="' + cx + '" y="' + (H - 8) + '" text-anchor="middle" font-size="10.5" fill="#5d6f64">' + MAN[m] + "</text>";
  }
  el("diagram").innerHTML = s;
}

function render() {
  const levels = new Set();
  document.querySelectorAll("#typer input:checked").forEach(function (i) { levels.add(i.value); });
  const mode = document.querySelector("input[name=visa]:checked").value;
  const key = mode === "s" ? "s" : "u", tot = mode === "s" ? "total" : "people";
  const unit = mode === "s" ? "starter" : "unika deltagare";
  const year = parseInt(el("ar").value || years[0], 10);
  const hasPrev = D.ar.indexOf(year - 1) >= 0;
  const isNow = String(year) === D.uppdaterad.slice(0, 4);
  const cutoff = isNow ? D.uppdaterad.slice(5) : "12-31";
  const nowMonth = isNow ? parseInt(D.uppdaterad.slice(5, 7), 10) - 1 : 11;

  const cur = agg(year, "12-31", levels);
  const curC = isNow ? agg(year, cutoff, levels) : cur;
  const prevFull = hasPrev ? agg(year - 1, "12-31", levels) : null;
  const prevC = hasPrev ? agg(year - 1, cutoff, levels) : null;

  el("htitel").textContent = (mode === "s" ? "Startade " : "Unika deltagare ") + year;
  el("l1").textContent = (mode === "s" ? "Starter " : "Unika deltagare ") + (isNow ? "i år" : "under " + year);
  el("v1").textContent = fmt(curC[tot]);
  el("l2").textContent = isNow ? "Samma datum " + (year - 1) : "Hela " + (year - 1);
  el("v2").textContent = hasPrev ? fmt(prevC[tot]) : "–";
  let change = "–";
  if (hasPrev && prevC[tot] > 0) {
    const ch = Math.round(100 * (curC[tot] - prevC[tot]) / prevC[tot]);
    change = (ch > 0 ? "+" : ch < 0 ? "\\u2212" : "") + Math.abs(ch) + " %";
  }
  el("v3").textContent = change;

  el("legend").innerHTML = '<span><i style="background:#ff4d00"></i>' + year + "</span>" +
    (hasPrev ? '<span><i style="background:#a9bbaf"></i>' + (year - 1) + " (totalt " + fmt(prevFull[tot]) + ")</span>" : "");
  drawChart(cur[key], hasPrev ? prevFull[key] : null, year, nowMonth, unit);

  const cnt = new Array(E.length).fill(0);
  S.forEach(function (p) { cnt[p[0]]++; });
  const idx = [];
  E.forEach(function (e, i) { if (e.y === year && cnt[i] > 0 && levels.has(e.l)) { idx.push(i); } });
  idx.sort(function (a, b) {
    return E[a].d < E[b].d ? 1 : E[a].d > E[b].d ? -1 : E[a].n.localeCompare(E[b].n, "sv");
  });
  const maxc = idx.reduce(function (m, i) { return Math.max(m, cnt[i]); }, 1);
  const list = el("lista");
  list.innerHTML = "";
  if (!idx.length) {
    const p = document.createElement("div");
    p.className = "empty"; p.textContent = "Inga starter för valda tävlingstyper.";
    list.appendChild(p);
  }
  idx.forEach(function (i) {
    const e = E[i], row = document.createElement("div");
    row.className = "row";
    const d = document.createElement("div");
    d.className = "d"; d.textContent = parseInt(e.d.slice(8, 10), 10) + " " + MAN[parseInt(e.d.slice(5, 7), 10) - 1];
    const mid = document.createElement("div");
    const n = document.createElement("div");
    n.className = "n"; n.textContent = e.n;
    const tr = document.createElement("div");
    tr.className = "track";
    const f = document.createElement("div");
    f.className = "fill"; f.style.width = Math.round(100 * cnt[i] / maxc) + "%";
    tr.appendChild(f); mid.appendChild(n); mid.appendChild(tr);
    const c = document.createElement("div");
    c.className = "c"; c.textContent = cnt[i];
    row.appendChild(d); row.appendChild(mid); row.appendChild(c);
    list.appendChild(row);
  });

  el("foot").textContent = "Källa: Eventor \\u00b7 klubbmedlemmar som startade \\u00b7 uppdateras måndagar kl 9 \\u00b7 senast " +
    parseInt(D.uppdaterad.slice(8, 10), 10) + " " + MAN[parseInt(D.uppdaterad.slice(5, 7), 10) - 1];
}
render();
</script>
</body></html>
"""


def read_fonts_css():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts.css")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        print("Obs: fonts.css hittades inte - använder vanliga typsnitt.",
              file=sys.stderr)
        return ""


def build_html(payload):
    data_json = json.dumps(payload, ensure_ascii=True).replace("</", "<\\/")
    return (HTML_TEMPLATE.replace("__FONTS__", read_fonts_css())
            .replace("__DATA__", data_json))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-year", type=int, default=date.today().year)
    ap.add_argument("--org", help="Organisations-ID (hämtas annars från API-nyckeln)")
    ap.add_argument("--club-name", help="Klubbnamn på sidan (hämtas annars från Eventor)")
    ap.add_argument("--out", default=".", help="Katalog för utfiler")
    args = ap.parse_args()

    key = os.environ.get("EVENTOR_API_KEY")
    if not key:
        sys.exit("Sätt miljövariabeln EVENTOR_API_KEY.")

    session = requests.Session()
    session.headers["ApiKey"] = key

    if args.org:
        org, org_name = args.org, ""
    else:
        org, org_name = get_own_org(session)
    club = args.club_name or org_name
    members = list_member_ids(session, org)
    print(f"Organisation {org} ({club or 'namn saknas'}): "
          f"{len(members)} medlemmar hittades", file=sys.stderr)
    if not members:
        sys.exit("Inga medlemmar hittades.")

    today = date.today()
    years = list(range(args.from_year, today.year + 1))
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
        n_ok += 1 if starts else 0
        n_empty += 0 if starts else 1
        for s in starts:
            s["pid"] = pid
            records.append(s)
        time.sleep(0.2)  # var snäll mot Eventor

    records = [r for r in records if int(r["date"][:4]) in years]
    print(f"Medlemmar med starter: {n_ok}, utan: {n_empty}", file=sys.stderr)

    # Saknas nivå eller namn i resultatsvaret slår vi upp dem i tävlingslistan
    if any(r["level"] is None or not r["name"] for r in records):
        info = load_event_info(session, from_dt, to_dt)
        for r in records:
            lvl, nm = info.get(r["event_id"], (None, None))
            r["level"] = r["level"] or lvl
            r["name"] = r["name"] or nm

    payload = build_payload(records, years, club, today.isoformat())
    n_unknown = sum(1 for e in payload["e"] if e[2] == "okand")
    print(f"Starter: {len(payload['s'])} i {len(payload['e'])} tävlingar, "
          f"varav {n_unknown} tävlingar utan känd typ", file=sys.stderr)
    per_month = {}
    for ei, _ in payload["s"]:
        ym = payload["e"][ei][1][:7]
        per_month[ym] = per_month.get(ym, 0) + 1
    for ym in sorted(per_month):
        print(f"  {ym}: {per_month[ym]} starter", file=sys.stderr)
    if not records:
        sys.exit("Inga starter hittades - se XML-strukturen ovan.")

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "statistik.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    with open(os.path.join(args.out, "statistik.html"), "w", encoding="utf-8") as f:
        f.write(build_html(payload))


if __name__ == "__main__":
    main()
