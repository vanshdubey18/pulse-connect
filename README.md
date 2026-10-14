# Pulse Connect

Links a business's Tally (Tally.ERP 9, every TallyPrime release, Tally on cloud/AWS via VCC) to its Business Pulse dashboard.
Read-only: it only sends Tally *export* requests.

- `pulse_connect.py` – the Windows app (pairing, Tally detection, data-link switch-on, background sync, local UI on 127.0.0.1:47819)
- `engine.py` – reads Tally over XML/HTTP and builds the dashboard data
- `ui.html` – the app's window (opens in the browser)
- `dashboard_template.html` → `make_generic.py` → `pulse_template.html` – the dashboard page
- `.github/workflows/build.yml` – builds `PulseConnect.exe` on Windows; publish it as a release asset named `PulseConnect.exe`
