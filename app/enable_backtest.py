"""
enable_backtest.py -- switch the back-test on in the server's api.py. Runs ON THE DROPLET.
---------------------------------------------------------------------------
    cd /opt/mfapi
    venv/bin/python3 enable_backtest.py            # add it
    venv/bin/python3 enable_backtest.py --check    # say whether it is added, change nothing
    venv/bin/python3 enable_backtest.py --undo     # take it out again

WHY A SCRIPT AND NOT A NEW api.py
    The api.py on this laptop carries every change made since the last upload
    (price chart, ratios, shareholding ...), and some of those need database
    tables that do not exist yet. Uploading it just to get the back-test would
    ship all of them. The back-test is built to stand alone, so the live api.py
    only needs one small block added at its very end, and that is all this does.
    Nothing already in api.py is changed or moved.

WHAT IT DOES
    1. copies api.py to api.py.before_backtest (never overwriting an earlier copy);
    2. appends the block below to the END of api.py;
    3. checks the result is valid Python BEFORE replacing the file, so a mistake
       cannot leave the site with a broken api.py.
    Then restart the service (systemctl restart mfapi). SAFE TO RE-RUN: it
    recognises its own block and does nothing the second time.

IF THE NEW api.py IS UPLOADED LATER
    The laptop's api.py already mounts the back-test itself. Uploading it simply
    replaces this block with that, and this script then says there is nothing to
    add.
"""

import os
import sys
import time

MARK = "# BACKTEST-MOUNT"

BLOCK = '''
# BACKTEST-MOUNT -- added by enable_backtest.py. "Back-test your rules": the page at /backtest and the
# API under /api/rule-backtest. For Stocks subscribers and administrators; MF_BACKTEST_ACCESS can change that (see backtest_api.py).
# To take it out again: run enable_backtest.py --undo (or restore api.py.before_backtest) and restart.
# A failure here is printed and skipped on purpose: a problem in this tool must not stop
# the whole site from starting. Look for the message in: journalctl -u mfapi
try:
    import backtest_api
    backtest_api.mount(app)
except Exception:                                  # noqa: BLE001
    import traceback
    print("mfapi: the back-test could not be started and will be unavailable:", flush=True)
    traceback.print_exc()
'''


def newline_of(text):
    return "\r\n" if "\r\n" in text else "\n"


def read(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


def write_checked(path, text):
    """Write only if `text` is valid Python; replace atomically."""
    compile(text, path, "exec")                       # raises SyntaxError before anything is touched
    tmp = path + ".new"
    with open(tmp, "wb") as f:
        f.write(text.encode("utf-8"))
    os.replace(tmp, path)


def state(text):
    if MARK in text:
        return "added"
    if "backtest_api" in text:
        return "native"                                # this api.py mounts it by itself
    return "absent"


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    path = args[0] if args else "/opt/mfapi/api.py"
    if not os.path.exists(path):
        sys.exit("Cannot find %s. Run this from /opt/mfapi, or give the path of api.py." % path)
    text = read(path)
    st = state(text)

    if "--check" in argv:
        print({"added": "The back-test block is in %s." % path,
               "native": "%s mounts the back-test by itself; nothing to add." % path,
               "absent": "The back-test is NOT in %s yet." % path}[st])
        return

    if "--undo" in argv:
        if st != "added":
            sys.exit("Nothing to undo: %s has no block from this script." % path)
        i = text.index(MARK)
        i = text.rfind("\n", 0, i)                     # back to the blank line before the block
        out = text[:i].rstrip("\r\n") + newline_of(text)
        write_checked(path, out)
        print("Removed the back-test block from %s. Restart the service: systemctl restart mfapi" % path)
        return

    if st == "added":
        print("Already added to %s; nothing changed." % path)
        return
    if st == "native":
        print("%s already mounts the back-test by itself; nothing to add." % path)
        return
    if "app = FastAPI" not in text and "app=FastAPI" not in text:
        sys.exit("%s does not look like the site's api.py (no 'app = FastAPI(...)'). Nothing changed." % path)

    # The files it needs must be there, or the block would only ever print an error.
    here = os.path.dirname(os.path.abspath(path))
    missing = [f for f in ("backtest_engine.py", "backtest_api.py", "price_adjust.py", "backtest.html", "indicators.py")
               if not os.path.exists(os.path.join(here, f))]
    if missing:
        sys.exit("These files are not in %s yet: %s. Upload them first, then run this again. Nothing changed."
                 % (here, ", ".join(missing)))

    backup = path + ".before_backtest"
    if os.path.exists(backup):
        backup += "." + time.strftime("%Y%m%d%H%M%S")
    with open(backup, "wb") as f:
        f.write(text.encode("utf-8"))
    nl = newline_of(text)
    block = BLOCK.replace("\n", nl)
    write_checked(path, text.rstrip("\r\n") + nl + block)
    print("Added the back-test block to the end of %s." % path)
    print("A copy of the old file is at %s." % backup)
    print("Now restart the service:  systemctl restart mfapi")


if __name__ == "__main__":
    main(sys.argv[1:])
