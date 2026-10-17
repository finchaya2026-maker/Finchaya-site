"""
enable_ipo.py -- switch the IPO section on in the server's api.py. Runs ON THE DROPLET.
---------------------------------------------------------------------------
    cd /opt/mfapi
    venv/bin/python3 enable_ipo.py            # add it
    venv/bin/python3 enable_ipo.py --check    # say whether it is added, change nothing
    venv/bin/python3 enable_ipo.py --undo     # take it out again

WHY A SCRIPT AND NOT A NEW api.py
    The same reason as enable_screenbuilder.py and enable_results.py: the api.py on the laptop carries every change made since the last upload. This
    section stands alone, so the live api.py only needs one small block at its very end. Nothing already in api.py is changed.

WHAT IT DOES
    1. copies api.py to api.py.before_ipo (never overwriting an earlier copy);
    2. appends the block below to the END of api.py (it mounts the API under /api/ipo and the pages at /ipo and /ipo/<name>);
    3. checks the result is valid Python BEFORE replacing the file.
    Then restart the service (systemctl restart mfapi). SAFE TO RE-RUN: it recognises its own block.

IF THE NEW api.py IS UPLOADED LATER
    The laptop's api.py mounts the IPO section itself. Uploading it replaces this block, and this script then says there is nothing to add.
"""

import os
import sys
import time

MARK = "# IPO-MOUNT"
END_LINE = "    traceback.print_exc()"            # the last line of BLOCK: --undo removes from MARK to here and nothing after it

BLOCK = '''
# IPO-MOUNT -- added by enable_ipo.py. The IPO section: the API under /api/ipo and the pages at /ipo and /ipo/<name>. Information only, no verdict.
# Open to everyone (MF_IPO_ACCESS=admin | signed_in | premium narrows it); MF_IPO_ENABLED=false switches it off. To take it out again:
# run enable_ipo.py --undo (or restore api.py.before_ipo) and restart.
# A failure here is printed and skipped on purpose: a problem in this feature must not stop the whole site from starting.
# Look for the message in: journalctl -u mfapi
try:
    import ipo_api
    ipo_api.mount(app)
except Exception:                                  # noqa: BLE001
    import traceback
    print("mfapi: the IPO section could not be started and will be unavailable:", flush=True)
    traceback.print_exc()
'''

# everything the section reads or borrows
NEEDS = ("ipo_api.py", "ipo_store.py", "ipo_sources.py", "ipo_extract.py", "ipo_view.js", "ipo.html", "ipo_issue.html", "stock_access.py", "price_adjust.py")


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
    if "ipo_api" in text:
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
        print({"added": "The IPO block is in %s." % path,
               "native": "%s mounts the IPO section by itself; nothing to add." % path,
               "absent": "The IPO section is NOT in %s yet." % path}[st])
        return

    if "--undo" in argv:
        if st != "added":
            sys.exit("Nothing to undo: %s has no block from this script." % path)
        # remove exactly OUR block (from its marker line to its last line), and leave anything appended after it
        i = text.rfind("\n", 0, text.index(MARK))
        end = text.find(END_LINE, i)
        if end < 0:
            sys.exit("Found the marker in %s but not the end of the block, so nothing was removed. Restore api.py.before_ipo by hand." % path)
        end = text.find("\n", end)
        end = len(text) if end < 0 else end
        head, tail, nl = text[:i].rstrip("\r\n"), text[end:].strip("\r\n"), newline_of(text)
        write_checked(path, head + nl + ((nl + tail + nl) if tail else ""))
        print("Removed the IPO block from %s. Restart the service: systemctl restart mfapi" % path)
        return

    if st == "added":
        print("Already added to %s; nothing changed." % path)
        return
    if st == "native":
        print("%s already mounts the IPO section by itself; nothing to add." % path)
        return
    if "app = FastAPI" not in text and "app=FastAPI" not in text:
        sys.exit("%s does not look like the site's api.py (no 'app = FastAPI(...)'). Nothing changed." % path)

    here = os.path.dirname(os.path.abspath(path))
    missing = [f for f in NEEDS if not os.path.exists(os.path.join(here, f))]
    if missing:
        sys.exit("These files are not in %s yet: %s. Upload them first, then run this again. Nothing changed." % (here, ", ".join(missing)))

    backup = path + ".before_ipo"
    if os.path.exists(backup):
        backup += "." + time.strftime("%Y%m%d%H%M%S")
    with open(backup, "wb") as f:
        f.write(text.encode("utf-8"))
    nl = newline_of(text)
    write_checked(path, text.rstrip("\r\n") + nl + BLOCK.replace("\n", nl))
    print("Added the IPO block to the end of %s." % path)
    print("A copy of the old file is at %s." % backup)
    print("Now restart the service:  systemctl restart mfapi")


if __name__ == "__main__":
    main(sys.argv[1:])
