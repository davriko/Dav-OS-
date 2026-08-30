#!/usr/bin/env python3
"""One-time helper: turn a Google OAuth desktop client into a refresh token.

Run this once on your own machine. It opens a browser, asks you to grant
calendar access to your own OAuth client, and prints the refresh token you
then store as the GOOGLE_REFRESH_TOKEN repository secret.

    python3 scripts/google_oauth_setup.py --client-id ... --client-secret ...

The refresh token is printed to your terminal and never written to disk.
"""

from __future__ import annotations

import argparse
import http.server
import secrets
import threading
import urllib.parse
import webbrowser

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar"
PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{PORT}/"

received: dict[str, str] = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - required name from BaseHTTPRequestHandler
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        received.update({k: v[0] for k, v in query.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = ("<h2>Done.</h2><p>Return to your terminal for the refresh token.</p>"
                if "code" in received else
                f"<h2>Authorisation failed.</h2><p>{received.get('error', 'unknown error')}</p>")
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):  # silence the default request logging
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    args = parser.parse_args()

    state = secrets.token_urlsafe(16)
    params = {
        "client_id": args.client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        # Without this Google returns a refresh token only on the very first
        # consent, and re-running the helper would silently yield none.
        "prompt": "consent",
        "state": state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print(f"Opening your browser. If nothing happens, visit:\n\n{url}\n")
    webbrowser.open(url)

    while "code" not in received and "error" not in received:
        server.handle_request()
    server.shutdown()

    if "error" in received:
        print(f"Authorisation failed: {received['error']}")
        return 1
    if received.get("state") != state:
        print("State mismatch - aborting.")
        return 1

    response = requests.post(TOKEN_URL, data={
        "code": received["code"],
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=30)
    response.raise_for_status()
    refresh_token = response.json().get("refresh_token")

    if not refresh_token:
        print("Google returned no refresh token. Revoke the app at "
              "https://myaccount.google.com/permissions and run this again.")
        return 1

    print("\nStore this as the GOOGLE_REFRESH_TOKEN repository secret:\n")
    print(refresh_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
