"""
Zerodha Login Module - WITH AUTO-LOGIN OPTIONS
Tries: Saved Token -> Playwright Auto -> Manual Fallback

WHAT CHANGED, AND WHY EACH CHANGE WAS NEEDED ON A SERVER
    This module worked on a desktop and fell back to manual login on the
    droplet. Four reasons, all of them server-specific:

    1. HEADLESS.  The browser was launched with headless=False, which
       needs a screen. A droplet has none, so Chromium failed to start,
       the exception was caught, and the code quietly fell through to
       "paste the URL here". Now the default follows the machine: no
       DISPLAY means headless. ZERODHA_HEADLESS=0 forces a visible
       window when you are testing on your own laptop.

    2. ROOT.  Chromium refuses to run as root without --no-sandbox.
       Everything on this server runs as root, so without that flag the
       browser never launches at all.

    3. TOKEN CAPTURE.  The old code read the request token out of
       Chrome's error page after the redirect to 127.0.0.1 failed. That
       error page exists only in a visible browser -- headless has no
       such page to read, so the token vanished even on a successful
       login. Now a request listener captures the redirect URL the
       moment the browser ATTEMPTS it, before the connection is refused.
       That works in both modes and does not depend on how Chrome
       renders a failure.

    4. NO HANGING AT 2AM.  manual_login_with_live_totp() calls input().
       Under cron or nohup there is no keyboard attached, so that either
       crashes obscurely or waits forever while the job appears to run.
       Unattended runs now stop immediately with an explanation instead.

    The token file also moved next to this script rather than living in
    whatever directory you happened to be standing in when you ran it --
    cron starts somewhere else, and a saved token nobody can find is the
    same as no saved token.

WHAT IS UNCHANGED
    The public interface: ZerodhaAuth, and get_session(auto_login=True).
    Anything importing this module keeps working as before.
"""

import os
import json
import sys
from datetime import datetime
from kiteconnect import KiteConnect
import pyotp
import webbrowser
import threading
import time

# Anchored to this file, not to the current directory. cron runs from
# somewhere else entirely, and a token saved to the wrong folder makes
# every unattended run look like a first login.
HERE = os.path.dirname(os.path.abspath(__file__))


def _headless_default():
    """Visible browser on a desktop, headless on a server.

    ZERODHA_HEADLESS wins when set (0/false/no -> visible). Otherwise:
    no DISPLAY means no screen to draw on, which is exactly the droplet.
    """
    env = os.getenv("ZERODHA_HEADLESS")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off")
    return not bool(os.environ.get("DISPLAY"))


def _interactive():
    """Is there actually a human at a keyboard?

    Checked before anything asks a question. Under nohup or cron stdin
    is not a terminal, and a prompt there is not a prompt -- it is a
    hang, or an EOFError three hours into a run.
    """
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


class ZerodhaAuth:
    def __init__(self):
        """Initialize Zerodha authentication"""
        self.api_key = os.getenv('ZERODHA_API_KEY')
        self.api_secret = os.getenv('ZERODHA_API_SECRET')
        self.user_id = os.getenv('ZERODHA_USER_ID')
        self.password = os.getenv('ZERODHA_PASSWORD')
        self.totp_secret = os.getenv('ZERODHA_TOTP_SECRET')

        self.kite = None
        self.access_token = None
        self.token_file = os.path.join(HERE, 'zerodha_token.json')

    def validate_credentials(self):
        """Validate that all credentials are present"""
        missing = []

        if not self.api_key:
            missing.append('ZERODHA_API_KEY')
        if not self.api_secret:
            missing.append('ZERODHA_API_SECRET')
        if not self.totp_secret:
            missing.append('ZERODHA_TOTP_SECRET')

        if missing:
            print(f"\n[X] Missing environment variables:")
            for var in missing:
                print(f"   - {var}")
            print(f"\nPlease add these to your .env file")
            return False

        # NOT fatal, but it decides whether auto-login is even possible.
        # Said out loud here, because the old code skipped auto-login in
        # silence when these were absent and the run just looked slow.
        if not self.user_id or not self.password:
            missing_auto = [n for n, v in
                            (('ZERODHA_USER_ID', self.user_id),
                             ('ZERODHA_PASSWORD', self.password)) if not v]
            print("\n[!] Auto-login needs " + " and ".join(missing_auto) + ".")
            print("    Without them only manual login is possible, which")
            print("    cannot work from cron.")

        return True

    def load_saved_token(self):
        """Load previously saved access token"""
        if not os.path.exists(self.token_file):
            return None

        try:
            with open(self.token_file, 'r') as f:
                data = json.load(f)

            # Check if token is still valid (expires in 24 hours)
            saved_time = datetime.fromisoformat(data['timestamp'])
            hours_old = (datetime.now() - saved_time).total_seconds() / 3600

            if hours_old < 23:
                print(f"[ok] Found saved token ({hours_old:.1f} hours old)")
                return data['access_token']
            else:
                print(f"[!] Saved token expired ({hours_old:.1f} hours old)")
                return None
        except Exception as e:
            print(f"[!] Could not load saved token: {e}")
            return None

    def save_token(self, access_token):
        """Save access token for future use"""
        try:
            data = {
                'access_token': access_token,
                'timestamp': datetime.now().isoformat()
            }
            with open(self.token_file, 'w') as f:
                json.dump(data, f)
            print(f"[ok] Access token saved (valid for 24 hours)")
            print(f"     {self.token_file}")
        except Exception as e:
            print(f"[!] Could not save token: {e}")

    def generate_totp(self):
        """Generate TOTP code for 2FA"""
        try:
            totp = pyotp.TOTP(self.totp_secret)
            code = totp.now()
            return code
        except Exception as e:
            print(f"[X] Error generating TOTP: {e}")
            return None

    def auto_login_playwright(self, headless=None):
        """Auto-login using Playwright.

        headless=None means "decide from the machine" -- see
        _headless_default(). Pass True or False to override.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        if headless is None:
            headless = _headless_default()

        print("\n" + "=" * 70)
        print("AUTO-LOGIN (Playwright)"
              f"   [{'headless' if headless else 'visible browser'}]")
        print("=" * 70)

        login_url = self.kite.login_url()

        # THE TOKEN CATCHER.
        #
        # Zerodha finishes login by redirecting to the app's registered
        # URL with ?request_token=... on it. That URL is usually
        # 127.0.0.1, where nothing is listening, so the navigation fails
        # and the browser shows an error page.
        #
        # The token is still in the URL the browser TRIED to open, and
        # Playwright reports every attempt through this event -- whether
        # or not it succeeds, and whether or not there is a screen. That
        # makes this the one place the token can always be read from.
        captured = {"token": None}

        def _catch(url):
            if not url or captured["token"] or "request_token=" not in url:
                return
            try:
                captured["token"] = url.split("request_token=")[1].split("&")[0]
            except Exception:
                pass

        try:
            with sync_playwright() as p:
                print("\nStep 1/5: Launching browser...")
                # --no-sandbox: Chromium will not start as root without
                # it, and everything on this server runs as root.
                # --disable-dev-shm-usage: containers and small droplets
                # give /dev/shm 64MB, which Chromium exhausts and then
                # crashes mid-page for no visible reason.
                browser = p.chromium.launch(
                    headless=headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                page = browser.new_page()

                page.on("request", lambda r: _catch(r.url))
                page.on("framenavigated", lambda f: _catch(f.url))

                print("Step 2/5: Opening Zerodha login page...")
                page.goto(login_url, timeout=30000)
                time.sleep(3)

                print("Step 3/5: Entering User ID and password...")
                try:
                    page.fill('input[id="userid"]', self.user_id)
                except Exception:
                    try:
                        page.fill('input[type="text"]', self.user_id)
                    except Exception:
                        page.locator('input').first.fill(self.user_id)
                time.sleep(1)

                try:
                    page.fill('input[id="password"]', self.password)
                except Exception:
                    try:
                        page.fill('input[type="password"]', self.password)
                    except Exception:
                        page.click('input[type="password"]')
                        time.sleep(0.5)
                        page.keyboard.type(self.password)
                time.sleep(1)

                print("Step 4/5: Submitting credentials...")
                try:
                    page.click('button[type="submit"]')
                except Exception:
                    try:
                        page.click('button:has-text("Login")')
                    except Exception:
                        try:
                            page.click('.button-orange')
                        except Exception:
                            page.keyboard.press('Enter')

                # Wait for the TOTP field BEFORE generating the code.
                #
                # The old order generated it first and then waited, which
                # could burn most of a 30-second window before the code
                # was ever typed. A TOTP that expires between generation
                # and submission fails in a way that looks exactly like a
                # wrong password.
                print("Step 5/5: Waiting for the 2FA field...")
                try:
                    page.wait_for_selector(
                        'input[type="tel"], input[id="totp"], '
                        'input[placeholder*="TOTP"]', timeout=15000)
                    print("         2FA field found")
                except Exception:
                    print("         [!] 2FA field not found, trying anyway")

                totp_code = self.generate_totp()
                if not totp_code:
                    browser.close()
                    return None

                totp_filled = False
                for how, fn in (
                    ("type=tel",
                     lambda: page.fill('input[type="tel"]', totp_code, timeout=3000)),
                    ("id=totp",
                     lambda: page.fill('input[id="totp"]', totp_code, timeout=3000)),
                    ("keyboard",
                     lambda: page.keyboard.type(totp_code)),
                ):
                    try:
                        fn()
                        totp_filled = True
                        print(f"         2FA entered via {how}")
                        break
                    except Exception:
                        continue

                if not totp_filled:
                    print("         [X] Could not enter the 2FA code.")
                    try:
                        page.screenshot(path=os.path.join(HERE, 'login_debug.png'))
                        print(f"         Screenshot: {HERE}/login_debug.png")
                    except Exception:
                        pass
                    browser.close()
                    return None

                time.sleep(0.5)
                submitted = False
                for fn in (
                    lambda: page.click('button[type="submit"]', timeout=2000),
                    lambda: page.click('button:has-text("Continue")', timeout=2000),
                    lambda: page.keyboard.press('Enter'),
                ):
                    try:
                        fn()
                        submitted = True
                        break
                    except Exception:
                        continue
                if submitted:
                    print("         2FA submitted")

                print("         Waiting for redirect...")
                for _ in range(30):          # up to 15 seconds
                    if captured["token"]:
                        break
                    time.sleep(0.5)
                    _catch(page.url)

                if not captured["token"]:
                    _catch(page.url)

                if not captured["token"]:
                    # Worth a picture. In headless mode this is the only
                    # way to see whether Zerodha showed a captcha, a
                    # password error, or something new on the page.
                    try:
                        page.screenshot(path=os.path.join(HERE, 'login_debug.png'))
                        print(f"         [X] No request token. "
                              f"Screenshot: {HERE}/login_debug.png")
                    except Exception:
                        print("         [X] No request token.")

                browser.close()

        except Exception as e:
            print(f"\n[!] Auto-login error: {e}")
            return None

        if captured["token"]:
            print(f"\n[ok] AUTO-LOGIN SUCCESSFUL  "
                  f"(token {captured['token'][:12]}...)")
        return captured["token"]

    def manual_login_with_live_totp(self):
        """Manual login with live TOTP updates.

        Refuses to start when nothing is attached to the keyboard --
        see _interactive(). A prompt in an unattended run is not a
        prompt, it is a silent hang.
        """
        if not _interactive():
            print("\n" + "=" * 70)
            print("[X] LOGIN NEEDED, BUT THIS RUN HAS NO KEYBOARD")
            print("=" * 70)
            print("  Auto-login did not produce a token and there is no")
            print("  terminal to ask on, so this run stops here rather")
            print("  than waiting forever for an answer.")
            print("\n  Fix it once, by hand, and every later run is covered")
            print("  for 24 hours:")
            print("     cd /opt/mfapi && venv/bin/python3 "
                  "zerodha_login_with_auto.py")
            print("=" * 70 + "\n")
            return None

        print("\n" + "=" * 70)
        print("MANUAL LOGIN")
        print("=" * 70)

        login_url = self.kite.login_url()

        print("\nSTEP 1: Opening login page in your browser...")
        print(f"   URL: {login_url}")

        try:
            webbrowser.open(login_url)
            print("   Browser opened automatically\n")
        except Exception:
            print("   Could not open a browser")
            print(f"   Please open this URL manually: {login_url}\n")

        if self.user_id:
            print("STEP 2: Login with your credentials")
            print(f"   User ID: {self.user_id}\n")
        else:
            print("STEP 2: Login with your Zerodha credentials\n")

        print("=" * 70)
        print("STEP 3: Use this code (refreshes every 5 seconds)")
        print("=" * 70)

        stop_totp = threading.Event()

        def totp_updater():
            while not stop_totp.is_set():
                fresh = self.generate_totp()
                now = datetime.now().strftime("%H:%M:%S")
                print(f"\r   code: {fresh}  (as of {now})  ", end='', flush=True)
                time.sleep(5)

        updater = threading.Thread(target=totp_updater, daemon=True)
        updater.start()

        print()
        time.sleep(1)

        print("\n\n" + "=" * 70)
        print("STEP 4: After login, paste the redirect URL below")
        print("=" * 70)
        print("   The URL looks like:")
        print("   http://127.0.0.1/?request_token=XXXXXXXX&action=login")
        print("=" * 70 + "\n")

        try:
            redirect_url = input("Paste full URL here: ").strip()
        except (EOFError, KeyboardInterrupt):
            stop_totp.set()
            print("\nCancelled.\n")
            return None

        stop_totp.set()
        print()

        if not redirect_url:
            print("[X] No URL provided\n")
            return None

        if 'request_token=' not in redirect_url:
            print("[X] Invalid URL - missing request_token\n")
            print(f"   Got: {redirect_url[:100]}...\n")
            return None

        try:
            request_token = redirect_url.split('request_token=')[1].split('&')[0]
            print(f"[ok] Request token extracted\n")
            return request_token
        except Exception as e:
            print(f"[X] Could not extract token: {e}\n")
            return None

    def authenticate(self, auto_login=True):
        """
        Main authentication method
        Tries: Saved Token -> Auto-login (Playwright) -> Manual Fallback
        """
        if not self.validate_credentials():
            return None

        self.kite = KiteConnect(api_key=self.api_key)

        # STEP 1: saved token
        print("\nChecking for saved token...")
        saved_token = self.load_saved_token()

        if saved_token:
            self.kite.set_access_token(saved_token)
            try:
                profile = self.kite.profile()
                print(f"[ok] Using saved token")
                print(f"     User: {profile['user_name']}  "
                      f"({profile['user_id']})\n")
                return self.kite
            except Exception as e:
                print(f"[!] Saved token invalid: {e}")
                print("    Generating a new one...\n")

        # STEP 2: auto-login
        request_token = None

        if auto_login and self.user_id and self.password:
            try:
                from playwright.sync_api import sync_playwright  # noqa: F401
                request_token = self.auto_login_playwright()
            except ImportError:
                print("[!] Playwright is not installed, so auto-login is")
                print("    not possible. Install it with:")
                print("      /opt/mfapi/venv/bin/pip install playwright")
                print("      /opt/mfapi/venv/bin/playwright install chromium")
                print("      /opt/mfapi/venv/bin/playwright install-deps chromium\n")
            except Exception as e:
                print(f"[!] Auto-login failed: {e}\n")

        # STEP 3: manual fallback (refuses when unattended)
        if not request_token:
            request_token = self.manual_login_with_live_totp()

        if not request_token:
            print("[X] Login failed - no request token\n")
            return None

        try:
            print("Generating access token...")
            data = self.kite.generate_session(request_token,
                                              api_secret=self.api_secret)
            self.access_token = data['access_token']
            self.kite.set_access_token(self.access_token)

            self.save_token(self.access_token)

            profile = self.kite.profile()
            print(f"\n[ok] Login successful!")
            print(f"     User: {profile['user_name']}  ({profile['user_id']})")
            print(f"\n     Token saved. No login needed for 24 hours.\n")

            return self.kite

        except Exception as e:
            print(f"\n[X] Failed to generate session: {e}")
            print(f"    Usually one of:")
            print(f"    - the request token expired (login took too long)")
            print(f"    - a network problem")
            print(f"    - wrong API key or secret\n")
            return None


def get_session(auto_login=True):
    """
    Get authenticated Kite session.

    Returns an authenticated KiteConnect object, or None.

    Flow:
      1. saved token (< 23 hours old)
      2. Playwright auto-login, headless on a server, visible on a desktop
      3. manual login -- only when a terminal is actually attached
    """
    auth = ZerodhaAuth()
    return auth.authenticate(auto_login=auto_login)


if __name__ == "__main__":
    from dotenv import load_dotenv

    print("\n" + "=" * 70)
    print("Zerodha Authentication Test")
    print("=" * 70)

    load_dotenv(os.path.join(HERE, ".env"))
    load_dotenv()

    print(f"  browser mode : "
          f"{'headless' if _headless_default() else 'visible'}")
    print(f"  terminal     : "
          f"{'yes' if _interactive() else 'no (unattended)'}")

    kite = get_session(auto_login=True)

    print("=" * 70)
    print("Authentication test " + ("SUCCEEDED" if kite else "FAILED"))
    print("=" * 70 + "\n")
