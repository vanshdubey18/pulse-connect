"""Mock Tally XML server (TallyPrime-like) with a small, self-consistent distributor's books."""
import datetime as dt, os, random, re, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from xml.sax.saxutils import escape as e

PRODUCT = os.environ.get("MOCK_PRODUCT", "TallyPrime")
COMPANIES = os.environ.get("MOCK_COMPANIES", "SHREE GANESH TRADERS").split("|")
random.seed(7)
TODAY = dt.date.today()
G = [("Primary", None, ""), ("Current Assets", "\x04 Primary", "Current Assets"), ("Current Liabilities", "\x04 Primary", "Current Liabilities"),
     ("Bank Accounts", "Current Assets", "Bank Accounts"), ("Cash-in-Hand", "Current Assets", "Cash-in-Hand"),
     ("Sundry Debtors", "Current Assets", "Sundry Debtors"), ("Sundry Creditors", "Current Liabilities", "Sundry Creditors"),
     ("Duties & Taxes", "Current Liabilities", "Duties & Taxes"), ("Sales Accounts", "\x04 Primary", "Sales Accounts"),
     ("Purchase Accounts", "\x04 Primary", "Purchase Accounts"), ("Indirect Expenses", "\x04 Primary", "Indirect Expenses"),
     ("Stock-in-Hand", "Current Assets", "Stock-in-Hand"), ("Capital Account", "\x04 Primary", "Capital Account"),
     ("Retail Customers", "Sundry Debtors", "")]
VT = [("Sales", "Sales"), ("Purchase", "Purchase"), ("Receipt", "Receipt"), ("Payment", "Payment"), ("Journal", "Journal"),
      ("Credit Note", "Credit Note"), ("GST Sales", None)]
CUST = [("Mehta Stores", "Sundry Debtors", "9876543210"), ("District Hospital Kathua", "Sundry Debtors", "01922-234567"),
        ("Gupta & Sons", "Retail Customers", ""), ("Raina Enterprises", "Sundry Debtors", "+91 94191 22334"), ("Kapoor Medicals", "Sundry Debtors", "")]
SUPP = [("Hindustan Wholesale", "Sundry Creditors"), ("Alpha Distributors", "Sundry Creditors")]
ITEMS = [("Rice 25kg", "Grains", 1100.0), ("Sugar 50kg", "Grains", 2100.0), ("Oil 15L", "Edible", 1850.0), ("Soap Box", "FMCG", 640.0)]
LED = {"HDFC Bank": ("Bank Accounts", -250000.0), "Cash": ("Cash-in-Hand", -15000.0), "Capital": ("Capital Account", 900000.0),
       "Sales": ("Sales Accounts", 0.0), "Purchase": ("Purchase Accounts", 0.0), "Output GST": ("Duties & Taxes", 0.0),
       "Rent": ("Indirect Expenses", 0.0), "Salary": ("Indirect Expenses", 0.0)}
for n, g, _ in CUST: LED[n] = (g, -50000.0)
for n, g in SUPP: LED[n] = (g, 80000.0)
VCH = []   # (date, type, number, party, amount_party, [(ledger, amt)], [(item, qty, amt)], narration, postdated)
def add(d, t, party, lines, items=(), nr="", pdc=False):
    VCH.append((d, t, str(len(VCH) + 1), party, dict(lines).get(party, 0.0), lines, list(items), nr, pdc))
start = TODAY - dt.timedelta(days=430)
d = start
while d <= TODAY:
    if random.random() < 0.6:
        c = random.choice(CUST)[0]; it = random.choice(ITEMS); q = random.randint(2, 40); v = round(it[2] * q, 2); g = round(v * .05, 2)
        add(d, random.choice(["Sales", "GST Sales"]), c, [(c, -(v + g)), ("Sales", v), ("Output GST", g)], [(it[0], q, v)], "Bill to " + c)
    if random.random() < 0.35:
        c = random.choice(CUST)[0]; a = random.randint(5, 60) * 1000.0
        add(d, "Receipt", c, [(c, a), ("HDFC Bank", -a)], nr="NEFT recd " + c)
    if random.random() < 0.2:
        s = random.choice(SUPP)[0]; it = random.choice(ITEMS); q = random.randint(20, 80); v = round(it[2] * .85 * q, 2)
        add(d, "Purchase", s, [(s, v), ("Purchase", -v)], [(it[0], q, -v)])
    if random.random() < 0.15:
        s = random.choice(SUPP)[0]; a = random.randint(10, 50) * 1000.0
        add(d, "Payment", s, [(s, -a), ("HDFC Bank", a)])
    if d.day == 1:
        add(d, "Payment", "", [("Rent", -25000.0), ("HDFC Bank", 25000.0)], nr="Shop rent"); add(d, "Payment", "", [("Salary", -60000.0), ("Cash", 60000.0)])
    d += dt.timedelta(days=1)
add(TODAY + dt.timedelta(days=12), "Receipt", "Mehta Stores", [("Mehta Stores", 45000.0), ("HDFC Bank", -45000.0)], nr="PDC chq 004512", pdc=True)
ALT = {"v": 1000}
LOCK = threading.Lock()

def bal(ledger, to):
    g, op = LED[ledger]
    return op + sum(a for v in VCH if v[0] <= to and not v[8] for l, a in v[5] if l == ledger)

def stock(to):
    out = {}
    for n, grp, rate in ITEMS:
        q = 300 + sum(-qq if a > 0 else qq for v in VCH if v[0] <= to and not v[8] for i, qq, a in v[6] if i == n)
        out[n] = (grp, max(q, 0), max(q, 0) * rate * .85)
    return out

def dt_(s):
    return dt.datetime.strptime(s, "%Y%m%d").date() if s else None

H, F = "<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DESC></DESC><DATA><COLLECTION>", "</COLLECTION></DATA></BODY></ENVELOPE>"
def amt(x): return "{:.2f}".format(x)

def respond(body):
    m = re.search(r"<SVFROMDATE[^>]*>(\d+)</SVFROMDATE><SVTODATE[^>]*>(\d+)</SVTODATE>", body)
    f, t = (dt_(m.group(1)), dt_(m.group(2))) if m else (None, TODAY)
    mt = re.search(r"<TYPE>(\w+)</TYPE><FETCH>", body)
    co = re.search(r"<SVCURRENTCOMPANY>(.*?)</SVCURRENTCOMPANY>", body)
    if co and co.group(1) not in [e(c) for c in COMPANIES]:
        return "<RESPONSE><LINEERROR>Could not find Company '{}'</LINEERROR></RESPONSE>".format(co.group(1))
    if "<ID>Bills Receivable</ID>" in body or "<ID>Bills Payable</ID>" in body:
        rec = "Receivable" in body; out = "<ENVELOPE>"
        for v in VCH:
            if v[8]: continue
            if rec and v[1] in ("Sales", "GST Sales") and (TODAY - v[0]).days < 200 and int(v[2]) % 3 == 0:
                out += "<BILLFIXED><BILLDATE>{}</BILLDATE><BILLREF>INV/{}</BILLREF><BILLPARTY>{}</BILLPARTY></BILLFIXED><BILLCL>{}</BILLCL><BILLDUE>{}</BILLDUE><BILLOVERDUE>{}</BILLOVERDUE>".format(
                    "{}-{}".format(v[0].day, v[0].strftime("%b-%y")), v[2], e(v[3]), amt(v[4]), "{}-{}".format((v[0] + dt.timedelta(30)).day, (v[0] + dt.timedelta(30)).strftime("%b-%y")), max(0, (TODAY - v[0]).days - 30))
            if not rec and v[1] == "Purchase" and (TODAY - v[0]).days < 120:
                out += "<BILLFIXED><BILLDATE>{}</BILLDATE><BILLREF>P/{}</BILLREF><BILLPARTY>{}</BILLPARTY></BILLFIXED><BILLCL>{}</BILLCL><BILLDUE></BILLDUE><BILLOVERDUE>0</BILLOVERDUE>".format(
                    "{}-{}".format(v[0].day, v[0].strftime("%b-%y")), v[2], e(v[3]), amt(v[4]))
        return out + "</ENVELOPE>"
    if not mt: return "<RESPONSE><LINEERROR>Unknown request</LINEERROR></RESPONSE>"
    typ = mt.group(1)
    if typ == "Company":
        return H + "".join('<COMPANY NAME="{}"><BASICCOMPANYFORMALNAME>{}</BASICCOMPANYFORMALNAME><STARTINGFROM>20260401</STARTINGFROM><BOOKSFROM>20240401</BOOKSFROM><ALTVCHID>{}</ALTVCHID><ALTMSTID>55</ALTMSTID><GUID>g-{}</GUID></COMPANY>'.format(e(c), e(c), ALT["v"], i) for i, c in enumerate(COMPANIES)) + F
    if typ == "Group":
        return H + "".join('<GROUP NAME="{}" RESERVEDNAME="{}"><PARENT>{}</PARENT></GROUP>'.format(e(n), e(r), e(p or "")) for n, p, r in G if p) + F
    if typ == "VoucherType":
        return H + "".join('<VOUCHERTYPE NAME="{}" RESERVEDNAME="{}"><PARENT>{}</PARENT></VOUCHERTYPE>'.format(n, r or "", "Sales" if not r else n) for n, r in VT) + F
    if typ == "Ledger":
        party = "LEDGERMOBILE" in body
        rows = []
        for n, (g, op) in LED.items():
            x = '<LEDGER NAME="{}"><PARENT>{}</PARENT>'.format(e(n), e(g))
            if party:
                ph = next((c[2] for c in CUST if c[0] == n), "")
                x += "<LEDGERMOBILE>{}</LEDGERMOBILE><BILLCREDITPERIOD>{}</BILLCREDITPERIOD><CREDITLIMIT>{}</CREDITLIMIT>".format(ph, "30 Days" if ph else "", "-200000.00" if ph else "")
            else:
                x += "<CLOSINGBALANCE>{}</CLOSINGBALANCE>".format(amt(bal(n, t)))
            rows.append(x + "</LEDGER>")
        return H + "".join(rows) + F
    if typ == "StockItem":
        return H + "".join('<STOCKITEM NAME="{}"><PARENT>{}</PARENT><BASEUNITS>Nos</BASEUNITS><CLOSINGBALANCE> {:.0f} Nos</CLOSINGBALANCE><CLOSINGVALUE>{}</CLOSINGVALUE></STOCKITEM>'.format(e(n), g, q, amt(-v)) for n, (g, q, v) in stock(t).items()) + F
    if typ == "Voucher":
        rows = []
        for v in VCH:
            if not (f <= v[0] <= t): continue
            if v[8] and "ISPOSTDATED" not in body: continue
            x = '<VOUCHER VCHTYPE="{}"><DATE>{}</DATE><VOUCHERTYPENAME>{}</VOUCHERTYPENAME><VOUCHERNUMBER>{}</VOUCHERNUMBER><PARTYLEDGERNAME>{}</PARTYLEDGERNAME><AMOUNT>{}</AMOUNT><NARRATION>{}</NARRATION><ISPOSTDATED>{}</ISPOSTDATED>'.format(
                v[1], v[0].strftime("%Y%m%d"), v[1], v[2], e(v[3]), amt(v[4]), e(v[7]), "Yes" if v[8] else "No")
            x += "".join("<ALLLEDGERENTRIES.LIST><LEDGERNAME>{}</LEDGERNAME><AMOUNT>{}</AMOUNT></ALLLEDGERENTRIES.LIST>".format(e(l), amt(a)) for l, a in v[5])
            x += "".join("<ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>{}</STOCKITEMNAME><BILLEDQTY> {} Nos</BILLEDQTY><AMOUNT>{}</AMOUNT></ALLINVENTORYENTRIES.LIST>".format(e(i), q, amt(a)) for i, q, a in v[6])
            rows.append(x + "</VOUCHER>")
        return H + "".join(rows) + F
    return H + F

class Hd(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/delold":
            with LOCK:
                old = TODAY - dt.timedelta(days=150)
                i = next(i for i, v in enumerate(VCH) if v[0] >= old and v[1] in ("Sales", "GST Sales"))
                msg = "deleted " + str(VCH[i][0]); del VCH[i]; ALT["v"] += 1
            b = msg.encode(); self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
        if self.path == "/bump":
            with LOCK:
                c = CUST[0][0]; add(TODAY, "Sales", c, [(c, -11800.0), ("Sales", 10000.0), ("Output GST", 1800.0)], [(ITEMS[0][0], 9, 10000.0)], "new bill"); ALT["v"] += 1
            msg = "bumped"
        else:
            msg = "<RESPONSE>{} Server is Running</RESPONSE>".format("Tally.ERP 9" if "ERP" in PRODUCT else "TallyPrime")
        b = msg.encode(); self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
        if os.environ.get("MOCK_HANG_FILE") and os.path.exists(os.environ["MOCK_HANG_FILE"]) and "<TYPE>Voucher</TYPE>" in body:
            import time; time.sleep(3600)
        with LOCK: r = respond(body)
        b = r.encode("utf-16-le") if "UTF16" in os.environ.get("MOCK_ENC", "") else r.encode("utf-8")
        self.send_response(200); self.send_header("Content-Type", "text/xml"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

class S(ThreadingMixIn, HTTPServer): daemon_threads = True
if __name__ == "__main__":
    S(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 9000), Hd).serve_forever()
