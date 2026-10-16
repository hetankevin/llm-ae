import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from llm_ae import credentials, mail, setup
from llm_ae.cli import main
from llm_ae.config import AEError, Config, initialize, update_config


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "runtime"
        self.config = initialize(self.home)
        self.output = io.StringIO()

    def tearDown(self):
        self.tmp.cleanup()

    def test_whole_setup_saves_working_connection_without_secret_in_files(self):
        answers = [
            "openai",
            "yes",
            "ae@gmail.com",
            "",
            "no",
            "Dr Editor",
            "Test Journal",
            "editor@journal.org, second@journal.org",
        ]
        secrets = {}
        keychain = Mock()
        keychain.set_password.side_effect = lambda service, ref, value: secrets.update({ref: value})
        keychain.get_password.side_effect = lambda service, ref: secrets.get(ref)

        def selected(config, name, **kwargs):
            update_config(config, {"llm": {"provider": "codex"}})

        with (
            contextlib.redirect_stdout(self.output),
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=answers),
            patch("llm_ae.setup.shutil.which", return_value="/bin/codex"),
            patch("llm_ae.accounts.status", return_value={"account_ready": True}),
            patch("llm_ae.accounts.select", side_effect=selected) as login,
            patch("llm_ae.credentials.backend", return_value=keychain),
            patch("llm_ae.setup.getpass.getpass", return_value="abcd efgh ijkl mnop"),
            patch("llm_ae.setup.check_mail") as check,
        ):
            self.assertEqual(main(["--home", str(self.home), "setup"]), 0)
            cfg = Config(self.home)
            self.assertEqual(cfg.secret("mail", "password_env"), "abcdefghijklmnop")
        login.assert_called_once_with(unittest.mock.ANY, "openai")
        self.assertEqual(check.call_count, 1)
        self.assertEqual(cfg["mail"]["imap_host"], "imap.gmail.com")
        self.assertEqual(cfg["mail"]["transport"], "smtp")
        self.assertEqual(cfg["ae"]["editors"], ["editor@journal.org", "second@journal.org"])
        self.assertTrue(cfg["llm"]["allow_remote_manuscripts"])
        for file in self.home.iterdir():
            if file.is_file():
                self.assertNotIn(b"abcdefghijklmnop", file.read_bytes())
        self.assertIn("Setup complete", self.output.getvalue())
        self.assertNotIn("abcdefghijklmnop", self.output.getvalue())

    def test_failed_mail_preserves_old_configuration_and_does_not_save_secret(self):
        before = (self.home / "config.toml").read_bytes()
        with (
            contextlib.redirect_stdout(self.output),
            patch("builtins.input", side_effect=["ae@gmail.com", "", "no", "no"]),
            patch("llm_ae.credentials.backend"),
            patch("llm_ae.setup.getpass.getpass", return_value="secret"),
            patch("llm_ae.setup.check_mail", side_effect=AEError("Incoming mail failed")),
            patch("llm_ae.credentials.save") as save,
        ):
            with self.assertRaisesRegex(AEError, "paused"):
                setup.mailbox(self.config)
        save.assert_not_called()
        self.assertEqual(before, (self.home / "config.toml").read_bytes())

    def test_password_retry_can_recover(self):
        with (
            contextlib.redirect_stdout(self.output),
            patch("builtins.input", side_effect=["ae@yahoo.com", "", "no", "yes"]),
            patch("llm_ae.credentials.backend"),
            patch("llm_ae.setup.getpass.getpass", side_effect=["wrong", "correct"]),
            patch("llm_ae.setup.check_mail", side_effect=[AEError("Try again"), None]),
            patch("llm_ae.credentials.save") as save,
        ):
            cfg = setup.mailbox(self.config)
        self.assertEqual(save.call_args.args[2], "correct")
        self.assertEqual(cfg["mail"]["smtp_host"], "smtp.mail.yahoo.com")

    def test_setup_resumes_existing_mail_without_overwriting_data(self):
        cfg = update_config(self.config, {"mail": {"imap_host": "imap.example.org"}})
        with (
            contextlib.redirect_stdout(self.output),
            patch("builtins.input", return_value="yes"),
            patch("llm_ae.setup.check_mail") as check,
            patch("llm_ae.credentials.save") as save,
        ):
            self.assertIs(setup.mailbox(cfg), cfg)
        check.assert_called_once_with(cfg)
        save.assert_not_called()

    def test_custom_server_setup(self):
        answers = [
            "ae@university.edu",
            "custom",
            "login-name",
            "imap.university.edu",
            "bad",
            "993",
            "Editorial",
            "smtp.university.edu",
            "starttls",
            "587",
        ]
        with (
            contextlib.redirect_stdout(self.output),
            patch("builtins.input", side_effect=answers),
            patch("llm_ae.credentials.backend"),
            patch("llm_ae.credentials.save"),
            patch("llm_ae.setup.check_mail"),
            patch("llm_ae.setup.getpass.getpass", return_value="secret"),
        ):
            cfg = setup.mailbox(self.config)
        self.assertEqual(cfg["mail"]["username"], "login-name")
        self.assertEqual(cfg["mail"]["imap_folder"], "Editorial")
        self.assertEqual(cfg["mail"]["smtp_security"], "starttls")

    def test_microsoft_does_not_ask_for_password_or_claim_connection(self):
        with (
            contextlib.redirect_stdout(self.output),
            patch("builtins.input", side_effect=["ae@outlook.com", ""]),
            patch("llm_ae.setup.getpass.getpass") as password,
        ):
            with self.assertRaisesRegex(AEError, "requires OAuth"):
                setup.mailbox(self.config)
        password.assert_not_called()

    def test_account_installs_and_logs_in_if_needed(self):
        with (
            contextlib.redirect_stdout(self.output),
            patch("builtins.input", side_effect=["anthropic", "yes", "no"]),
            patch("llm_ae.setup.shutil.which", return_value=None),
            patch("llm_ae.setup.install_account_cli") as install,
            patch("llm_ae.accounts.status", return_value={"account_ready": False}),
            patch("llm_ae.accounts.select") as login,
        ):
            setup.account(self.config, device_auth=True)
        install.assert_called_once_with("claude")
        login.assert_called_once_with(self.config, "anthropic", login=True, device_auth=False)

    def test_connection_check_is_readonly_and_sends_nothing(self):
        imap = Mock()
        imap.select.return_value = ("OK", [b"12"])
        smtp = Mock()
        cfg = self.config
        cfg["mail"].update(
            imap_host="imap.example.org", smtp_host="smtp.example.org", username="ae"
        )
        with (
            patch("llm_ae.mail.imaplib.IMAP4_SSL", return_value=imap),
            patch("llm_ae.mail.smtplib.SMTP_SSL", return_value=smtp),
            patch.dict(os.environ, LLM_AE_MAIL_PASSWORD="secret"),
        ):
            setup.check_mail(cfg)
        imap.select.assert_called_once_with("INBOX", readonly=True)
        imap.uid.assert_not_called()
        smtp.send_message.assert_not_called()
        smtp.sendmail.assert_not_called()
        imap.logout.assert_called_once()
        smtp.close.assert_called_once()

    def test_smtp_login_failure_closes_and_hides_server_diagnostics(self):
        cfg = self.config
        cfg["mail"].update(smtp_host="smtp.example.org", username="ae")
        client = Mock()
        client.login.side_effect = mail.smtplib.SMTPAuthenticationError(535, b"private detail")
        with (
            patch("llm_ae.mail.connect_imap") as imap,
            patch("llm_ae.mail.smtplib.SMTP_SSL", return_value=client),
            patch.dict(os.environ, LLM_AE_MAIL_PASSWORD="secret"),
        ):
            imap.return_value.select.return_value = ("OK", [])
            with self.assertRaisesRegex(AEError, "Outgoing mail failed") as error:
                setup.check_mail(cfg)
        self.assertNotIn("private detail", str(error.exception))
        client.close.assert_called_once()

    def test_config_update_preserves_extras_and_rejects_invalid_values(self):
        path = self.home / "config.toml"
        with path.open("a") as file:
            file.write('\n[custom]\nvalue = "retained"\n')
        cfg = update_config(self.config, {"ae": {"name": 'A "quoted" name'}})
        self.assertEqual(cfg["custom"]["value"], "retained")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        before = path.read_bytes()
        with self.assertRaises(AEError):
            update_config(cfg, {"mail": {"smtp_port": 999999}})
        self.assertEqual(path.read_bytes(), before)

    def test_secret_prefers_explicit_keychain_reference_over_stale_environment(self):
        self.config["mail"]["password_keyring"] = "ref"
        with (
            patch("llm_ae.credentials.get", return_value="keychain"),
            patch.dict(os.environ, LLM_AE_MAIL_PASSWORD="old"),
        ):
            self.assertEqual(self.config.secret("mail", "password_env"), "keychain")

    def test_interrupt_preserves_config_and_offers_resume(self):
        before = (self.home / "config.toml").read_bytes()
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("llm_ae.setup.run", side_effect=KeyboardInterrupt),
            contextlib.redirect_stderr(self.output),
        ):
            self.assertEqual(main(["--home", str(self.home), "setup"]), 130)
        self.assertIn("resume", self.output.getvalue())
        self.assertEqual(before, (self.home / "config.toml").read_bytes())

    def test_no_tty_or_json_fails_before_creating_home(self):
        with patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stderr(self.output):
            self.assertEqual(main(["--home", str(self.home / "new"), "setup"]), 1)
            self.assertFalse((self.home / "new").exists())
        with contextlib.redirect_stderr(self.output):
            self.assertEqual(main(["--home", str(self.home), "--json", "setup"]), 1)

    def test_keychain_failure_is_sanitized(self):
        keychain = Mock()
        keychain.set_password.side_effect = RuntimeError("secret")
        with patch("llm_ae.credentials.backend", return_value=keychain):
            with self.assertRaisesRegex(AEError, "Could not save") as error:
                credentials.save(self.config, "ref", "secret")
        self.assertNotIn("secret", str(error.exception))

    def test_installer_download_failure_never_executes_partial_script(self):
        with (
            contextlib.redirect_stdout(self.output),
            patch("llm_ae.setup.subprocess.run", return_value=Mock(returncode=22)) as run,
        ):
            with self.assertRaisesRegex(AEError, "download failed"):
                setup.install_account_cli("codex")
        self.assertEqual(run.call_count, 1)
        self.assertIn("https://chatgpt.com/codex/install.sh", run.call_args.args[0])

    def test_plaintext_keyring_is_rejected(self):
        backend = type(
            "PlaintextKeyring", (), {"__module__": "keyrings.alt.file", "priority": 10}
        )()
        with patch("keyring.get_keyring", return_value=backend):
            with self.assertRaisesRegex(AEError, "No system keychain"):
                credentials.backend()

    def test_installer_continues_to_setup_with_tools_in_a_path_with_spaces(self):
        import pty
        import select
        import time

        with tempfile.TemporaryDirectory(prefix="llm ae install ") as directory:
            root = Path(directory)
            uv = root / "uv"
            uv.write_text(
                "#!/bin/sh\n"
                'case "$*" in\n'
                '  "tool dir --bin") dirname "$0" ;;\n'
                '  "tool install "*) echo "installed extras" ;;\n'
                '  "tool update-shell") echo "updated path" ;;\n'
                "  *) exit 19 ;;\n"
                "esac\n"
            )
            launcher = root / "llm-ae"
            launcher.write_text(
                '#!/bin/sh\n[ -t 0 ] || exit 20\n[ "$1" = setup ] || exit 21\necho "interactive setup reached"\n'
            )
            uv.chmod(0o700)
            launcher.chmod(0o700)
            master, slave = pty.openpty()
            proc = subprocess.Popen(
                ["sh", "install.sh"],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]},
            )
            os.close(slave)
            output = b""
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            data = os.read(master, 65536)
                        except OSError:
                            break
                        if not data:
                            break
                        output += data
                    if proc.poll() is not None:
                        break
                self.assertEqual(proc.wait(timeout=2), 0, output.decode())
                self.assertIn(b"interactive setup reached", output)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                os.close(master)

    def test_install_script_shell_syntax(self):
        result = subprocess.run(["sh", "-n", "install.sh"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
