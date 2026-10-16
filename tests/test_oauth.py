import base64
import contextlib
import hashlib
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit

from llm_ae import mail, oauth, setup
from llm_ae.config import AEError, Config, initialize

CLIENTS = {
    "google": {"client_id": "test-only.apps.googleusercontent.com", "client_secret": "desktop"},
    "microsoft": {"client_id": "00000000-0000-0000-0000-000000000001", "client_secret": ""},
}


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = initialize(Path(self.tmp.name))
        self.values = {}
        keychain = Mock()
        keychain.get_password.side_effect = lambda service, ref: self.values.get(ref)
        keychain.set_password.side_effect = lambda service, ref, val: self.values.update({ref: val})
        keychain.delete_password.side_effect = lambda service, ref: self.values.pop(ref, None)
        self.keychain = patch("llm_ae.credentials.backend", return_value=keychain)
        self.keychain.start()
        self.output = io.StringIO()

    def tearDown(self):
        self.keychain.stop()
        self.tmp.cleanup()

    def response(self, **extra):
        return {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "expires_in": 3600,
            "token_type": "Bearer",
            **extra,
        }

    def record(self, provider="google"):
        return {
            "provider": provider,
            "username": "ae@example.org",
            **CLIENTS[provider],
            **oauth.normalized_tokens(provider, self.response()),
        }

    def connected(self, provider="google"):
        record = self.record(provider)
        reference = oauth.persist(self.config, record)
        self.config["mail"].update(
            oauth.mail_settings(provider, record["username"]), oauth_keyring=reference
        )
        return record, reference

    def test_actual_loopback_callback_pkce_state_host_and_secret_free_response(self):
        for provider in CLIENTS:
            with self.subTest(provider=provider):
                captured = {}
                errors = []
                threads = []

                def browser(url, captured=captured, errors=errors, threads=threads):
                    query = parse_qs(urlsplit(url).query)
                    captured.update(query)
                    redirect = urlsplit(query["redirect_uri"][0])

                    def callback():
                        try:
                            connection = http.client.HTTPConnection(
                                "127.0.0.1", redirect.port, timeout=3
                            )
                            for path, host, expected in [
                                (
                                    "/callback?" + urlencode({"code": "evil", "state": "wrong"}),
                                    redirect.netloc,
                                    400,
                                ),
                                (
                                    "/callback?"
                                    + urlencode({"code": "evil", "state": query["state"][0]}),
                                    "evil.example",
                                    404,
                                ),
                                ("/favicon.ico", redirect.netloc, 404),
                                (
                                    "/callback?"
                                    + urlencode({"code": "auth-code", "state": query["state"][0]}),
                                    redirect.netloc,
                                    200,
                                ),
                            ]:
                                connection.request("GET", path, headers={"Host": host})
                                response = connection.getresponse()
                                body = response.read()
                                self.assertEqual(response.status, expected)
                                self.assertNotIn(b"auth-code", body)
                            connection.close()
                        except BaseException as exc:
                            errors.append(exc)

                    thread = threading.Thread(target=callback)
                    thread.start()
                    threads.append(thread)
                    return True

                with (
                    patch("llm_ae.oauth.webbrowser.open", side_effect=browser),
                    contextlib.redirect_stdout(self.output),
                ):
                    code, verifier, redirect = oauth.authorization_code(
                        provider, CLIENTS[provider], "ae@example.org", timeout=5
                    )
                for thread in threads:
                    thread.join(timeout=5)
                self.assertFalse(errors, errors)
                self.assertEqual(code, "auth-code")
                expected = (
                    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                    .rstrip(b"=")
                    .decode()
                )
                self.assertEqual(captured["code_challenge"], [expected])
                self.assertEqual(captured["code_challenge_method"], ["S256"])
                self.assertGreaterEqual(len(verifier), 43)
                self.assertNotIn("auth-code", self.output.getvalue())
                self.assertNotIn(verifier, self.output.getvalue())
                self.assertIn("http://", redirect)

    def test_timeout_and_browser_fallback(self):
        with (
            patch("llm_ae.oauth.webbrowser.open", return_value=False),
            contextlib.redirect_stdout(self.output),
        ):
            with self.assertRaisesRegex(AEError, "timed out"):
                oauth.authorization_code(
                    "google", CLIENTS["google"], "ae@example.org", timeout=0.01
                )
        self.assertIn("Open this link", self.output.getvalue())

    def test_sign_in_exchanges_code_with_verifier_and_no_token_on_disk(self):
        for provider in CLIENTS:
            with (
                self.subTest(provider=provider),
                patch("llm_ae.oauth.registration", return_value=CLIENTS[provider]),
                patch(
                    "llm_ae.oauth.authorization_code",
                    return_value=("code", "verifier", "http://localhost:1234/callback"),
                ),
                patch("llm_ae.oauth.token_request", return_value=self.response()) as request,
            ):
                record = oauth.sign_in(provider, "ae@example.org")
                oauth.persist(self.config, record)
                fields = request.call_args.args[1]
                self.assertEqual(fields["code_verifier"], "verifier")
                self.assertEqual(fields["grant_type"], "authorization_code")
                self.assertEqual(record["refresh_token"], "refresh-secret")
                if provider == "microsoft":
                    self.assertNotIn("client_secret", fields)
        self.assertNotIn("access-secret", (self.config.home / "config.toml").read_text())
        self.assertNotIn("refresh-secret", (self.config.home / "config.toml").read_text())

    def test_valid_token_reused_without_network(self):
        self.connected()
        with patch("llm_ae.oauth.token_request") as network:
            self.assertEqual(oauth.access_token(self.config), "access-secret")
        network.assert_not_called()

    def test_refresh_rotation_is_saved_and_uses_original_client_registration(self):
        for provider in CLIENTS:
            with self.subTest(provider=provider):
                record, reference = self.connected(provider)
                record["expires_at"] = time.time() - 1
                self.values[reference] = json.dumps(record)
                with patch(
                    "llm_ae.oauth.token_request",
                    return_value=self.response(
                        access_token="new-access", refresh_token="rotated-refresh"
                    ),
                ) as request:
                    self.assertEqual(oauth.access_token(self.config), "new-access")
                self.assertEqual(
                    json.loads(self.values[reference])["refresh_token"], "rotated-refresh"
                )
                self.assertEqual(
                    request.call_args.args[1]["client_id"], CLIENTS[provider]["client_id"]
                )
                self.assertEqual(request.call_args.args[1]["refresh_token"], "refresh-secret")

    def test_google_refresh_without_new_refresh_token_keeps_old_one(self):
        response = self.response()
        del response["refresh_token"]
        result = oauth.normalized_tokens("google", response, "original-refresh")
        self.assertEqual(result["refresh_token"], "original-refresh")

    def test_incomplete_tokens_and_partial_consent_are_rejected(self):
        for override in (
            {"refresh_token": ""},
            {"expires_in": float("nan")},
            {"access_token": "bad\x01token"},
            {"token_type": "other"},
            {"scope": "https://www.googleapis.com/auth/userinfo.email"},
        ):
            with self.subTest(override=override), self.assertRaises(AEError):
                oauth.normalized_tokens("google", self.response(**override))
        with self.assertRaisesRegex(AEError, "permissions"):
            oauth.normalized_tokens("microsoft", self.response(scope="IMAP.AccessAsUser.All"))
        oauth.normalized_tokens("microsoft", self.response(scope="IMAP.AccessAsUser.All SMTP.Send"))

    def test_mailbox_mismatch_rejected_before_network(self):
        self.connected()
        self.config["mail"]["username"] = "someone-else@example.org"
        with (
            patch("llm_ae.oauth.token_request") as network,
            self.assertRaisesRegex(AEError, "different mailbox"),
        ):
            oauth.access_token(self.config)
        network.assert_not_called()

    def test_changed_servers_never_receive_provider_token(self):
        self.connected()
        self.config["mail"]["imap_host"] = "evil.example.org"
        with (
            patch("llm_ae.mail.imaplib.IMAP4_SSL") as connect,
            self.assertRaisesRegex(AEError, "do not match"),
        ):
            mail.connect_imap(self.config)
        connect.assert_not_called()

    def test_refresh_error_is_sanitized_and_old_bundle_survives(self):
        record, ref = self.connected()
        record["expires_at"] = 0
        self.values[ref] = json.dumps(record)
        before = self.values[ref]
        error = HTTPError(
            "https://oauth2.googleapis.com/token",
            400,
            "private token",
            {},
            io.BytesIO(b"private token"),
        )
        with patch("llm_ae.oauth.urlopen", side_effect=error), self.assertRaises(AEError) as caught:
            oauth.access_token(self.config)
        self.assertNotIn("private token", str(caught.exception))
        self.assertEqual(before, self.values[ref])

    def test_token_request_is_form_post_and_rejects_oversized_response(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(self.response()).encode()
        with patch("llm_ae.oauth.urlopen", return_value=response) as network:
            oauth.token_request("google", {"code": "sensitive-code", "code_verifier": "verifier"})
        req = network.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertNotIn("sensitive-code", req.full_url)
        self.assertIn(b"code_verifier=verifier", req.data)
        response.read.return_value = b"x" * 131073
        with patch("llm_ae.oauth.urlopen", return_value=response), self.assertRaises(AEError):
            oauth.token_request("google", {})

    def test_xoauth2_on_both_mail_transports_and_no_password_fallback(self):
        for provider in CLIENTS:
            with self.subTest(provider=provider):
                self.connected(provider)
                imap, smtp = Mock(), Mock()
                with (
                    patch("llm_ae.mail.imaplib.IMAP4_SSL", return_value=imap),
                    patch("llm_ae.mail.smtplib.SMTP_SSL", return_value=smtp),
                    patch("llm_ae.mail.smtplib.SMTP", return_value=smtp),
                ):
                    mail.connect_imap(self.config)
                    mail.connect_smtp(self.config)
                imap.login.assert_not_called()
                smtp.login.assert_not_called()
                imap_callback = imap.authenticate.call_args.args[1]
                smtp_callback = smtp.auth.call_args.args[1]
                self.assertIn(b"auth=Bearer access-secret", imap_callback(b""))
                self.assertEqual(imap_callback(b'{"status":"401"}'), b"")
                self.assertIn("auth=Bearer access-secret", smtp_callback())
                self.assertEqual(smtp_callback(b"error"), "")

    def test_browser_connection_failure_rolls_back_new_bundle_and_config(self):
        before = (self.config.home / "config.toml").read_bytes()
        with (
            patch("llm_ae.oauth.sign_in", return_value=self.record()),
            patch("llm_ae.setup.check_mail", side_effect=AEError("wrong account")),
            contextlib.redirect_stdout(self.output),
        ):
            with self.assertRaisesRegex(AEError, "wrong account"):
                setup.browser_mailbox(self.config, "google", "ae@example.org")
        self.assertEqual(self.values, {})
        self.assertEqual((self.config.home / "config.toml").read_bytes(), before)

    def test_successful_browser_connection_persists_reference_and_clears_old_password(self):
        self.config["mail"]["password_keyring"] = "old-password"
        self.values["old-password"] = "old-secret"
        with (
            patch("llm_ae.oauth.sign_in", return_value=self.record()),
            patch("llm_ae.setup.check_mail"),
            contextlib.redirect_stdout(self.output),
        ):
            setup.browser_mailbox(self.config, "google", "ae@example.org")
        cfg = Config(self.config.home)
        self.assertEqual(cfg["mail"]["oauth_provider"], "google")
        self.assertEqual(cfg["mail"]["password_keyring"], "")
        self.assertNotIn("old-password", self.values)
        self.assertIn(cfg["mail"]["oauth_keyring"], self.values)

    def test_missing_publisher_registration_never_asks_user_for_client_id(self):
        path = self.config.home / "clients.json"
        path.write_text(json.dumps({"google": {"client_id": ""}}))
        with (
            patch.dict(os.environ, LLM_AE_OAUTH_CLIENTS_FILE=str(path)),
            self.assertRaisesRegex(AEError, "publisher activation"),
        ):
            oauth.registration("google")

    def test_full_wizard_uses_browser_tokens_for_gmail_and_outlook(self):
        from llm_ae.cli import main

        for provider, email, choice in [
            ("google", "ae@gmail.com", "gmail"),
            ("microsoft", "ae@outlook.com", "outlook"),
        ]:
            with self.subTest(provider=provider):
                home = self.config.home / provider
                imap, smtp = Mock(), Mock()
                imap.select.return_value = ("OK", [])
                with (
                    patch("sys.stdin.isatty", return_value=True),
                    patch(
                        "builtins.input",
                        side_effect=[
                            "offline",
                            email,
                            choice,
                            "Dr Example",
                            "Test Journal",
                            "editor@example.org",
                        ],
                    ),
                    patch("llm_ae.setup.getpass.getpass") as password,
                    patch("llm_ae.oauth.registration", return_value=CLIENTS[provider]),
                    patch(
                        "llm_ae.oauth.authorization_code",
                        return_value=("code", "verifier", "http://localhost:1234/callback"),
                    ),
                    patch("llm_ae.oauth.token_request", return_value=self.response()),
                    patch("llm_ae.mail.imaplib.IMAP4_SSL", return_value=imap),
                    patch("llm_ae.mail.smtplib.SMTP_SSL", return_value=smtp),
                    patch("llm_ae.mail.smtplib.SMTP", return_value=smtp),
                    contextlib.redirect_stdout(self.output),
                ):
                    self.assertEqual(main(["--home", str(home), "setup"]), 0)
                password.assert_not_called()
                smtp.send_message.assert_not_called()
                self.assertEqual(Config(home)["mail"]["oauth_provider"], provider)
                self.assertEqual(Config(home)["ae"]["email"], email)

    def test_publisher_script_accepts_desktop_registration_and_rejects_web_secret(self):
        from scripts.configure_oauth_clients import main

        source = self.config.home / "google.json"
        output = self.config.home / "public-clients.json"
        source.write_text(json.dumps({"installed": CLIENTS["google"]}))
        with contextlib.redirect_stdout(self.output):
            self.assertEqual(
                main(
                    [
                        "--google-desktop-json",
                        str(source),
                        "--microsoft-client-id",
                        CLIENTS["microsoft"]["client_id"],
                        "--output",
                        str(output),
                    ]
                ),
                0,
            )
        self.assertEqual(json.loads(output.read_text()), CLIENTS)
        before = output.read_bytes()
        source.write_text(json.dumps({"web": {"client_secret": "never-publish"}}))
        with contextlib.redirect_stderr(self.output):
            self.assertEqual(
                main(["--google-desktop-json", str(source), "--output", str(output)]), 1
            )
        self.assertEqual(before, output.read_bytes())
        self.assertNotIn("never-publish", self.output.getvalue())

    def test_declined_browser_consent_fails_without_exchanging_code(self):
        threads = []

        def browser(url):
            query = parse_qs(urlsplit(url).query)
            redirect = urlsplit(query["redirect_uri"][0])

            def callback():
                connection = http.client.HTTPConnection("127.0.0.1", redirect.port, timeout=3)
                connection.request(
                    "GET",
                    "/callback?"
                    + urlencode(
                        {
                            "state": query["state"][0],
                            "error": "access_denied",
                            "error_description": "private-diagnostic",
                        }
                    ),
                    headers={"Host": redirect.netloc},
                )
                connection.getresponse().read()
                connection.close()

            thread = threading.Thread(target=callback)
            thread.start()
            threads.append(thread)
            return True

        with (
            patch("llm_ae.oauth.registration", return_value=CLIENTS["google"]),
            patch("llm_ae.oauth.webbrowser.open", side_effect=browser),
            patch("llm_ae.oauth.token_request") as exchange,
            contextlib.redirect_stdout(self.output),
        ):
            with self.assertRaisesRegex(AEError, "declined") as error:
                oauth.sign_in("google", "ae@example.org")
        for thread in threads:
            thread.join(timeout=5)
        exchange.assert_not_called()
        self.assertNotIn("private-diagnostic", str(error.exception))

    def test_publisher_override_and_web_secret_rejected(self):
        path = self.config.home / "clients.json"
        path.write_text(json.dumps(CLIENTS))
        with patch.dict(os.environ, LLM_AE_OAUTH_CLIENTS_FILE=str(path)):
            self.assertEqual(oauth.registration("google"), CLIENTS["google"])
        with self.assertRaisesRegex(AEError, "public client"):
            oauth.validate_client("microsoft", {**CLIENTS["microsoft"], "client_secret": "private"})


if __name__ == "__main__":
    unittest.main()
