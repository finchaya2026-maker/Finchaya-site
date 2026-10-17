"""
enable_tradeplan.py -- switch "My trade plan" on in the server's api.py. Runs ON THE DROPLET.
---------------------------------------------------------------------------
    cd /opt/mfapi
    venv/bin/python3 enable_tradeplan.py            # add it
    venv/bin/python3 enable_tradeplan.py --check    # say whether it is added, change nothing
    venv/bin/python3 enable_tradeplan.py --undo     # take it out again

WHY A SCRIPT AND NOT A NEW api.py
    The same reason as enable_backtest.py: the api.py on the laptop carries every change made since the last
    upload, some of which need tables that do not exist yet. This feature stands alone, so the live api.py only
    needs one small block at its very end, and that is all this adds. Nothing already in api.py is changed.

WHAT IT DOES
    1. copies api.py to api.py.before_tradeplan (never overwriting an earlier copy);
    2. appends the block below to the END of api.py;
    3. checks the result is valid Python BEFORE replacing the file.
    Then restart the service (systemctl restart mfapi). SAFE TO RE-RUN: it recognises its own block.

IF THE NEW api.py IS UPLOADED LATER
    The laptop's api.py mounts the feature itself. Uploading it replaces this block, and this script then says
    there is nothing to add.
"""

import os
import sys
import time

MARK = "# TRADEPLAN-MOUNT"

BLOCK = '''
# TRADEPLAN-MOUNT -- added by enable_tradeplan.py. "My trade plan": the page at /tradeplan and the API under
# /api/tradeplan. ADMIN-ONLY until MF_TRADEPLAN_ACCESS says otherwise (see tradeplan_api.py).
# To take it out again: run enable_tradeplan.py --undo (or restore api.py.before_tradeplan) and restart.
# A failure here is printed and skipped on purpose: a problem in an admin-only tool must not stop the whole
# site from starting. Look for the message in: journalctl -u mfapi
try:
    import tradeplan_api
    tradeplan_api.mount(app)
except Exception:                                  # noqa: BLE001
    import traceback
    print("mfapi: the trade plan tool could not be started and will be unavailable:", flush=True)
    traceback.print_exc()
'''

NEEDS = ("tradeplan_engine.py", "tradeplan_api.py", "tradeplan_alerts.py", "tradeplan.html", "backtest_engine.py", "backtest_api.py",
         "price_adjust.py", "indicators.py", "mailer.py")           # mailer.py: tradeplan_alerts.py sends through it


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
    if "tradeplan_api" in text:
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
        print({"added": "The trade plan block is in %s." % path,
               "native": "%s mounts the trade plan tool by itself; nothing to add." % path,
               "absent": "The trade plan tool is NOT in %s yet." % path}[st])
        return

    if "--undo" in argv:
        if st != "added":
            sys.exit("Nothing to undo: %s has no block from this script." % path)
        i = text.index(MARK)
        i = text.rfind("\n", 0, i)
        write_checked(path, text[:i].rstrip("\r\n") + newline_of(text))
        print("Removed the trade plan block from %s. Restart the service: systemctl restart mfapi" % path)
        return

    if st == "added":
        print("Already added to %s; nothing changed." % path)
        return
    if st == "native":
        print("%s already mounts the trade plan tool by itself; nothing to add." % path)
        return
    if "app = FastAPI" not in text and "app=FastAPI" not in text:
        sys.exit("%s does not look like the site's api.py (no 'app = FastAPI(...)'). Nothing changed." % path)

    here = os.path.dirname(os.path.abspath(path))
    missing = [f for f in NEEDS if not os.path.exists(os.path.join(here, f))]
    if missing:
        sys.exit("These files are not in %s yet: %s. Upload them first, then run this again. Nothing changed."
                 % (here, ", ".join(missing)))

    backup = path + ".before_tradeplan"
    if os.path.exists(backup):
        backup += "." + time.strftime("%Y%m%d%H%M%S")
    with open(backup, "wb") as f:
        f.write(text.encode("utf-8"))
    nl = newline_of(text)
    write_checked(path, text.rstrip("\r\n") + nl + BLOCK.replace("\n", nl))
    print("Added the trade plan block to the end of %s." % path)
    print("A copy of the old file is at %s." % backup)
    print("Now restart the service:  systemctl restart mfapi")


if __name__ == "__main__":
    main(sys.argv[1:])
