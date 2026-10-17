"""
enable_results.py -- switch the quarterly-results page on in the server's api.py. Runs ON THE DROPLET.
---------------------------------------------------------------------------
    cd /opt/mfapi
    venv/bin/python3 enable_results.py            # add it
    venv/bin/python3 enable_results.py --check    # say whether it is added, change nothing
    venv/bin/python3 enable_results.py --undo     # take it out again

WHY A SCRIPT AND NOT A NEW api.py
    The same reason as enable_tradeplan.py: the api.py on the laptop carries every change made since the last
    upload, some of which need tables that do not exist yet. This feature stands alone, so the live api.py only
    needs one small block at its very end, and that is all this adds. Nothing already in api.py is changed.

WHAT IT DOES
    1. copies api.py to api.py.before_results (never overwriting an earlier copy);
    2. appends the block below to the END of api.py;
    3. checks the result is valid Python BEFORE replacing the file.
    Then restart the service (systemctl restart mfapi). SAFE TO RE-RUN: it recognises its own block.

IF THE NEW api.py IS UPLOADED LATER
    The laptop's api.py mounts the page itself. Uploading it replaces this block, and this script then says
    there is nothing to add.
"""

import os
import sys
import time

MARK = "# RESULTS-MOUNT"

BLOCK = '''
# RESULTS-MOUNT -- added by enable_results.py. The quarterly-results page at /results and its API under /api/results.
# For Stocks subscribers and administrators (stock_access.py); MF_RESULTS_ACCESS can change that, and
# MF_RESULTS_ENABLED=false switches it off.
# To take it out again: run enable_results.py --undo (or restore api.py.before_results) and restart.
# A failure here is printed and skipped on purpose: a problem in this page must not stop the whole site from
# starting. Look for the message in: journalctl -u mfapi
try:
    import results_api
    results_api.mount(app)
except Exception:                                  # noqa: BLE001
    import traceback
    print("mfapi: the quarterly results page could not be started and will be unavailable:", flush=True)
    traceback.print_exc()
'''

NEEDS = ("results_api.py", "results.html", "portfolio_api.py", "stock_access.py")
# what results_api borrows from api.py for the P/E, PEG and market cap, so the page and the stock page agree
BORROWS = ("build_ratios", "_build_pe_peg", "_fin_headline", "_fin_eps_comparable", "latest_prices")


def newline_of(text):
    return "\r\n" if "\r\n" in text else "\n"


def read(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


def write_checked(path, text):
    """Write only if `text` is valid Python; replace atomically."""
    compile(text, path, "exec")
    tmp = path + ".new"
    with open(tmp, "wb") as f:
        f.write(text.encode("utf-8"))
    os.replace(tmp, path)


def state(text):
    if MARK in text:
        return "added"
    if "results_api" in text:
        return "native"
    return "absent"


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    path = args[0] if args else "/opt/mfapi/api.py"
    if not os.path.exists(path):
        sys.exit("Cannot find %s. Run this from /opt/mfapi, or give the path of api.py." % path)
    text = read(path)
    st = state(text)

    if "--check" in argv:
        print({"added": "The results page block is in %s." % path,
               "native": "%s mounts the results page by itself; nothing to add." % path,
               "absent": "The results page is NOT in %s yet." % path}[st])
        return

    if "--undo" in argv:
        if st != "added":
            sys.exit("Nothing to undo: %s has no block from this script." % path)
        i = text.index(MARK)
        i = text.rfind("\n", 0, i)
        write_checked(path, text[:i].rstrip("\r\n") + newline_of(text))
        print("Removed the results page block from %s. Restart the service: systemctl restart mfapi" % path)
        return

    if st == "added":
        print("Already added to %s; nothing changed." % path)
        return
    if st == "native":
        print("%s already mounts the results page by itself; nothing to add." % path)
        return
    if "app = FastAPI" not in text and "app=FastAPI" not in text:
        sys.exit("%s does not look like the site's api.py (no 'app = FastAPI(...)'). Nothing changed." % path)

    here = os.path.dirname(os.path.abspath(path))
    missing = [f for f in NEEDS if not os.path.exists(os.path.join(here, f))]
    if missing:
        sys.exit("These files are not in %s yet: %s. Upload them first, then run this again. Nothing changed."
                 % (here, ", ".join(missing)))
    lacking = [n for n in BORROWS if ("def %s(" % n) not in text]
    if lacking:
        sys.exit("The api.py on this server does not have %s, which the results page borrows from it for the P/E, "
                 "PEG and market cap (they came with the ratios update). Upload the current api.py first, or this page "
                 "would show no ratios. Nothing changed." % ", ".join(lacking))

    backup = path + ".before_results"
    if os.path.exists(backup):
        backup += "." + time.strftime("%Y%m%d%H%M%S")
    with open(backup, "wb") as f:
        f.write(text.encode("utf-8"))
    nl = newline_of(text)
    write_checked(path, text.rstrip("\r\n") + nl + BLOCK.replace("\n", nl))
    print("Added the results page block to the end of %s." % path)
    print("A copy of the old file is at %s." % backup)
    print("Now restart the service:  systemctl restart mfapi")


if __name__ == "__main__":
    main(sys.argv[1:])
