#!/usr/bin/env python3
"""LOCAL SETUP UTILITY — get the BiteWise customer-support Gmail refresh token.

THIS IS A DEVELOPER TOOL FOR YOUR OWN MACHINE ONLY.
It is NOT part of the running application:
- The application never imports or executes this file.
- It does NOT run automatically, and it refuses to run in a production
  environment (e.g. when RAILWAY_* variables are present) or in a
  non-interactive terminal.
- It does NOT touch GMAIL_REFRESH_TOKEN (the OTP token). It only authorizes
  mybitewise@gmail.com for the gmail.modify scope and prints the new
  support refresh token so you can paste it into Railway as
  GMAIL_SUPPORT_REFRESH_TOKEN.

What it does:
1. Reads GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET from the environment (or a
   .env file found next to the project's backend directory). The client
   secret is NEVER hardcoded and NEVER printed.
2. Opens the Google consent screen for mybitewise@gmail.com with scope
   https://www.googleapis.com/auth/gmail.modify (offline access, forced
   consent so a refresh token is always issued).
3. Catches the redirect on a local one-shot HTTP server on 127.0.0.1.
4. Exchanges the authorization code for tokens.
5. Prints the REFRESH TOKEN (the only value you need to copy) and writes it
   to a file OUTSIDE this repository with restrictive permissions.
6. Optional --verify: confirms the token works with Gmail and belongs to
   the account mybitewise@gmail.com.

Security rules:
- The client secret, access token, id token and OAuth state are never printed.
- Nothing is written inside the repository by default.
- The refresh token is written to a temp file with 0o600 permissions; delete
  the file after you have pasted the value into Railway.

Usage (from the backend/ directory):
    .\\venv\\Scripts\\python.exe tools\\get_support_token.py
    .\\venv\\Scripts\\python.exe tools\\get_support_token.py --verify
    .\\venv\\Scripts\\python.exe tools\\get_support_token.py --out C:\\Users\\You\\bitewise-support-token.txt

Environment needed before running:
    GMAIL_CLIENT_ID
    GMAIL_CLIENT_SECRET
(For the Google Cloud project nutrilens-ai-fd9fb / your project's OAuth client.)

The refresh token you obtain must be stored ONLY as GMAIL_SUPPORT_REFRESH_TOKEN
in Railway. It must NOT replace GMAIL_REFRESH_TOKEN (OTP).
"""

import json
import os
import sys
import tempfile
import threading
import webbrowser
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

SCOPE = "https://www.googleapis.com/auth/gmail.modify"
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
EXPECTED_ACCOUNT = "mybitewise@gmail.com"

_REDIRECT_PATH = "/"

# The marker string that must never appear in logs/stdout of the app. This
# script intentionally prints only the refresh token value.
_HINT = ("[bitewise-oauth] This is a LOCAL developer tool. It must not be run "
         "inside Railway or on a server.")


def _env(name):
    return os.getenv(name, "").strip()


def _is_interactive():
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def _looks_like_production():
    for key in os.environ:
        if key.startswith("RAILWAY_") or key in ("FLY_APP_NAME", "HEROKU_APP_NAME"):
            return True
    return False


def _load_dotenv_if_present(start_dir):
    """Best-effort .env loading so the script works with env vars or .env."""
    try:
        from dotenv import load_dotenv
        for base in (start_dir, os.getcwd()):
            candidate = os.path.join(base, ".env")
            if os.path.isfile(candidate):
                load_dotenv(candidate)
                return
    except Exception:
        pass


def _write_token_file(value, out_path):
    path = out_path if out_path else os.path.join(
        tempfile.gettempdir(), "bitewise-support-refresh-token.txt")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value.strip() + "\n")
        os.chmod(path, 0o600)
        return path
    except OSError as e:
        print(f"[bitewise-oauth] could not write token file: {e}")
        return None


class _RedirectHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != _REDIRECT_PATH:
            self.send_error(404)
            return
        query = urllib.parse.parse_qs(parsed.query)
        self.server.auth_code = query.get("code", [None])[0]
        self.server.auth_error = query.get("error", [None])[0]
        html = (
            "<html><body><p>BiteWise OAuth complete. "
            "You can close this tab and return to the terminal.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html.encode())))
        self.end_headers()
        self.wfile.write(html.encode())

    def log_request(self, *args, **kwargs):
        pass


def _exchange_code(code, redirect_uri, client_id, client_secret):
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URI, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _authorize(client_id, client_secret):
    server = HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}{_REDIRECT_PATH}"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
    }
    auth_url = AUTH_URI + "?" + urllib.parse.urlencode(params)

    print()
    print("=" * 72)
    print("BiteWise SUPPORT TOKEN AUTHORIZATION (local developer tool)")
    print("=" * 72)
    print(f"Scope           : {SCOPE}")
    print(f"Account         : {EXPECTED_ACCOUNT}")
    print(f"Redirect        : {redirect_uri}")
    print("The OAuth window is opening in your browser.")
    print("Sign in as mybitewise@gmail.com and press Allow / Continue.")
    print()

    def _serve():
        server.handle_request()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        webbrowser.open(auth_url)
    except Exception:
        print("If the browser did not open automatically, paste this URL:")
        print(auth_url)
    thread.join(timeout=300)

    code = getattr(server, "auth_code", None)
    error = getattr(server, "auth_error", None)
    if error:
        print(f"[bitewise-oauth] Authorization failed: {error}")
        sys.exit(1)
    if not code:
        print("[bitewise-oauth] No authorization code received (timed out).")
        sys.exit(1)

    try:
        tokens = _exchange_code(code, redirect_uri, client_id, client_secret)
    except Exception as e:
        print(f"[bitewise-oauth] Token exchange failed: {type(e).__name__} — "
              "check GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET and that the OAuth "
              "client allows the redirect URI used above (http://127.0.0.1 redirects "
              "are allowed by default for Web clients).")
        sys.exit(1)

    refresh = (tokens.get("refresh_token") or "").strip()
    if not refresh:
        print("[bitewise-oauth] No refresh_token in the response. The Google "
              "client may not have offline access enabled; re-run and complete "
              "the consent (prompt=consent is forced).")
        sys.exit(1)

    scopes = tokens.get("scope", "").split()
    if SCOPE not in scopes:
        print(f"[bitewise-oauth] Unexpected scopes granted: {scopes}. "
              f"Expected: {SCOPE}")
        sys.exit(1)

    return refresh


def _verify(refresh_token, client_id, client_secret):
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        print("[bitewise-oauth] googleapiclient not available; skipping verify. "
              "Install:  .\\venv\\Scripts\\python.exe -m pip install google-api-python-client")
        return False
    creds = Credentials(
        token=None, refresh_token=refresh_token,
        client_id=client_id, client_secret=client_secret,
        token_uri=TOKEN_URI, scopes=[SCOPE],
    )
    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        profile = service.users().getProfile(userId="me").execute()
        email = (profile or {}).get("emailAddress", "")
        if email != EXPECTED_ACCOUNT:
            print(f"[bitewise-oauth] WARNING: token belongs to {email}, "
                  f"expected {EXPECTED_ACCOUNT}")
            return False
        print(f"[bitewise-oauth] verified: gmail.modify works for {email}")
        return True
    except Exception as e:
        print(f"[bitewise-oauth] verify failed: {type(e).__name__} — "
              "the token may be invalid or the scope was not granted.")
        return False


def main():
    if _looks_like_production():
        print(_HINT)
        print("[bitewise-oauth] Refusing to run: production environment "
              "(RAILWAY_* / platform vars detected). Delete this script from "
              "deployment or use a fresh token on your own machine.")
        sys.exit(2)
    if not _is_interactive() and "--force" not in sys.argv:
        print(_HINT)
        print("[bitewise-oauth] Refusing to run: no interactive terminal. "
              "Use --force only if you know what you are doing (not recommended).")
        sys.exit(2)

    _load_dotenv_if_present(os.path.dirname(os.path.abspath(__file__)))

    client_id = _env("GMAIL_CLIENT_ID")
    client_secret = _env("GMAIL_CLIENT_SECRET")
    if not (client_id and client_secret):
        print("[bitewise-oauth] GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be "
              "set in the environment (or backend/.env). They are read only; "
              "they are never printed or committed.")
        sys.exit(1)

    out_path = None
    if "--out" in sys.argv:
        i = sys.argv.index("--out")
        if i + 1 < len(sys.argv):
            out_path = sys.argv[i + 1]

    run_verify = "--verify" in sys.argv

    refresh = _authorize(client_id, client_secret)
    path = _write_token_file(refresh, out_path)

    print()
    print("=" * 72)
    print("SUPPORT REFRESH TOKEN OBTAINED (for GMAIL_SUPPORT_REFRESH_TOKEN)")
    print("=" * 72)
    print()
    print(refresh)
    print()
    print("This token is a SECRET. It was granted to mybitewise@gmail.com with")
    print(f"scope {SCOPE}.")
    print("1. Copy it into Railway as  GMAIL_SUPPORT_REFRESH_TOKEN  (only).")
    print("2. Do NOT put it in GMAIL_REFRESH_TOKEN (that is the OTP token).")
    print("3. Do NOT commit it, paste it in issues, or log it.")
    if path:
        print(f"4. A copy was saved to: {path} (permissions 0600). Delete this "
              "file after pasting the value into Railway.")
    print("=" * 72)

    if run_verify:
        _verify(refresh, client_id, client_secret)


if __name__ == "__main__":
    main()