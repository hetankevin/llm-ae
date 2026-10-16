"""Native browser authorization for mailbox access, with PKCE and keychain refresh."""

import base64
import hashlib
import json
import math
import os
import re
import secrets
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

from . import credentials
from .config import AEError
from .http import urlopen

PROVIDERS = {
    "google": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scopes": ["https://mail.google.com/"],
        "label": "Google",
    },
    "microsoft": {
        "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": [
            "https://outlook.office.com/IMAP.AccessAsUser.All",
            "https://outlook.office.com/SMTP.Send",
            "offline_access",
        ],
        "label": "Microsoft",
    },
}


def validate_client(provider, data):
    if provider not in PROVIDERS or not isinstance(data, dict):
        raise AEError("Invalid mailbox application registration.")
    client_id = data.get("client_id", "")
    secret = data.get("client_secret", "")
    pattern = (
        r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com"
        if provider == "google"
        else r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    )
    if not isinstance(client_id, str) or not re.fullmatch(pattern, client_id):
        raise AEError("Invalid mailbox application client ID.")
    if not isinstance(secret, str) or any(ord(c) < 33 or ord(c) > 126 for c in secret):
        raise AEError("Invalid desktop client credential.")
    if provider == "microsoft" and secret:
        raise AEError("Microsoft desktop login must use a public client without a client secret.")
    return {"client_id": client_id, "client_secret": secret}


def registration(provider):
    """Publisher-supplied desktop registrations. End users never create an app."""
    if provider not in PROVIDERS:
        raise AEError("Choose Gmail or Outlook for browser sign-in.")
    override = os.environ.get("LLM_AE_OAUTH_CLIENTS_FILE")
    try:
        source = Path(override) if override else files("llm_ae").joinpath("oauth_clients.json")
        data = json.loads(source.read_text(encoding="utf-8"))
        value = data.get(provider, {})
        if not value or not value.get("client_id"):
            raise AEError(
                f"{PROVIDERS[provider]['label']} sign-in is awaiting LLM-AE publisher activation. "
                "No app password is needed. Update LLM-AE after the publisher enables this connection."
            )
        return validate_client(provider, value)
    except (OSError, ValueError, AttributeError) as exc:
        raise AEError(
            "Mailbox sign-in configuration is invalid. Contact the LLM-AE publisher."
        ) from exc


def authorization_code(provider, client, email, timeout=180):
    """Listen on loopback only. Ignore unrelated/invalid callbacks until the deadline."""
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(2)
            super().setup()

        def log_message(self, *args):
            pass  # Callback query strings contain an authorization code.

        def do_GET(self):
            if len(self.path) > 20000:
                self.reply(400, "Invalid sign-in response.")
                return
            try:
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
            except ValueError:
                self.reply(400, "Invalid sign-in response.")
                return
            if parsed.path != "/callback" or self.headers.get("Host") != expected_host:
                self.reply(404, "Not found.")
                return
            supplied = query.get("state", [])
            if len(supplied) != 1 or not secrets.compare_digest(
                supplied[0].encode(), state.encode()
            ):
                self.reply(400, "This sign-in does not match the active terminal session.")
                return
            if "error" in query:
                result["error"] = True
                self.reply(200, "Sign-in was not completed. Return to your terminal to try again.")
            elif (
                len(query.get("code", [])) == 1
                and query["code"][0]
                and len(query["code"][0]) < 16384
            ):
                result["code"] = query["code"][0]
                self.reply(
                    200,
                    "Sign-in received. Return to your terminal while LLM-AE checks the connection.",
                )
            else:
                self.reply(400, "Invalid sign-in response.")

        def reply(self, status, message):
            content = message.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
            )
            self.end_headers()
            try:
                self.wfile.write(content)
            except (TimeoutError, OSError):
                pass

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        # Microsoft registers http://localhost/callback and ignores its ephemeral port.
        host = "localhost" if provider == "microsoft" else "127.0.0.1"
        expected_host = f"{host}:{server.server_port}"
        redirect = f"http://{expected_host}/callback"
        parameters = {
            "client_id": client["client_id"],
            "response_type": "code",
            "redirect_uri": redirect,
            "scope": " ".join(PROVIDERS[provider]["scopes"]),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "login_hint": email,
            "prompt": "consent" if provider == "google" else "select_account",
        }
        if provider == "google":
            parameters["access_type"] = "offline"
        url = PROVIDERS[provider]["authorize"] + "?" + urlencode(parameters)
        print(
            f"Opening {PROVIDERS[provider]['label']} sign-in. Sign in as {email} and approve email access."
        )
        if not webbrowser.open(url):
            print("Open this link in a browser on this computer:\n" + url)
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.timeout = min(0.5, max(0, deadline - time.monotonic()))
            server.handle_request()
    if result.get("error"):
        raise AEError(
            "Email sign-in was declined or blocked by your organization. Rerun setup to try again."
        )
    if not result.get("code"):
        raise AEError("Email sign-in timed out. Rerun setup and finish sign-in on this computer.")
    return result["code"], verifier, redirect


def token_request(provider, data):
    request = Request(
        PROVIDERS[provider]["token"],
        data=urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read(131073)
        if len(raw) > 131072:
            raise ValueError("oversized response")
        result = json.loads(raw)
        if not isinstance(result, dict) or "error" in result:
            raise ValueError("invalid response")
        return result
    except HTTPError as exc:
        # Never show response bodies, request data, or provider diagnostic strings.
        if exc.code in (400, 401, 403):
            raise AEError(
                "Email authorization expired, was revoked, or was refused. Run llm-ae setup to reconnect."
            ) from exc
        raise AEError("The email sign-in service is unavailable. Try again later.") from exc
    except (URLError, OSError, ValueError) as exc:
        raise AEError(
            "Could not complete email sign-in. Check your connection and try again."
        ) from exc


def token_string(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 65536
        and all(33 <= ord(c) <= 126 for c in value)
    )


def normalized_tokens(provider, response, previous_refresh=""):
    access = response.get("access_token")
    refresh = response.get("refresh_token", previous_refresh)
    duration = response.get("expires_in")
    if (
        not token_string(access)
        or not token_string(refresh)
        or str(response.get("token_type", "")).lower() != "bearer"
        or type(duration) not in (int, float)
        or not math.isfinite(duration)
        or duration <= 60
    ):
        raise AEError(
            "Email sign-in returned incomplete credentials. Reconnect and approve ongoing access."
        )
    scope = response.get("scope")
    if scope is not None:
        if not isinstance(scope, str):
            raise AEError("Email sign-in returned invalid permissions.")
        granted = set(scope.split())
        required = set(PROVIDERS[provider]["scopes"]) - {"offline_access"}
        if provider == "microsoft":
            # Exchange may return short scope names or its office365 resource alias.
            granted = {s.rsplit("/", 1)[-1] for s in granted}
            required = {s.rsplit("/", 1)[-1] for s in required}
        if not required <= granted:
            raise AEError(
                "Reading and sending email permissions are required. Rerun setup and approve both."
            )
    return {"access_token": access, "refresh_token": refresh, "expires_at": time.time() + duration}


def sign_in(provider, email):
    client = registration(provider)
    credentials.backend()
    code, verifier, redirect = authorization_code(provider, client, email)
    parameters = {
        "client_id": client["client_id"],
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect,
    }
    if client["client_secret"]:
        parameters["client_secret"] = client["client_secret"]
    if provider == "microsoft":
        parameters["scope"] = " ".join(PROVIDERS[provider]["scopes"])
    response = token_request(provider, parameters)
    return {
        "provider": provider,
        "username": email,
        **client,
        **normalized_tokens(provider, response),
    }


def persist(config, record):
    reference = "oauth-" + uuid.uuid4().hex
    credentials.save(config, reference, json.dumps(record))
    return reference


def access_token(config):
    """Refresh before expiry and save rotated refresh tokens before returning access."""
    mail = config["mail"]
    provider = mail["oauth_provider"]
    validate_mail_settings(mail)
    try:
        record = json.loads(credentials.get(config, mail["oauth_keyring"]))
        if (
            record["provider"] != provider
            or record["username"] != mail["username"]
            or not token_string(record["access_token"])
            or not token_string(record["refresh_token"])
            or type(record["expires_at"]) not in (int, float)
            or not math.isfinite(record["expires_at"])
        ):
            raise ValueError("credential mismatch")
        validate_client(provider, record)
    except (KeyError, TypeError, ValueError) as exc:
        raise AEError(
            "Email credentials are invalid or belong to a different mailbox. Run llm-ae setup."
        ) from exc
    if record["expires_at"] <= time.time() + 120:
        parameters = {
            "client_id": record["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": record["refresh_token"],
        }
        if record.get("client_secret"):
            parameters["client_secret"] = record["client_secret"]
        if provider == "microsoft":
            parameters["scope"] = " ".join(PROVIDERS[provider]["scopes"])
        response = token_request(provider, parameters)
        record.update(normalized_tokens(provider, response, record["refresh_token"]))
        credentials.save(config, mail["oauth_keyring"], json.dumps(record))
    return record["access_token"]


def mail_settings(provider, email):
    if provider == "google":
        host, smtp, port, security = "imap.gmail.com", "smtp.gmail.com", 465, "ssl"
    elif provider == "microsoft":
        host, smtp, port, security = "outlook.office365.com", "smtp.office365.com", 587, "starttls"
        if email.rsplit("@", 1)[-1] in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
            smtp = "smtp-mail.outlook.com"
    else:
        raise AEError("Invalid browser email provider.")
    return {
        "oauth_provider": provider,
        "oauth_token_env": "",
        "password_keyring": "",
        "password_env": "",
        "username": email,
        "imap_host": host,
        "imap_port": 993,
        "imap_folder": "INBOX",
        "smtp_host": smtp,
        "smtp_port": port,
        "smtp_security": security,
        "transport": "smtp",
    }


def validate_mail_settings(mail):
    """Never send a provider token to an arbitrary server from a changed config."""
    provider = mail["oauth_provider"]
    expected = mail_settings(provider, mail["username"])
    for key in ("imap_host", "imap_port", "smtp_port", "smtp_security"):
        if mail[key] != expected[key]:
            raise AEError("Browser email settings do not match the provider. Run llm-ae setup.")
    allowed_smtp = (
        {"smtp.office365.com", "smtp-mail.outlook.com"}
        if provider == "microsoft"
        else {"smtp.gmail.com"}
    )
    if mail["smtp_host"] not in allowed_smtp:
        raise AEError("Browser email settings do not match the provider. Run llm-ae setup.")
