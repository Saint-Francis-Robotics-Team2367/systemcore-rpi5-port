"""Dashboard JS patches, shared by patcher/, patcher_win/ and build-image.sh.

The dashboard is a minified React bundle (`main.*.js`), so variable names and
the JSX runtime alias change between upstream releases (Beta 10: `o`/`a`/`xo`,
Beta 15: `s`/`o`/`Ro`). Each patch discovers the names it needs from nearby
code instead of hardcoding them.

Run standalone: python3 -m patcher.dashboard path/to/main.xxxx.js
"""

from __future__ import annotations

import logging
import re
import sys

WLAN_FORCE_RE = re.compile(
    r',\{static_ip:"172\.30\.0\.1",gateway:"172\.30\.0\.1",use_dhcp:!1\}'
)
FAULT_COUNTS_RE = re.compile(r"faultCounts:t\.fc\|\|\[0,0,0,0,0,0\]")
FAULT_HISTORY_RE = re.compile(r'"historical-"\.concat\(t\)\)\}\)\)\]')

# JSX_ is replaced with the bundle's JSX runtime alias.
RESET_BUTTON = (
    '"historical-".concat(t))})),'
    '(0,JSX_.jsx)("div",{style:{marginTop:"8px",textAlign:"center"},'
    'children:(0,JSX_.jsx)("button",{onClick:function(){'
    'window.__faultBL=window.__rawFC?window.__rawFC.slice():[]},'
    'style:{fontSize:"11px",padding:"2px 8px",cursor:"pointer",'
    'background:"#333",color:"#fff",border:"1px solid #666",'
    'borderRadius:"3px"},children:"Reset Fault Counts"})})]'
)


def unlock_wlan(js: str, log: logging.Logger) -> str:
    """Let the user edit wlan0 (Access Point) settings in the network page."""
    m = re.search(r"\b([A-Za-z_$][\w$]*)=\"wlan0\"===e\b", js)
    if not m:
        log.warning("dashboard: wlan0 flag variable not found, skipping WLAN unlock")
        return js
    wlan = re.escape(m.group(1))
    # `disabled:<busy>||<isWlan>` -> `disabled:<busy>`
    js, n = re.subn(rf"disabled:([A-Za-z_$][\w$]*)\|\|{wlan}\b", r"disabled:\1", js)
    log.info("dashboard: unlocked %d wlan0 field(s)", n)
    # Stop the save handler from forcing wlan0 back to 172.30.0.1/static.
    js, n = WLAN_FORCE_RE.subn(",{}", js)
    log.info("dashboard: removed %d forced wlan0 override(s)", n)
    return js


def add_fault_reset(js: str, log: logging.Logger) -> str:
    """Add a frontend-only 'Reset Fault Counts' button to the fault tooltip."""
    if "Reset Fault Counts" in js:
        log.info("dashboard: fault reset button already present")
        return js
    hist = FAULT_HISTORY_RE.search(js)
    if not hist or not FAULT_COUNTS_RE.search(js):
        log.warning("dashboard: fault tooltip pattern not found, skipping reset button")
        return js
    aliases = re.findall(r"\(0,([A-Za-z_$][\w$]*)\.jsx\)", js[:hist.start()])
    if not aliases:
        log.warning("dashboard: JSX runtime alias not found, skipping reset button")
        return js
    js = FAULT_COUNTS_RE.sub(
        "faultCounts:(window.__rawFC=t.fc||[0,0,0,0,0,0]).map(function(v,j){"
        "return Math.max(0,v-((window.__faultBL||[])[j]||0))})",
        js, count=1,
    )
    button = RESET_BUTTON.replace("JSX_", aliases[-1])
    js = js[:hist.start()] + button + js[hist.end():]
    log.info("dashboard: added fault reset button (JSX alias %s)", aliases[-1])
    return js


def patch(js: str, log: logging.Logger, wlan: bool = True, faults: bool = True) -> str:
    if wlan:
        js = unlock_wlan(js, log)
    if faults:
        js = add_fault_reset(js, log)
    return js


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        text = f.read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(patch(text, logging.getLogger("dashboard")))
