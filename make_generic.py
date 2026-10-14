"""Turn the TMS dashboard template into the generic 'Pulse' template used by the connector app."""
import sys
src, dst = sys.argv[1], sys.argv[2]
t = open(src, encoding="utf-8").read()
R = [
 ('<meta name="apple-mobile-web-app-title" content="TMS Pulse">', '<meta name="apple-mobile-web-app-title" content="Pulse">'),
 ("fill='white'%3ETMS%3C/text%3E", "fill='white'%3EP%3C/text%3E"),
 ('<title>TMS Business Pulse</title>', '<title>Business Pulse</title>'),
 ('<div class="logo">TMS</div>', '<div class="logo" id="coLogo">P</div>'),
 ('<div class="co" id="coName">Techno Med Services<small>Business Pulse</small></div>', '<div class="co" id="coName">Business Pulse<small>live from Tally</small></div>'),
 ('Open TMS Pulse on a phone', 'Open Pulse on a phone'),
 ('</svg>Ask TMS</button>', '</svg>Ask Pulse</button>'),
 ("'TMS business dashboard (live). Tap to open, then Add to Home Screen:\\n'", "'Our business dashboard (live). Tap to open, then Add to Home Screen:\\n'"),
 ('`\\n_TMS Business Pulse_`', '`\\n_Business Pulse_`'),
 ('Switch on Ask TMS', 'Switch on Ask Pulse'), ('Saved. Ask TMS is on.', 'Saved. Ask Pulse is on.'),
 ('Government hospitals owe ${inr(R.govt)} (${pct(R.govt,R.govt+R.private)}%).', '${R.govt>0?`Government buyers owe ${inr(R.govt)} (${pct(R.govt,R.govt+R.private)}%).`:\'\'}'),
 ('You are the finance assistant inside "TMS Business Pulse", the owner\'s dashboard for ${M.company} (Techno Med Services), a medical equipment and consumables distributor in Jammu & Srinagar that sells mostly to government hospitals (JKMSCL, GMC, SKIMS, AIIMS, district hospitals) and private dealers. The person asking is the owner or his family; they are not accountants.',
  'You are the finance assistant inside "Business Pulse", the owner\'s dashboard for ${M.company}, an Indian business that keeps its books in Tally. The person asking is the owner or their family; they are not accountants.'),
 ('Credit notes and journal deductions (TDS, JKMSCL deductions) are separate.', 'Credit notes and journal deductions (e.g. TDS) are separate.'),
 ('- "govt" customers are identified by name (Managing Director, JKMSCL, GMC, hospital, director, etc.).', '- "govt" customers are identified by name (Government, Department, Directorate, Municipal, etc.).'),
 ('(e.g. "GMC" as "GMS")', '(e.g. a short name misheard)'),
 ('TallyPrime books', 'Tally books'),
 ('customers are recognised by name (Managing Director, JKMSCL, GMC, SKIMS, AIIMS, Hospital, Director…).', 'customers are recognised by name (Government, Department, Directorate, Municipal…).'),
]
for a, b in R:
    if a not in t: sys.exit("missing: " + a[:70])
    t = t.replace(a, b)
# Logo initials from the company name; hide the Government/Private split when nobody is government.
a = "$('#coName').innerHTML="
t = t.replace(a, "$('#coLogo').textContent=(M.company.match(/\\b[A-Za-z]/g)||['P']).slice(0,3).join('').toUpperCase();" + a, 1)
a = "const g=R.govt,pv=R.private,tt=(g+pv)||1;"
t = t.replace(a, a + "{const gs=$('#govSplit');const box=gs&&gs.closest('.card');if(box)box.style.display=g>0?'':'none';const ct=$('#custTabs');if(ct)ct.style.display=g>0?'':'none';}", 1)
for bad in ("Techno Med", "JKMSCL", "TMS Pulse", "Ask TMS"):
    if bad in t: print("note: still contains", bad)
open(dst, "w", encoding="utf-8").write(t)
print("ok", len(t))
