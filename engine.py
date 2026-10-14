#!/usr/bin/env python3
"""
TMS Dashboard Sync  (read-only)
-------------------------------
Pulls data from TallyPrime (XML over HTTP, port 9000), computes the business
picture and writes TMS_Dashboard.html next to this script.

ONLY sends Export requests - it never creates, changes or deletes anything in Tally.

Usage:
  python tms_sync.py                 # one sync, then exit
  python tms_sync.py --every 15      # keep syncing every 15 minutes
Options: --host localhost --port 9000 --company "TECHNO MED SERVICES" --od-limit 10000000
"""
import argparse, datetime as dt, json, re, sys, time, traceback, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
CACHE = HERE / "tms_cache"
TEMPLATE = HERE / "dashboard_template.html"
OUTPUT = HERE / "TMS_Dashboard.html"
DATA_OUT = HERE / "tms_dashboard_data.json"
TIMEOUT = 240
PORT = 8765
AI_KEY_FILE = HERE / "ai_key.txt"
AI_MODEL = "claude-sonnet-5-5"
AI_EFFORT = "medium"   # chat: Anthropic recommends medium or low for Sonnet 5.5

# ---------------------------------------------------------------- Tally I/O
def _clean_xml(text):
    text = re.sub(r"&#(\d+);", lambda m: "" if int(m.group(1)) < 32 and int(m.group(1)) not in (9, 10, 13) else m.group(0), text)
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: "" if int(m.group(1), 16) < 32 and int(m.group(1), 16) not in (9, 10, 13) else m.group(0), text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    # Custom fields (e.g. <UDF:XYZ>) use undeclared XML prefixes - turn "UDF:" into "UDF_"
    return re.sub(r"(</?)([A-Za-z][\w.]*):(?=[A-Za-z])", r"\1\2_", text)

def _decode(raw):
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"): return raw.decode("utf-16")
    if len(raw) > 4 and raw[1:2] == b"\x00" and raw[3:4] == b"\x00": return raw.decode("utf-16-le")
    try: return raw.decode("utf-8")
    except UnicodeDecodeError: return raw.decode("latin-1")

class Tally:
    def __init__(self, host, port, company):
        self.url = "http://{}:{}".format(host, port); self.company = company; self.calls = 0
    def post(self, xml):
        self.calls += 1
        req = urllib.request.Request(self.url, data=xml.encode("utf-8"), headers={"Content-Type": "text/xml; charset=utf-8"}, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            text = _clean_xml(_decode(r.read()))
        root = ET.fromstring(text)
        le = root.find(".//LINEERROR")
        if le is not None and (le.text or "").strip():
            raise RuntimeError("Tally error: " + le.text.strip())
        return root
    def sv(self, extra=""):
        co = "<SVCURRENTCOMPANY>{}</SVCURRENTCOMPANY>".format(escape(self.company)) if self.company else ""
        return "<STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>{}{}</STATICVARIABLES>".format(co, extra)
    def collection(self, typ, fetch, f=None, t=None, filt=False):
        per = period(f, t) if f else ""
        fx = "<FILTER>TMSNotCancelled</FILTER>" if filt else ""
        sysf = "<SYSTEM TYPE=\"Formulae\" NAME=\"TMSNotCancelled\">NOT $IsCancelled AND NOT $IsOptional</SYSTEM>" if filt else ""
        xml = ("<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>TMSColl</ID></HEADER>"
               "<BODY><DESC>{}<TDL><TDLMESSAGE><COLLECTION NAME=\"TMSColl\" ISMODIFY=\"No\"><TYPE>{}</TYPE><FETCH>{}</FETCH>{}</COLLECTION>{}"
               "</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>").format(self.sv(per), typ, fetch, fx, sysf)
        return self.post(xml)
    def report(self, rid, extra=""):
        xml = ("<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Data</TYPE><ID>{}</ID></HEADER>"
               "<BODY><DESC>{}</DESC></BODY></ENVELOPE>").format(escape(rid), self.sv(extra))
        return self.post(xml)

def period(f, t):
    return "<SVFROMDATE TYPE=\"Date\">{}</SVFROMDATE><SVTODATE TYPE=\"Date\">{}</SVTODATE>".format(ymd(f), ymd(t))

def ymd(d): return d.strftime("%Y%m%d")
def txt(el, tag):
    c = el.find(tag)
    return (c.text or "").strip() if c is not None and c.text else ""
def amt(v):
    if v is None: return 0.0
    s = str(v).strip().replace(",", "")
    m = re.search(r"-?\d+(\.\d+)?", s)
    if not m: return 0.0
    n = float(m.group(0))
    return -abs(n) if "(-)" in s else n
def qty(v):
    m = re.search(r"-?\d+(\.\d+)?", (v or "").replace(",", ""))
    return float(m.group(0)) if m else 0.0
def tdate(s):
    s = (s or "").strip()
    for f in ("%Y%m%d", "%d-%b-%y", "%d-%b-%Y"):
        try: return dt.datetime.strptime(s, f).date()
        except ValueError: pass
    return None
def fy_start(d): return dt.date(d.year if d.month >= 4 else d.year - 1, 4, 1)
def month_end(y, m):
    nxt = dt.date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return nxt - dt.timedelta(days=1)

# ---------------------------------------------------------------- cache
def cache_get(key, max_age_days=None, today=None):
    p = CACHE / (key + ".json")
    if not p.exists(): return None
    try: obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception: return None
    if max_age_days is not None and obj.get("_day") != str(today): return None
    return obj["data"]
def cache_put(key, data, today):
    CACHE.mkdir(exist_ok=True)
    (CACHE / (key + ".json")).write_text(json.dumps({"_day": str(today), "data": data}), encoding="utf-8")

# ---------------------------------------------------------------- pulls
def pull_groups(T):
    out = {}
    for el in T.collection("Group", "NAME, PARENT").iter("GROUP"):
        n = el.get("NAME")
        if n: out[n] = {"parent": txt(el, "PARENT"), "reserved": el.get("RESERVEDNAME") or ""}
    return out

def pull_vtypes(T):
    out = {}
    for el in T.collection("VoucherType", "NAME, PARENT").iter("VOUCHERTYPE"):
        n = el.get("NAME")
        if n: out[n] = {"parent": txt(el, "PARENT"), "reserved": el.get("RESERVEDNAME") or ""}
    return out

def pull_ledger_balances(T, f, t):
    out = {}
    for el in T.collection("Ledger", "NAME, PARENT, CLOSINGBALANCE", f, t).iter("LEDGER"):
        n = el.get("NAME")
        if n: out[n] = [txt(el, "PARENT"), amt(txt(el, "CLOSINGBALANCE"))]
    return out

def pull_stock(T, f, t):
    out = {}
    for el in T.collection("StockItem", "NAME, PARENT, BASEUNITS, CLOSINGBALANCE, CLOSINGVALUE", f, t).iter("STOCKITEM"):
        n = el.get("NAME")
        if n: out[n] = [txt(el, "PARENT"), qty(txt(el, "CLOSINGBALANCE")), abs(amt(txt(el, "CLOSINGVALUE"))), txt(el, "BASEUNITS")]
    return out

def pull_bills(T, rid, today):
    root = T.report(rid, "<SVTODATE TYPE=\"Date\">{}</SVTODATE>".format(ymd(today)))
    bills, cur = [], None
    for el in root.iter():
        if el.tag == "BILLFIXED":
            cur = {"date": txt(el, "BILLDATE"), "ref": txt(el, "BILLREF"), "party": txt(el, "BILLPARTY"), "amount": 0.0, "due": "", "od": 0}
            bills.append(cur)
        elif cur is not None:
            if el.tag == "BILLCL": cur["amount"] = amt(el.text)
            elif el.tag == "BILLDUE": cur["due"] = (el.text or "").strip()
            elif el.tag == "BILLOVERDUE": cur["od"] = int(amt(el.text))
    return bills

def pull_vouchers(T, f, t):
    root = T.collection("Voucher", "DATE, VOUCHERTYPENAME, VOUCHERNUMBER, PARTYLEDGERNAME, AMOUNT, NARRATION", f, t, filt=True)
    out = []
    for v in root.iter("VOUCHER"):
        led = defaultdict(float)
        for tag in ("LEDGERENTRIES.LIST", "ALLLEDGERENTRIES.LIST"):
            for le in v.findall(tag):
                n = txt(le, "LEDGERNAME")
                if n: led[n] += amt(txt(le, "AMOUNT"))
        for ie in v.findall("ALLINVENTORYENTRIES.LIST") + v.findall("INVENTORYENTRIES.LIST"):
            for aa in ie.findall("ACCOUNTINGALLOCATIONS.LIST"):
                n = txt(aa, "LEDGERNAME")
                if n: led[n] += amt(txt(aa, "AMOUNT"))
        items = []
        for ie in v.findall("ALLINVENTORYENTRIES.LIST") + v.findall("INVENTORYENTRIES.LIST"):
            n = txt(ie, "STOCKITEMNAME")
            if n: items.append([n, qty(txt(ie, "BILLEDQTY") or txt(ie, "ACTUALQTY")), amt(txt(ie, "AMOUNT"))])
        out.append({"d": txt(v, "DATE"), "t": txt(v, "VOUCHERTYPENAME") or v.get("VCHTYPE", ""), "n": txt(v, "VOUCHERNUMBER"),
                    "p": txt(v, "PARTYLEDGERNAME"), "a": amt(txt(v, "AMOUNT")), "l": [[k, round(x, 2)] for k, x in led.items()], "i": items,
                    "nr": " ".join((txt(v, "NARRATION") or "").split())[:90]})
    return out

def list_companies(host, port, timeout=20):
    """Companies currently open in Tally, with change counters (AltVchId/AltMstId go up on every edit)."""
    T = Tally(host, port, None)
    root = T.collection("Company", "NAME, BASICCOMPANYFORMALNAME, STARTINGFROM, BOOKSFROM, ALTVCHID, ALTMSTID, GUID")
    out = []
    for el in root.iter("COMPANY"):
        n = el.get("NAME") or txt(el, "NAME")
        if not n: continue
        out.append({"name": n, "formal": txt(el, "BASICCOMPANYFORMALNAME"), "fy_from": txt(el, "STARTINGFROM"),
                    "books_from": txt(el, "BOOKSFROM"), "alt_vch": txt(el, "ALTVCHID"), "alt_mst": txt(el, "ALTMSTID"), "guid": txt(el, "GUID")})
    return out

PARTY_FIELDS = "NAME, PARENT, LEDGERPHONE, LEDGERMOBILE, EMAIL, PARTYGSTIN, LEDSTATENAME, BILLCREDITPERIOD, CREDITLIMIT, LEDGERCONTACT"
def pull_parties(T):
    """Phone / e-mail / GSTIN / credit terms of every ledger (kept only for customers and suppliers later)."""
    out = {}
    for el in T.collection("Ledger", PARTY_FIELDS).iter("LEDGER"):
        n = el.get("NAME")
        if not n: continue
        ph = re.sub(r"[^0-9+]", "", txt(el, "LEDGERMOBILE") or "") or re.sub(r"[^0-9+]", "", txt(el, "LEDGERPHONE") or "")
        rec = {"ph": ph, "em": txt(el, "EMAIL"), "gst": txt(el, "PARTYGSTIN"), "st": txt(el, "LEDSTATENAME"),
               "cp": txt(el, "BILLCREDITPERIOD"), "cl": abs(amt(txt(el, "CREDITLIMIT"))), "ct": txt(el, "LEDGERCONTACT")}
        rec = {k: v for k, v in rec.items() if v}
        if rec: out[n] = rec
    return out

def pull_pdc(T, today):
    """Post-dated vouchers (cheques dated in the future): money that will come in / go out."""
    root = T.collection("Voucher", "DATE, VOUCHERTYPENAME, VOUCHERNUMBER, PARTYLEDGERNAME, AMOUNT, ISPOSTDATED, NARRATION",
                        today + dt.timedelta(days=1), today + dt.timedelta(days=366))
    out = []
    for v in root.iter("VOUCHER"):
        if (txt(v, "ISPOSTDATED") or "").lower() != "yes": continue
        out.append({"d": str(tdate(txt(v, "DATE"))), "t": txt(v, "VOUCHERTYPENAME") or v.get("VCHTYPE", ""), "n": txt(v, "VOUCHERNUMBER"),
                    "p": txt(v, "PARTYLEDGERNAME"), "a": round(abs(amt(txt(v, "AMOUNT")))), "nr": " ".join((txt(v, "NARRATION") or "").split())[:90]})
    return sorted(out, key=lambda r: r["d"])

# ---------------------------------------------------------------- classification
PRIMARY = {"Sales Accounts", "Purchase Accounts", "Direct Expenses", "Direct Incomes", "Indirect Expenses", "Indirect Incomes",
           "Sundry Debtors", "Sundry Creditors", "Bank Accounts", "Bank OD A/c", "Cash-in-Hand", "Duties & Taxes",
           "Secured Loans", "Unsecured Loans", "Loans (Liability)", "Loans & Advances (Asset)", "Deposits (Asset)",
           "Stock-in-Hand", "Fixed Assets", "Investments", "Capital Account", "Reserves & Surplus", "Provisions",
           "Current Assets", "Current Liabilities", "Branch / Divisions", "Suspense A/c", "Misc. Expenses (ASSET)"}

def chain_reserved(group, groups):
    """List of reserved names up the chain, nearest first."""
    out, seen, g = [], set(), group
    while g and g not in seen:
        seen.add(g)
        info = groups.get(g)
        if not info: break
        if info["reserved"]: out.append(info["reserved"])
        g = info["parent"].strip()
    return out

def ledger_class(group, groups):
    ch = chain_reserved(group, groups)
    s = set(ch)
    for k in ("Bank OD A/c", "Bank Accounts", "Cash-in-Hand", "Sundry Debtors", "Sundry Creditors", "Duties & Taxes",
              "Secured Loans", "Unsecured Loans", "Loans (Liability)", "Sales Accounts", "Purchase Accounts", "Direct Expenses",
              "Direct Incomes", "Indirect Expenses", "Indirect Incomes", "Branch / Divisions", "Stock-in-Hand", "Fixed Assets",
              "Deposits (Asset)", "Loans & Advances (Asset)", "Capital Account", "Provisions", "Investments"):
        if k in s: return k
    return ch[0] if ch else "Other"

def vtype_base(name, vtypes):
    seen, n = set(), name
    while n and n not in seen:
        seen.add(n)
        info = vtypes.get(n)
        if not info: return n
        if info["reserved"]: return info["reserved"]
        n = info["parent"]
    return name

GOVT_WORDS = ("MANAGING DIRECTOR", "JKMSCL", "GMC", "SKIMS", "AIIMS", "HOSPITAL", "MEDICAL SUPERINTENDENT", "PRINCIPAL", "DIRECTOR",
              "CMO", "BMO", "SMHS", "GOVT", "GOVERNMENT", "DISTRICT", "HEALTH SERVICES", "COLLEGE", "ARMY", "DEFENCE", "CRPF", "BSF", "COMMAND")
def is_govt(name, group):
    s = (name + " " + (group or "")).upper()
    return any(w in s for w in GOVT_WORDS)

# ---------------------------------------------------------------- sync
def sync(args):
    today = dt.date.today()
    now = dt.datetime.now()
    T = Tally(args.host, args.port, args.company)
    log = []
    def step(msg): print("  " + msg); log.append(msg)

    groups = cache_get("groups", 1, today) or pull_groups(T); cache_put("groups", groups, today)
    vtypes = cache_get("vtypes", 1, today) or pull_vtypes(T); cache_put("vtypes", vtypes, today)
    step("groups {} / voucher types {}".format(len(groups), len(vtypes)))

    fys = fy_start(today)
    ly_today = today.replace(year=today.year - 1) if not (today.month == 2 and today.day == 29) else today - dt.timedelta(days=366)
    ly_fys = fy_start(ly_today)

    # ledger balances: now, same date last year, 13 month-ends
    led_now = pull_ledger_balances(T, fys, today)
    led_ly = cache_get("led_ly", 1, today) or pull_ledger_balances(T, ly_fys, ly_today); cache_put("led_ly", led_ly, today)
    months = []
    y, m = today.year, today.month
    for _ in range(13):
        months.append((y, m)); y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    months.reverse()
    month_led = {}
    for (y, m) in months:
        end = min(month_end(y, m), today)
        key = "led_{}{:02d}".format(y, m)
        is_recent = (y, m) in months[-2:]
        data = led_now if end == today else (None if is_recent else cache_get(key, 1, today))
        if data is None:
            data = pull_ledger_balances(T, fy_start(end), end); cache_put(key, data, today)
        month_led["{}-{:02d}".format(y, m)] = data
    step("ledgers {} (13 month-ends)".format(len(led_now)))

    # stock: now, 90 / 180 days ago, FY opening, last-year equivalents
    stock_now = pull_stock(T, fys, today)
    def stock_at(d, key):
        s = cache_get(key, 1, today)
        if s is None:
            try:
                s = pull_stock(T, fy_start(d), d)
            except Exception as e:
                step("stock at {} unavailable ({})".format(d, e)); s = {}
            cache_put(key, s, today)
        return s
    stock_90 = stock_at(today - dt.timedelta(days=90), "stock_90")
    stock_180 = stock_at(today - dt.timedelta(days=180), "stock_180")
    stock_fy_open = stock_at(fys - dt.timedelta(days=1), "stock_fyopen")
    stock_ly = stock_at(ly_today, "stock_ly")
    stock_ly_open = stock_at(ly_fys - dt.timedelta(days=1), "stock_lyopen")
    step("stock items {}".format(len(stock_now)))

    bills_r = pull_bills(T, "Bills Receivable", today)
    bills_p = pull_bills(T, "Bills Payable", today)
    step("bills receivable {} / payable {}".format(len(bills_r), len(bills_p)))

    vouchers = []
    for (y, m) in months:
        key = "vch_{}{:02d}".format(y, m)
        recent = (y, m) in months[-2:]
        v = None if recent else cache_get(key, 1, today)
        if v is None:
            v = pull_vouchers(T, dt.date(y, m, 1), min(month_end(y, m), today)); cache_put(key, v, today)
        vouchers.extend(v)
    step("vouchers {} (13 months)".format(len(vouchers)))

    data = compute(args, today, now, groups, vtypes, led_now, led_ly, month_led, months, stock_now, stock_90, stock_180,
                   stock_fy_open, stock_ly, stock_ly_open, bills_r, bills_p, vouchers)
    # extras: contact details for customers/suppliers, post-dated cheques. Optional - older Tally builds may not have every field.
    try:
        parties = cache_get("parties", 1, today) or pull_parties(T); cache_put("parties", parties, today)
        keep = {n for n, v in led_now.items() if ledger_class(v[0], groups) in ("Sundry Debtors", "Sundry Creditors")}
        data["parties"] = {n: r for n, r in parties.items() if n in keep}
        step("party contacts {}".format(sum(1 for r in data["parties"].values() if r.get("ph"))))
    except Exception as e:
        step("party contacts unavailable ({})".format(e)); data["parties"] = {}
    try:
        data["pdc"] = pull_pdc(T, today)
        step("post-dated vouchers {}".format(len(data["pdc"])))
    except Exception as e:
        step("post-dated vouchers unavailable ({})".format(e)); data["pdc"] = []
    data["chat"]["post_dated"] = [[r["d"], r["t"], r["p"], r["a"], r["nr"]] for r in data["pdc"][:100]]
    data["chat"]["columns"]["post_dated"] = ["date", "voucher_type", "party", "amount", "narration"]
    terms = {n: [r.get("cp", ""), round(r["cl"]) if r.get("cl") else None] for n, r in data["parties"].items() if r.get("cp") or r.get("cl")}
    if terms:
        data["chat"]["credit_terms"] = dict(list(terms.items())[:300])
        data["chat"]["columns"]["credit_terms"] = "party -> [credit period as set in Tally, credit limit in rupees]"
    data["meta"]["tally_calls"] = T.calls
    DATA_OUT.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    OUTPUT.write_text(html, encoding="utf-8")
    step("dashboard written: {}".format(OUTPUT.name))
    return data

# ---------------------------------------------------------------- compute
def fifo_age(parties, bills, sign, today, members=None):
    """parties: {name: outstanding>0}. bills: open bills (amount sign: Dr<0 for receivable).
    Allocates each party's real outstanding (ledger balance) to its NEWEST open bills first.
    Returns per-party aged bills + bucket totals + stale amount (bills open in Tally but already settled)."""
    by_party = defaultdict(list)
    for b in bills:
        a = b["amount"] * sign
        if a > 0: by_party[b["party"]].append(b)
    buckets = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "91-180": 0.0, "180+": 0.0, "No bill ref": 0.0}
    per_party, stale = {}, 0.0
    for p, owed in parties.items():
        bl = sorted(by_party.get(p, []), key=lambda b: tdate(b["date"]) or dt.date(1900, 1, 1), reverse=True)
        left, aged = owed, []
        for b in bl:
            a = b["amount"] * sign
            if left <= 0.5:
                stale += a; continue
            take = min(a, left); left -= take
            if take < a: stale += a - take
            bd = tdate(b["date"])
            age = (today - bd).days if bd else None
            dd = tdate(b.get("due") or "")
            od = (today - dd).days if dd else (b.get("od") or None)   # days past the agreed due date (credit period)
            aged.append({"ref": b["ref"], "date": bd.isoformat() if bd else b["date"], "amt": round(take), "age": age,
                         "od": od if od and od > 0 else 0})
        if left > 0.5: aged.append({"ref": "(no bill reference)", "date": "", "amt": round(left), "age": None})
        for a in aged:
            age = a["age"]
            k = "No bill ref" if age is None else "0-30" if age <= 30 else "31-60" if age <= 60 else "61-90" if age <= 90 else "91-180" if age <= 180 else "180+"
            buckets[k] += a["amt"]
        per_party[p] = aged
    for p, bl in by_party.items():
        if p not in parties and (members is None or p in members): stale += sum(b["amount"] * sign for b in bl)
    return per_party, buckets, stale

def compute(args, today, now, groups, vtypes, led_now, led_ly, month_led, months, stock_now, stock_90, stock_180,
            stock_fy_open, stock_ly, stock_ly_open, bills_r, bills_p, vouchers):
    cls_cache = {}
    def lcls(name, parent):
        if parent not in cls_cache: cls_cache[parent] = ledger_class(parent, groups)
        return cls_cache[parent]
    led_class = {n: lcls(n, v[0]) for n, v in led_now.items()}
    led_group = {n: v[0] for n, v in led_now.items()}

    def total(ledmap, klass, sign=-1):
        return sum(v[1] * sign for n, v in ledmap.items() if lcls(n, v[0]) == klass)

    # ---- cash & bank
    accounts = []
    for n, (g, bal) in led_now.items():
        c = led_class[n]
        if c in ("Bank Accounts", "Cash-in-Hand", "Bank OD A/c") and abs(bal) > 0.5:
            accounts.append({"name": n, "type": {"Bank Accounts": "Bank", "Cash-in-Hand": "Cash", "Bank OD A/c": "OD / CC"}[c], "bal": round(-bal)})
    accounts.sort(key=lambda a: (a["type"], -abs(a["bal"])))
    cash = sum(a["bal"] for a in accounts if a["type"] == "Cash")
    bank = sum(a["bal"] for a in accounts if a["type"] == "Bank")
    od = sum(-a["bal"] for a in accounts if a["type"] == "OD / CC")  # positive = OD used
    def liquid(lm):
        c = sum(-v[1] for n, v in lm.items() if lcls(n, v[0]) in ("Bank Accounts", "Cash-in-Hand"))
        o = sum(v[1] for n, v in lm.items() if lcls(n, v[0]) == "Bank OD A/c")
        return c, o

    # ---- parties
    debt_dr, debt_cr, cred_cr, cred_dr = {}, {}, {}, {}
    for n, (g, bal) in led_now.items():
        c = led_class[n]
        if c == "Sundry Debtors":
            if bal < -0.5: debt_dr[n] = -bal
            elif bal > 0.5: debt_cr[n] = bal
        elif c == "Sundry Creditors":
            if bal > 0.5: cred_cr[n] = bal
            elif bal < -0.5: cred_dr[n] = -bal
    debt_set = {n for n, c in led_class.items() if c == "Sundry Debtors"}
    cred_set = {n for n, c in led_class.items() if c == "Sundry Creditors"}
    recv_party, recv_buckets, recv_stale = fifo_age(debt_dr, bills_r, -1, today, debt_set)
    pay_party, pay_buckets, pay_stale = fifo_age(cred_cr, bills_p, 1, today, cred_set)
    recv_past_due = sum(a["amt"] for al in recv_party.values() for a in al if a.get("od"))
    pay_past_due = sum(a["amt"] for al in pay_party.values() for a in al if a.get("od"))
    to_collect = sum(debt_dr.values()); to_pay = sum(cred_cr.values())

    # ---- vouchers
    vbase = {}
    def vb(t):
        if t not in vbase: vbase[t] = vtype_base(t, vtypes)
        return vbase[t]
    mkeys = ["{}-{:02d}".format(y, m) for (y, m) in months]
    M = {k: defaultdict(float) for k in mkeys}
    cust_sales_12 = defaultdict(float); cust_sales_prev = defaultdict(float); cust_last = {}
    cust_recv = defaultdict(list)
    item_sales_90 = defaultdict(lambda: [0.0, 0.0]); item_sales_12 = defaultdict(float)
    supp_purch_12 = defaultdict(float)
    daily = defaultdict(lambda: defaultdict(float))
    recent_receipts = []
    cust_month = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0])); cust_coll_12 = defaultdict(float); cust_last_rcpt = {}
    supp_paid_12 = defaultdict(float); supp_last = {}
    item_qty_12 = defaultdict(float); item_last = {}
    recent_vch = []
    d90 = today - dt.timedelta(days=90); d30 = today - dt.timedelta(days=30)
    for v in vouchers:
        d = tdate(v["d"])
        if not d: continue
        mk = "{}-{:02d}".format(d.year, d.month)
        if mk not in M: continue
        base = vb(v["t"])
        if d >= today - dt.timedelta(days=30) and base in ("Sales", "Receipt", "Payment", "Purchase", "Credit Note", "Debit Note", "Journal", "Contra"):
            recent_vch.append([str(d), v["t"], v["p"], round(abs(v["a"])), v.get("nr", "")])
        for (ln, a) in v["l"]:
            c = lcls(ln, led_group.get(ln, ""))
            if c == "Sundry Debtors":
                if base == "Sales" and a < 0:
                    M[mk]["billed"] += -a; cust_sales_12[ln] += -a; cust_last[ln] = max(cust_last.get(ln, d), d)
                    cust_month[ln][mk][0] += -a
                    if d >= today - dt.timedelta(days=1): daily[str(d)]["billed"] += -a
                elif base == "Receipt" and a > 0:
                    M[mk]["collected"] += a; daily[str(d)]["collected"] += a
                    cust_month[ln][mk][1] += a; cust_coll_12[ln] += a; cust_last_rcpt[ln] = max(cust_last_rcpt.get(ln, d), d)
                    if d >= today - dt.timedelta(days=7): recent_receipts.append({"date": str(d), "party": ln, "amt": round(a)})
                elif base == "Credit Note" and a > 0: M[mk]["credit_notes"] += a
                elif base == "Journal" and a > 0: M[mk]["deductions"] += a
            elif c == "Sundry Creditors":
                if base == "Purchase" and a > 0:
                    M[mk]["purchased"] += a; supp_purch_12[ln] += a; supp_last[ln] = max(supp_last.get(ln, d), d)
                elif base == "Payment" and a < 0: M[mk]["paid"] += -a; daily[str(d)]["paid"] += -a; supp_paid_12[ln] += -a
            elif c == "Sales Accounts" and base in ("Sales", "Credit Note"):
                party_cls = lcls(v["p"], led_group.get(v["p"], ""))
                if party_cls == "Sundry Debtors" or party_cls == "Cash-in-Hand" or party_cls == "Bank Accounts":
                    M[mk]["net_sales"] += a  # Cr positive, credit note Dr negative
            elif c == "Purchase Accounts" and base in ("Purchase", "Debit Note"):
                M[mk]["net_purchases"] += -a
            elif c in ("Indirect Expenses", "Direct Expenses") and base not in ("Sales", "Purchase"):
                pass
        if base == "Sales":
            party_cls = lcls(v["p"], led_group.get(v["p"], ""))
            if party_cls in ("Sundry Debtors", "Cash-in-Hand", "Bank Accounts"):
                for (it, q, a) in v["i"]:
                    val = abs(a)
                    item_sales_12[it] += val; item_qty_12[it] += abs(q); item_last[it] = max(item_last.get(it, d), d)
                    if d >= d90:
                        item_sales_90[it][0] += val; item_sales_90[it][1] += abs(q)

    # ---- P&L FYTD vs LY same period (from ledger balances; Cr positive)
    def pl(lm, stock_open, stock_close):
        s = {k: sum(v[1] for n, v in lm.items() if lcls(n, v[0]) == k) for k in
             ("Sales Accounts", "Direct Incomes", "Purchase Accounts", "Direct Expenses", "Indirect Expenses", "Indirect Incomes")}
        so = sum(x[2] for x in stock_open.values()) if stock_open else 0.0
        sc = sum(x[2] for x in stock_close.values())
        sales = s["Sales Accounts"]; purch = -s["Purchase Accounts"]; dexp = -s["Direct Expenses"]; dinc = s["Direct Incomes"]
        cogs = so + purch + dexp - sc
        gp = sales + dinc - cogs
        iexp = -s["Indirect Expenses"]; iinc = s["Indirect Incomes"]
        return {"sales": round(sales), "purchases": round(purch), "direct_exp": round(dexp), "stock_open": round(so), "stock_close": round(sc),
                "cogs": round(cogs), "gross_profit": round(gp), "gp_pct": round(gp / sales * 100, 1) if sales else None,
                "indirect_exp": round(iexp), "other_income": round(iinc), "net_profit": round(gp - iexp + iinc)}
    pl_now = pl(led_now, stock_fy_open, stock_now)
    pl_ly = pl(led_ly, stock_ly_open, stock_ly) if stock_ly_open else None

    # top expenses FYTD
    exp_led = sorted(((n, -v[1]) for n, v in led_now.items() if lcls(n, v[0]) == "Indirect Expenses" and -v[1] > 0), key=lambda x: -x[1])[:8]

    # ---- monthly series from month-end ledgers
    series = []
    prev_cum = None
    for k in mkeys:
        lm = month_led[k]
        c, o = liquid(lm)
        deb = sum(-v[1] for n, v in lm.items() if lcls(n, v[0]) == "Sundry Debtors" and v[1] < 0)
        cre = sum(v[1] for n, v in lm.items() if lcls(n, v[0]) == "Sundry Creditors" and v[1] > 0)
        y, m = int(k[:4]), int(k[5:])
        row = {"m": k, "cash_bank": round(c), "od": round(o), "debtors": round(deb), "creditors": round(cre)}
        row.update({x: round(M[k][x]) for x in ("billed", "collected", "credit_notes", "deductions", "purchased", "paid", "net_sales", "net_purchases")})
        series.append(row)

    # ---- KPIs
    billed_12 = sum(r["billed"] for r in series[-12:]); purch_12 = sum(r["purchased"] for r in series[-12:])
    stock_value = sum(x[2] for x in stock_now.values())
    net_purch_12 = sum(r["net_purchases"] for r in series[-12:])
    dso = round(to_collect / billed_12 * 365) if billed_12 else None
    dpo = round(to_pay / purch_12 * 365) if purch_12 else None
    dio = round(stock_value / net_purch_12 * 365) if net_purch_12 else None
    gst = sum(v[1] for n, v in led_now.items() if led_class[n] == "Duties & Taxes")
    loans = sorted(({"name": n, "bal": round(v[1])} for n, v in led_now.items() if led_class[n] in ("Secured Loans", "Unsecured Loans", "Loans (Liability)") and v[1] > 0.5), key=lambda x: -x["bal"])

    # ---- customers
    custs = []
    for p, owed in sorted(debt_dr.items(), key=lambda x: -x[1]):
        aged = recv_party.get(p, [])
        known = [a for a in aged if a["age"] is not None]
        oldest = max((a["age"] for a in known), default=None)
        over90 = sum(a["amt"] for a in known if a["age"] > 90)
        custs.append({"name": p, "group": led_group.get(p, ""), "govt": is_govt(p, led_group.get(p, "")), "owed": round(owed),
                      "over90": round(over90), "oldest": oldest, "sales12": round(cust_sales_12.get(p, 0)),
                      "last_sale": str(cust_last[p]) if p in cust_last else None, "bills": aged[:40], "nbills": len(aged)})
    govt_owed = sum(c["owed"] for c in custs if c["govt"]); pvt_owed = to_collect - govt_owed
    top_sales = sorted(cust_sales_12.items(), key=lambda x: -x[1])[:12]
    tot_sales_12 = sum(cust_sales_12.values())
    inactive = []
    for p, last in cust_last.items():
        if last < d90 and cust_sales_12[p] > 0:
            inactive.append({"name": p, "last_sale": str(last), "sales12": round(cust_sales_12[p]), "owed": round(debt_dr.get(p, 0))})
    inactive.sort(key=lambda x: -x["sales12"])
    advances = sorted(({"name": n, "amt": round(a), "group": led_group.get(n, "")} for n, a in debt_cr.items()), key=lambda x: -x["amt"])

    # ---- suppliers
    supps = []
    for p, owe in sorted(cred_cr.items(), key=lambda x: -x[1])[:25]:
        aged = pay_party.get(p, [])
        known = [a for a in aged if a["age"] is not None]
        supps.append({"name": p, "owe": round(owe), "over90": round(sum(a["amt"] for a in known if a["age"] > 90)),
                      "oldest": max((a["age"] for a in known), default=None), "purch12": round(supp_purch_12.get(p, 0)), "bills": aged[:25]})

    # ---- stock
    by_group = defaultdict(float)
    for n, (g, q, v, u) in stock_now.items(): by_group[g or "Ungrouped"] += v
    no_move_180, no_move_90 = [], 0.0
    for n, (g, q, v, u) in stock_now.items():
        if v <= 0 or q <= 0: continue
        q180 = stock_180.get(n, [None, None])[1]; q90 = stock_90.get(n, [None, None])[1]
        if q90 is not None and abs(q90 - q) < 1e-6: no_move_90 += v
        if q180 is not None and abs(q180 - q) < 1e-6:
            no_move_180.append({"name": n, "group": g, "qty": q, "unit": u, "value": round(v)})
    no_move_180.sort(key=lambda x: -x["value"])
    top_items = sorted(({"name": n, "value": round(x[0]), "qty": x[1], "group": stock_now.get(n, [""])[0]} for n, x in item_sales_90.items()), key=lambda x: -x["value"])[:15]
    stock_vals = [x[2] for x in stock_now.values() if x[2] > 0]

    # ---- brief (rule-based, plain language)
    brief = []
    cur, prev = series[-1], series[-2]
    yd = str(today - dt.timedelta(days=1)); td = str(today)
    tb = daily[td]; ybd = daily[yd]
    if tb["billed"] or tb["collected"] or ybd["billed"] or ybd["collected"]:
        brief.append({"tone": "info", "text": "Yesterday: billed {B} and collected {C}. Today so far: billed {b}, collected {c}.",
                      "vals": {"B": ybd["billed"], "C": ybd["collected"], "b": tb["billed"], "c": tb["collected"]}})
    ly_same = next((r for r in series if r["m"] == "{}-{:02d}".format(today.year - 1, today.month)), None)
    if pl_ly and pl_now["sales"] and pl_ly["sales"]:
        ch = (pl_now["sales"] - pl_ly["sales"]) / abs(pl_ly["sales"]) * 100
        brief.append({"tone": "good" if ch >= 0 else "bad", "text": "Sales this financial year are {S}, {dir} {pct}% vs the same period last year ({L}).",
                      "vals": {"S": pl_now["sales"], "L": pl_ly["sales"]}, "dir": "up" if ch >= 0 else "down", "pct": round(abs(ch))})
    if cur["collected"] < cur["billed"] * 0.6 and cur["billed"] > 0:
        brief.append({"tone": "bad", "text": "This month you have billed {B} but collected only {C}. Cash is getting stuck with customers.",
                      "vals": {"B": cur["billed"], "C": cur["collected"]}})
    if recv_past_due > 0 and to_collect > 0:
        n_pd = sum(1 for al in recv_party.values() if any(x.get("od") for x in al))
        brief.append({"tone": "warn", "text": "{P} is past the due date you agreed with customers (" + str(n_pd) + " customers). Chase these first.",
                      "vals": {"P": recv_past_due}})
    old = recv_buckets["180+"]
    if old > 0:
        n_old = sum(1 for c in custs if (c["oldest"] or 0) > 180)
        brief.append({"tone": "bad", "text": "{O} of customer money is more than 6 months old, spread across " + str(n_old) + " customers.", "vals": {"O": old}})
    big = max(recent_receipts, key=lambda r: r["amt"]) if recent_receipts else None
    if big and big["amt"] >= 10000:
        brief.append({"tone": "good", "text": "Biggest payment this week: {A} from " + big["party"] + " on " + big["date"] + ".", "vals": {"A": big["amt"]}})
    if recv_stale > 1e5:
        brief.append({"tone": "warn", "text": "Tally still shows {S} of old customer bills as unpaid that are actually settled. Ask the accountant to clean up bill-wise entries.", "vals": {"S": recv_stale}})

    # ---- compact data pack for the "Ask" assistant
    def age_info(p, aged):
        known = [a for a in aged if a["age"] is not None]
        return (max((a["age"] for a in known), default=None), round(sum(a["amt"] for a in known if a["age"] > 90)))
    cparts = set(debt_dr) | set(cust_sales_12) | set(cust_coll_12) | set(debt_cr)
    chat_cust = []
    for p in cparts:
        oldest, o90 = age_info(p, recv_party.get(p, []))
        bal = debt_dr.get(p, 0) - debt_cr.get(p, 0)
        chat_cust.append([p, "govt" if is_govt(p, led_group.get(p, "")) else "pvt", round(bal), o90, oldest, round(cust_sales_12.get(p, 0)),
                          round(cust_coll_12.get(p, 0)), str(cust_last.get(p, "")) or None, str(cust_last_rcpt.get(p, "")) or None])
    chat_cust.sort(key=lambda r: -(abs(r[2]) + r[5]))
    top_active = [r[0] for r in sorted(chat_cust, key=lambda r: -(r[5] + r[6]))[:60] if r[5] + r[6] > 0]
    cust_monthly = {p: {k: [round(x[0]), round(x[1])] for k, x in cust_month[p].items() if x[0] or x[1]} for p in top_active}
    bills_top = {p: [[b["ref"], b["date"], b["amt"], b["age"], b.get("od", 0)] for b in recv_party.get(p, [])[:15]] for p in [r[0] for r in chat_cust[:40] if r[2] > 0]}
    sparts = set(cred_cr) | set(supp_purch_12) | set(supp_paid_12) | set(cred_dr)
    chat_supp = []
    for p in sparts:
        oldest, o90 = age_info(p, pay_party.get(p, []))
        chat_supp.append([p, round(cred_cr.get(p, 0) - cred_dr.get(p, 0)), o90, oldest, round(supp_purch_12.get(p, 0)), round(supp_paid_12.get(p, 0)), str(supp_last.get(p, "")) or None])
    chat_supp.sort(key=lambda r: -(abs(r[1]) + r[4]))
    chat_items = []
    for it in sorted(item_sales_12, key=lambda x: -item_sales_12[x])[:150]:
        st = stock_now.get(it, ["", 0, 0, ""])
        chat_items.append([it, st[0], round(item_sales_12[it]), round(item_qty_12[it], 2), str(item_last.get(it, "")), st[1], round(st[2])])
    nm = {x["name"] for x in no_move_180}
    chat_stock = [[n, g, q, u, round(v), n in nm] for n, (g, q, v, u) in sorted(stock_now.items(), key=lambda kv: -kv[1][2]) if v >= 50000][:150]
    def exp_rows(lm):
        return {n: round(-v[1]) for n, v in lm.items() if lcls(n, v[0]) in ("Indirect Expenses", "Direct Expenses") and abs(v[1]) >= 1}
    e_now, e_ly = exp_rows(led_now), exp_rows(led_ly)
    chat_exp = sorted(([n, e_now.get(n, 0), e_ly.get(n, 0)] for n in set(e_now) | set(e_ly)), key=lambda r: -r[1])[:60]
    chat = {
        "columns": {
            "customers": ["name", "type", "balance_owed_to_us(+)/advance_held(-)", "older_than_90d", "oldest_bill_age_days", "billed_last_12m_incl_gst", "collected_last_12m", "last_bill_date", "last_receipt_date"],
            "customer_monthly": "month -> [billed, collected] for the 60 most active customers",
            "customer_bills": "customer -> open bills [ref, bill_date, amount_outstanding, age_days, days_past_due_date] (newest-first matching to ledger balance; due date = bill date + credit period in Tally)",
            "suppliers": ["name", "we_owe(+)/advance_paid(-)", "older_than_90d", "oldest_bill_age_days", "purchased_last_12m_incl_gst", "paid_last_12m", "last_purchase_date"],
            "items_sold": ["item", "stock_group", "sales_value_12m", "qty_sold_12m", "last_sold", "stock_qty_now", "stock_value_now"],
            "stock": ["item", "group", "qty", "unit", "value", "not_moved_180d"],
            "expenses": ["ledger", "this_fy_to_date", "same_period_last_year"],
            "recent_vouchers": ["date", "voucher_type", "party", "amount", "narration"],
        },
        "customers": chat_cust[:400], "customer_monthly": cust_monthly, "customer_bills": bills_top,
        "suppliers": chat_supp[:200], "items_sold": chat_items, "stock": chat_stock, "expenses": chat_exp,
        "recent_vouchers": sorted(recent_vch, reverse=True)[:400],
    }

    return {
        "chat": chat,
        "meta": {"company": args.company, "synced_at": now.isoformat(timespec="seconds"), "today": str(today), "fy_start": str(fy_start(today)),
                 "od_limit": args.od_limit, "every": args.every},
        "hero": {"cash": round(cash), "bank": round(bank), "od": round(od), "to_collect": round(to_collect),
                 "overdue_90": round(recv_buckets["91-180"] + recv_buckets["180+"]), "to_pay": round(to_pay),
                 "pay_overdue_90": round(pay_buckets["91-180"] + pay_buckets["180+"]), "stock": round(stock_value),
                 "advances_from_customers": round(sum(debt_cr.values())), "advances_to_suppliers": round(sum(cred_dr.values())),
                 "past_due": round(recv_past_due), "pay_past_due": round(pay_past_due), "working_capital": round(cash + bank - od + to_collect + stock_value - to_pay - sum(debt_cr.values()))},
        "brief": brief, "accounts": accounts, "series": series,
        "recv": {"buckets": {k: round(v) for k, v in recv_buckets.items()}, "stale": round(recv_stale), "govt": round(govt_owed), "private": round(pvt_owed),
                 "customers": custs[:60], "n_customers": len(custs), "advances": advances[:15], "recent_receipts": sorted(recent_receipts, key=lambda r: -r["amt"])[:10]},
        "pay": {"buckets": {k: round(v) for k, v in pay_buckets.items()}, "stale": round(pay_stale), "suppliers": supps, "n_suppliers": len(cred_cr)},
        "pl": {"fytd": pl_now, "ly": pl_ly, "top_expenses": [{"name": n, "amt": round(a)} for n, a in exp_led]},
        "kpi": {"dso": dso, "dpo": dpo, "dio": dio, "ccc": (dso + dio - dpo) if None not in (dso, dio, dpo) else None, "billed_12": round(billed_12),
                "top5_share": round(sum(x[1] for x in top_sales[:5]) / tot_sales_12 * 100, 1) if tot_sales_12 else None, "gst_net": round(gst)},
        "customers_sales": [{"name": n, "sales12": round(v), "share": round(v / tot_sales_12 * 100, 1) if tot_sales_12 else 0, "owed": round(debt_dr.get(n, 0))} for n, v in top_sales],
        "inactive": inactive[:12],
        "stock": {"value": round(stock_value), "items": len(stock_vals), "by_group": [{"name": g, "value": round(v)} for g, v in sorted(by_group.items(), key=lambda x: -x[1])[:10]],
                  "no_move_180_value": round(sum(x["value"] for x in no_move_180)), "no_move_180": no_move_180[:20], "no_move_90_value": round(no_move_90),
                  "top_items_90": top_items},
        "loans": loans[:12],
    }


# ---------------------------------------------------------------- cloud copy (link for family phones)
# After each sync the laptop uploads the numbers (and the dashboard page when it changes) to a
# private cloud store. The shared link https://tms-pulse.vercel.app/?k=... opens it on any phone.
CLOUD_CFG = HERE / "cloud_config.txt"
_CLOUD = {"page_hash": None}

def cloud_config():
    cfg = {}
    if CLOUD_CFG.exists():
        for line in CLOUD_CFG.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1); cfg[k.strip()] = v.strip()
    return cfg if cfg.get("push_key") and cfg.get("supabase_url") and cfg.get("supabase_key") else None

def cloud_page(cfg):
    """The shared (cloud) version of the dashboard: loads its numbers from the cloud with the link's key."""
    t = TEMPLATE.read_text(encoding="utf-8")
    loader = r"""let D = null;
const TMS_SB='__SB_URL__', TMS_SBK='__SB_KEY__';
let TMS_KEY=new URLSearchParams(location.search).get('k');
try{ if(TMS_KEY) localStorage.setItem('tms-k',TMS_KEY); else TMS_KEY=localStorage.getItem('tms-k'); }catch(e){}
async function tmsFetch(){
  const r=await fetch(TMS_SB+'/rest/v1/rpc/get_snapshot',{method:'POST',cache:'no-store',
    headers:{'apikey':TMS_SBK,'Content-Type':'application/json'},body:JSON.stringify({view_key:TMS_KEY||''})});
  if(!r.ok){ let c=''; try{c=(await r.json()).code||''}catch(e){}
    throw new Error(r.status===401||r.status===403||c==='42501'?'forbidden':'net'); }
  const j=await r.json(); return j&&j.data;
}
function tmsMsg(h){document.body.innerHTML='<div style="font-family:system-ui,sans-serif;max-width:420px;margin:18vh auto;padding:0 20px;text-align:center;color:#334155">'+h+'</div>';}
tmsFetch().then(d=>{ if(!d){tmsMsg('<h2>No data yet</h2><p>The first sync from the office laptop has not arrived. Try again in a few minutes.</p>');return;} D=d; tmsMain(); })
  .catch(e=>{ tmsMsg(e.message==='forbidden'
     ? '<h2>Private dashboard</h2><p>Open it with the full link you were sent (it ends with <b>?k=</b>…).</p>'
     : '<h2>Could not load</h2><p>Check the internet connection and pull down to refresh.</p>'); });
function tmsMain(){""".replace("__SB_URL__", cfg["supabase_url"]).replace("__SB_KEY__", cfg["supabase_key"])
    t = t.replace("let D = /*__DATA__*/null;\n(function(){", loader, 1)
    i = t.rindex("})();\n</script>"); t = t[:i] + "}\n</script>" + t[i + len("})();\n</script>"):]
    t = t.replace("const r=await fetch('/data.json',{cache:'no-store'});if(!r.ok)return;const j=await r.json();",
                  "const j=await tmsFetch();if(!j)return;", 1)
    # Ask TMS on the family link: questions go to the tms-chat function on the cloud store, which holds
    # the API key and only answers when the link's secret key is sent along.
    t = t.replace("try{const r=await fetch('/api/status',{cache:'no-store'});const j=await r.json(); b=j.ai?{kind:'local',model:j.model}:{kind:'nokey'};}catch(e){}",
                  "b={kind:'local',model:'cloud'};", 1)
    t = t.replace("const res=await fetch('/api/chat',{method:'POST',signal:ctl.signal,headers:{'Content-Type':'application/json','X-TMS':'1'},body:JSON.stringify({system:system(),messages:ask.turns})});",
                  "const res=await fetch(TMS_SB+'/functions/v1/tms-chat',{method:'POST',signal:ctl.signal,headers:{'Content-Type':'application/json'},body:JSON.stringify({view_key:TMS_KEY,system:system(),messages:ask.turns})});", 1)
    t = t.replace("</head>", '<meta name="robots" content="noindex,nofollow">\n<meta name="referrer" content="no-referrer">\n</head>', 1)
    return t

def _rpc(cfg, fn, body):
    req = urllib.request.Request(cfg["supabase_url"].rstrip("/") + "/rest/v1/rpc/" + fn,
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"apikey": cfg["supabase_key"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()

def cloud_push(data):
    cfg = cloud_config()
    if not cfg: return
    import hashlib
    page = cloud_page(cfg)
    h = hashlib.sha256(page.encode("utf-8")).hexdigest()
    if h != _CLOUD["page_hash"]:
        _rpc(cfg, "push_page", {"push_key": cfg["push_key"], "page": page}); _CLOUD["page_hash"] = h
    _rpc(cfg, "push_snapshot", {"push_key": cfg["push_key"], "payload": data})
    print("  uploaded to the family link ({})".format(cfg.get("view_link", "cloud")))

# ---------------------------------------------------------------- WhatsApp morning brief (WhatsApp Cloud API)
WA_CFG = HERE / "whatsapp_config.txt"
WA_SENT = CACHE / "whatsapp_sent.txt"

def wa_config():
    cfg = {}
    if WA_CFG.exists():
        for line in WA_CFG.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); cfg[k.strip().lower()] = v.strip()
    cfg.setdefault("template", "tms_morning_brief"); cfg.setdefault("language", "en")
    cfg.setdefault("send_time", "08:00"); cfg.setdefault("api_version", "v23.0")
    cfg["ready"] = all(cfg.get(k) for k in ("token", "phone_number_id", "to"))
    return cfg

def inr(v):
    a = abs(v or 0); sgn = "-" if (v or 0) < 0 else ""
    if a >= 1e7: return "{}\u20b9{:.2f} Cr".format(sgn, a / 1e7).replace(".00 Cr", " Cr")
    if a >= 1e5: return "{}\u20b9{:.1f} L".format(sgn, a / 1e5).replace(".0 L", " L")
    if a >= 1e3: return "{}\u20b9{:.0f}K".format(sgn, a / 1e3)
    return "{}\u20b9{:.0f}".format(sgn, a)

def nice_name(n):
    out = []
    for w in n.split():
        core = w.strip("()&.,/-")
        out.append(w if (core.isalpha() and len(core) <= 4) or not core.isalpha() and len(core) <= 4 else w.title())
    return " ".join(out)

def wa_params(d):
    """Six one-line values for the approved template (WhatsApp does not allow line breaks inside them)."""
    h, pl, ly = d["hero"], d["pl"]["fytd"], d["pl"]["ly"]
    net = h["cash"] + h["bank"] - h["od"]
    when = dt.datetime.fromisoformat(d["meta"]["synced_at"]).strftime("%a %d %b, %I:%M %p")
    ch = ""
    if ly and ly.get("sales"):
        c = round((pl["sales"] - ly["sales"]) / abs(ly["sales"]) * 100); ch = " ({}{}% vs last year)".format("+" if c >= 0 else "", c)
    top = sorted(d["recv"]["customers"], key=lambda c: -c["over90"])[:3]
    follow = "; ".join("{} {}".format(nice_name(c["name"]), inr(c["owed"])) for c in top) or "nothing urgent"
    return [when,
            "{} (cash + bank {}, overdraft used {})".format(inr(net), inr(h["cash"] + h["bank"]), inr(h["od"])),
            "{} ({} over 3 months)".format(inr(h["to_collect"]), inr(h["overdue_90"])),
            inr(h["to_pay"]),
            inr(pl["sales"]) + ch,
            follow[:900]]

def wa_send(cfg, d):
    params = wa_params(d)
    url = "https://graph.facebook.com/{}/{}/messages".format(cfg["api_version"], cfg["phone_number_id"])
    results = []
    for to in [x.strip().replace("+", "").replace(" ", "") for x in cfg["to"].split(",") if x.strip()]:
        body = {"messaging_product": "whatsapp", "to": to, "type": "template",
                "template": {"name": cfg["template"], "language": {"code": cfg["language"]},
                             "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]}}
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Authorization": "Bearer " + cfg["token"], "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                results.append((to, True, r.read().decode("utf-8", "replace")[:200]))
        except urllib.error.HTTPError as e:
            results.append((to, False, e.read().decode("utf-8", "replace")[:300]))
        except Exception as e:
            results.append((to, False, str(e)))
    return results

def maybe_send_brief(d, force=False):
    cfg = wa_config()
    if not cfg["ready"]:
        if force: print("  ! WhatsApp is not set up: fill token, phone_number_id and to in whatsapp_config.txt")
        return
    now = dt.datetime.now(); today = str(now.date())
    try:
        hh, mm = [int(x) for x in cfg["send_time"].split(":")]
    except ValueError:
        hh, mm = 8, 0
    if not force:
        if WA_SENT.exists() and WA_SENT.read_text().strip() == today: return
        if now < now.replace(hour=hh, minute=mm, second=0) or now.hour >= 12: return
    for to, ok, info in wa_send(cfg, d):
        print("  WhatsApp brief to {}: {}".format(to, "sent" if ok else "FAILED " + info))
        if ok and not force: WA_SENT.write_text(today)

# ---------------------------------------------------------------- local web server (dashboard + Ask)
import http.server, os, socketserver, threading

def ai_key():
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not k and AI_KEY_FILE.exists():
        for line in AI_KEY_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip() and not line.strip().startswith("#"):
                k = line.strip(); break
    return k

import secrets, socket
TOKEN_FILE = CACHE / "phone_key.txt"

def phone_token():
    CACHE.mkdir(exist_ok=True)
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(t) >= 20: return t
    t = secrets.token_urlsafe(18); TOKEN_FILE.write_text(t, encoding="utf-8"); return t

def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("10.255.255.255", 1)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return None

_TS = {"t": 0, "url": None}
def tailscale_url():
    """https://<laptop>.<tailnet>.ts.net/ if Tailscale is installed and logged in (for the 'anywhere' link)."""
    if time.time() - _TS["t"] < 60: return _TS["url"]
    _TS["t"], url = time.time(), None
    try:
        import shutil, subprocess
        exe = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
        if os.path.exists(exe) or shutil.which("tailscale"):
            out = subprocess.run([exe, "status", "--json"], capture_output=True, timeout=8,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            st = json.loads(out.decode("utf-8", "replace") or "{}")
            name = ((st.get("Self") or {}).get("DNSName") or "").rstrip(".")
            if name and st.get("BackendState") == "Running": url = "https://{}/".format(name)
    except Exception:
        url = None
    _TS["url"] = url
    return url

class DashHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _save_key(self):
        # Paste-your-key box on the laptop dashboard. Laptop only (never from a phone or another site).
        if not self._local() or self.headers.get("X-TMS") != "1":
            return self._send(403, '{"error":"Only from the dashboard on this laptop"}')
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", "0") or 0), 4096)).decode("utf-8"))
            key = str(body.get("key", "")).strip()
        except Exception:
            return self._send(400, '{"error":"Bad request"}')
        if not re.fullmatch(r"sk-ant-[A-Za-z0-9_\-]{20,300}", key):
            return self._send(400, json.dumps({"error": "That does not look like an Anthropic key (sk-ant-...)"}))
        try:   # check the key with Anthropic before saving
            req = urllib.request.Request("https://api.anthropic.com/v1/models?limit=1",
                                         headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
            urllib.request.urlopen(req, timeout=20).close()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return self._send(400, json.dumps({"error": "Anthropic says this key is not valid. Copy it again from console.anthropic.com."}))
        except Exception:
            pass   # offline: save anyway, it will be used when the internet is back
        AI_KEY_FILE.write_text("# Anthropic API key for Ask TMS. Keep this file private.\n" + key + "\n", encoding="utf-8")
        print("  Ask assistant: ON (key saved from the dashboard)")
        return self._send(200, '{"ok":true}')
    def _local(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return self.client_address[0] in ("127.0.0.1", "::1") and host in ("127.0.0.1", "localhost")
    def _cookie_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "tms_k": return v
        return ""
    def _host_ok(self):
        # The laptop itself, or a phone on the office Wi-Fi that has the secret key (from the QR link)
        if self._local(): return True
        tok = self.server.token
        return bool(tok) and secrets.compare_digest(self._cookie_token(), tok)
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        path, _, query = self.path.partition("?")
        tok = self.server.token
        if not self._local() and tok and path == "/" and ("k=" + tok) in query.split("&"):
            # first visit from the phone link: remember the key in a cookie, then load the page without the key in the URL
            self.send_response(302); self.send_header("Location", "/")
            self.send_header("Set-Cookie", "tms_k={}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Lax".format(tok)); self.end_headers(); return
        if not self._host_ok():
            return self._send(403, "<p style='font-family:sans-serif;padding:30px'>This dashboard is private. Open it with the full link you were sent (it ends with ?k=...).</p>", "text/html; charset=utf-8")
        if path == "/api/phone":
            if not self._local(): return self._send(403, "{}")
            ip, ts = lan_ip(), tailscale_url()
            return self._send(200, json.dumps({"url": "http://{}:{}/?k={}".format(ip, PORT, tok) if ip and tok else None,
                                               "remote": ((cloud_config() or {}).get("view_link")) or ("{}?k={}".format(ts, tok) if ts and tok else None)}))
        if path in ("/", "/index.html"):
            if not OUTPUT.exists(): return self._send(503, "<p style='font-family:sans-serif;padding:40px'>First sync is still running. Refresh in a minute.</p>", "text/html; charset=utf-8")
            return self._send(200, OUTPUT.read_bytes(), "text/html; charset=utf-8")
        if path == "/data.json":
            return self._send(200, DATA_OUT.read_bytes() if DATA_OUT.exists() else b"{}")
        if path == "/api/status":
            return self._send(200, json.dumps({"ai": bool(ai_key()), "model": AI_MODEL}))
        self._send(404, "{}")
    def do_POST(self):
        if self.path == "/api/key":
            return self._save_key()
        if self.path == "/api/quit":
            # A newer copy started from START_DASHBOARD asks this one to hand over (laptop only).
            if not self._local() or self.headers.get("X-TMS") != "1": return self._send(403, "{}")
            self._send(200, '{"ok":true}')
            print("  A newer sync window took over - closing this one.")
            threading.Timer(0.5, lambda: os._exit(0)).start(); return
        # Only the dashboard page itself may call this: same host + custom header (blocks other websites)
        if not self._host_ok() or self.headers.get("X-TMS") != "1" or self.path != "/api/chat":
            return self._send(403, '{"error":"forbidden"}')
        key = ai_key()
        if not key: return self._send(400, '{"error":"no api key"}')
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)).decode("utf-8"))
            msgs = [m for m in body.get("messages", []) if m.get("role") in ("user", "assistant") and m.get("content")][-10:]
            while msgs and msgs[0].get("role") != "user": msgs = msgs[1:]
            # Low-cost setup: the business data (system prompt) is cached, so follow-up questions within
            # 5 minutes pay ~5% for it; the conversation is cached too; effort "medium" = less over-thinking.
            payload = {"model": AI_MODEL, "max_tokens": 8000, "stream": True,
                       "system": [{"type": "text", "text": str(body.get("system", ""))[:300000], "cache_control": {"type": "ephemeral"}}],
                       "cache_control": {"type": "ephemeral"},
                       "output_config": {"effort": AI_EFFORT},
                       "messages": msgs}
        except Exception:
            return self._send(400, '{"error":"bad request"}')
        req = urllib.request.Request(os.environ.get("TMS_AI_URL", "https://api.anthropic.com/v1/messages"), data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            resp = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            return self._send(502, json.dumps({"error": "Anthropic API error {}: {}".format(e.code, detail)}))
        except Exception as e:
            return self._send(502, json.dumps({"error": "Could not reach Anthropic API: {}".format(e)}))
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-store"); self.end_headers()
        use = {}
        try:
            for line in resp:
                self.wfile.write(line); self.wfile.flush()
                if b'"usage"' in line and line.startswith(b"data:"):
                    try:
                        ev = json.loads(line[5:].decode("utf-8"))
                        u = (ev.get("message") or {}).get("usage") or ev.get("usage") or {}
                        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"):
                            if u.get(k) is not None: use[k] = u[k]
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            resp.close()
            if use:
                print("  Ask TMS: {:,} new + {:,} cached input tokens, {:,} output tokens".format(
                    use.get("input_tokens", 0) + use.get("cache_creation_input_tokens", 0),
                    use.get("cache_read_input_tokens", 0), use.get("output_tokens", 0)))

class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = (os.name != "nt")  # on Windows this would let two copies share the port

def start_server(lan=True):
    try:
        srv = _Server(("0.0.0.0" if lan else "127.0.0.1", PORT), DashHandler)
    except OSError:
        print("  (dashboard server already running on port {} - using that one)".format(PORT)); return None
    srv.token = phone_token() if lan else None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("  Dashboard: http://127.0.0.1:{}/   Ask assistant: {}".format(PORT, "ON" if ai_key() else "OFF - add your key to ai_key.txt"))
    if lan:
        print("  Phone (same Wi-Fi): click 'Open on phone' on the dashboard and scan the QR code.")
        print("  If Windows asks about Python and networks, choose 'Private networks' and Allow.")
    return srv

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost"); ap.add_argument("--port", default=9000, type=int)
    ap.add_argument("--company", default="TECHNO MED SERVICES"); ap.add_argument("--every", default=0, type=int, help="minutes between syncs (0 = once)")
    ap.add_argument("--od-limit", default=0, type=float, help="sanctioned OD/CC limit in rupees (Tally does not store it)")
    ap.add_argument("--open", action="store_true", help="open the dashboard in the browser after the first sync")
    ap.add_argument("--no-server", action="store_true", help="do not start the local dashboard server")
    ap.add_argument("--no-phone", action="store_true", help="laptop only: do not allow phones on the Wi-Fi to open the dashboard")
    ap.add_argument("--test-whatsapp", action="store_true", help="sync once and send the WhatsApp brief right now (to test the setup)")
    args = ap.parse_args()
    if not args.no_server and not args.test_whatsapp:
        # Only one copy should run (it starts automatically at login).
        try:
            urllib.request.urlopen("http://127.0.0.1:{}/api/status".format(PORT), timeout=3).close()
            running = True
        except Exception:
            running = False
        if running:
            if args.open:   # started by hand: replace the older copy so the newest code runs
                try:
                    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:{}/api/quit".format(PORT), data=b"{}",
                                           method="POST", headers={"X-TMS": "1", "Content-Type": "application/json"}), timeout=5).close()
                    print("Closed the older sync window. Starting fresh...")
                    time.sleep(2)
                except Exception:
                    print("TMS sync is already running in another window. Close that window first, then run this again.")
                    print("Dashboard: http://localhost:{}/".format(PORT))
                    import webbrowser; webbrowser.open("http://127.0.0.1:{}/".format(PORT))
                    return
            else:            # automatic start at sign-in/unlock: keep the one already running
                print("TMS sync is already running. Dashboard: http://localhost:{}/".format(PORT))
                return
    if not args.no_server: start_server(lan=not args.no_phone)
    fails = 0
    while True:
        t = time.time()
        ok = False
        print("[{}] Syncing from Tally...".format(dt.datetime.now().strftime("%H:%M:%S")))
        try:
            data = sync(args)
            ok = True
            print("  done in {:.0f}s. Dashboard: http://localhost:{}/".format(time.time() - t, PORT))
            try:
                cloud_push(data)
            except Exception as e:
                print("  ! Upload to the family link failed (will retry next sync): {}".format(e))
            try:
                maybe_send_brief(data, force=args.test_whatsapp)
            except Exception as e:
                print("  ! WhatsApp brief failed: {}".format(e))
            if args.test_whatsapp: break
            if args.open:
                args.open = False
                import webbrowser
                webbrowser.open("http://127.0.0.1:{}/".format(PORT) if not args.no_server else OUTPUT.as_uri())
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            print("  ! Tally not reachable yet ({}). Waiting for VCC green + company open in Tally - retrying every minute.".format(getattr(e, "reason", e)))
        except Exception:
            print("  ! Sync failed (is the company open in Tally?) - retrying every minute:"); traceback.print_exc()
        if not args.every: break
        # Wait for the next sync. Retry every minute while Tally is not ready (after a
        # failure). Check the clock in short steps so a laptop waking from sleep syncs
        # straight away instead of finishing an old 15-minute wait.
        fails = 0 if ok else fails + 1
        wait = args.every * 60 if ok else 60
        due, last = t + wait, time.time()
        while True:
            time.sleep(15)
            now = time.time()
            if now >= due:
                break
            if now - last > 120:   # clock jumped: laptop was asleep
                print("[{}] Laptop woke up - syncing now.".format(dt.datetime.now().strftime("%H:%M:%S")))
                time.sleep(20)     # give Wi-Fi and VCC a moment to reconnect
                break
            last = now

if __name__ == "__main__":
    main()
