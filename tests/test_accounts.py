import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_ae import accounts
from llm_ae.config import AEError, Config, initialize
from llm_ae.demo import MANUSCRIPT
from llm_ae.store import Store
from llm_ae.workflow import Workflow


class AccountTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = initialize(Path(self.tmp.name))
        self.store = Store(self.cfg.home)
        self.w = Workflow(self.cfg, self.store)
        self.sub = self.w.add_submission(
            "Synthetic paper",
            ["Example Author"],
            "editor@example.org",
            MANUSCRIPT.encode(),
            "paper.txt",
        )
        self.cfg["llm"].update(provider="codex", allow_remote_manuscripts=True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def assessment(self):
        return {
            "summary": "Synthetic mean-estimation fixture",
            "recommendation": "review",
            "strengths": [],
            "concerns": [],
            "questions": ["Check the derivation"],
            "limitations": ["Not a full expert review"],
            "reviewer_topics": ["statistics"],
            "editor_note": "Recommend specialist review.",
        }

    def test_codex_account_status_does_not_expose_raw_auth_output(self):
        with (
            patch("shutil.which", return_value="/bin/codex"),
            patch(
                "llm_ae.accounts.capture",
                return_value=(0, "", "Logged in using ChatGPT\nprivate account details"),
            ),
        ):
            result = accounts.status("openai")
        self.assertTrue(result["account_ready"])
        self.assertNotIn("private", json.dumps(result))

    def test_api_login_is_not_accepted_as_subscription(self):
        with (
            patch("shutil.which", return_value="/bin/codex"),
            patch("llm_ae.accounts.capture", return_value=(0, "", "Logged in using an API key")),
        ):
            self.assertFalse(accounts.status("oai")["account_ready"])

    def test_claude_subscription_status(self):
        with (
            patch("shutil.which", return_value="/bin/claude"),
            patch(
                "llm_ae.accounts.capture",
                return_value=(
                    0,
                    json.dumps(
                        {
                            "loggedIn": True,
                            "authMethod": "claude.ai",
                            "email": "private@example.org",
                        }
                    ),
                    "",
                ),
            ),
        ):
            result = accounts.status("ant")
        self.assertTrue(result["account_ready"])
        self.assertNotIn("private@example.org", json.dumps(result))

    def test_claude_api_status_is_rejected(self):
        with (
            patch("shutil.which", return_value="/bin/claude"),
            patch(
                "llm_ae.accounts.capture",
                return_value=(0, json.dumps({"loggedIn": True, "authMethod": "api_key"}), ""),
            ),
        ):
            self.assertFalse(accounts.status("anthropic")["account_ready"])

    def test_missing_cli_gives_install_instructions(self):
        with patch("shutil.which", return_value=None):
            self.assertFalse(accounts.status("openai")["installed"])
            with self.assertRaisesRegex(AEError, "Install"):
                accounts.executable("claude")

    def test_environment_preserves_login_home_and_excludes_api_credentials(self):
        values = {
            "PATH": "/bin",
            "HOME": "/user",
            "OPENAI_API_KEY": "secret",
            "ANTHROPIC_API_KEY": "secret",
            "ANTHROPIC_AUTH_TOKEN": "secret",
            "LLM_AE_MAIL_PASSWORD": "secret",
            "ANTHROPIC_BASE_URL": "https://untrusted.example",
            "CODEX_HOME": "/account",
            "CLAUDE_CONFIG_DIR": "/claude-account",
        }
        with patch.dict(os.environ, values, clear=True):
            child = accounts.environment()
        self.assertEqual(child["HOME"], "/user")
        self.assertEqual(child["CODEX_HOME"], "/account")
        self.assertNotIn("secret", child.values())
        self.assertNotIn("ANTHROPIC_BASE_URL", child)

    def test_use_updates_config_without_login_or_enabling_remote_processing(self):
        with patch("llm_ae.accounts.require_account"), patch("subprocess.run") as login:
            accounts.select(self.cfg, "ant")
        login.assert_not_called()
        config = Config(self.cfg.home)
        self.assertEqual(config["llm"]["provider"], "claude")
        self.assertEqual(config["llm"]["model"], "")
        self.assertFalse(config["llm"]["allow_remote_manuscripts"])
        self.assertIn(
            "# Private runtime configuration", (self.cfg.home / "config.toml").read_text()
        )

    def test_login_delegates_to_official_cli(self):
        with (
            patch("shutil.which", return_value="/bin/codex"),
            patch("llm_ae.accounts.require_account"),
            patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as login,
        ):
            accounts.select(self.cfg, "openai", login=True, device_auth=True, allow_remote=True)
        self.assertEqual(login.call_args.args[0], ["/bin/codex", "login", "--device-auth"])
        self.assertTrue(Config(self.cfg.home)["llm"]["allow_remote_manuscripts"])

    def test_failed_login_does_not_change_configuration(self):
        path = self.cfg.home / "config.toml"
        original = path.read_text()
        with (
            patch("shutil.which", return_value="/bin/claude"),
            patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1)),
        ):
            with self.assertRaises(AEError):
                accounts.select(self.cfg, "anthropic", login=True)
        self.assertEqual(path.read_text(), original)

    def test_codex_uses_stdin_readonly_and_ephemeral_output(self):
        observed = {}

        def run(args, cwd, prompt, timeout):
            observed.update(args=args, cwd=cwd, prompt=prompt)
            result = Path(args[args.index("--output-last-message") + 1])
            result.write_text(json.dumps(self.assessment()))
            return 0, "", ""

        with (
            patch("llm_ae.accounts.require_account"),
            patch("shutil.which", return_value="/bin/codex"),
            patch("llm_ae.accounts.capture", side_effect=run),
        ):
            result = self.w.assess(self.sub["id"])
        self.assertEqual(result["recommendation"], "review")
        args = observed["args"]
        self.assertIn("--ignore-user-config", args)
        self.assertIn("--ephemeral", args)
        self.assertIn("read-only", args)
        self.assertIn('forced_login_method="chatgpt"', args)
        self.assertIn("features.shell_tool=false", args)
        self.assertIn("features.hooks=false", args)
        self.assertIn(MANUSCRIPT, json.loads(observed["prompt"])["manuscript"])
        self.assertNotIn(MANUSCRIPT, " ".join(args))
        self.assertFalse(Path(observed["cwd"]).exists())

    def test_claude_returns_structured_output_with_tools_disabled(self):
        self.cfg["llm"]["provider"] = "claude"
        output = json.dumps(
            {"subtype": "success", "is_error": False, "structured_output": self.assessment()}
        )
        with (
            patch("llm_ae.accounts.require_account"),
            patch("shutil.which", return_value="/bin/claude"),
            patch("llm_ae.accounts.capture", return_value=(0, output, "")) as run,
        ):
            result = self.w.assess(self.sub["id"])
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--safe-mode", args)
        self.assertIn("--no-session-persistence", args)
        self.assertNotIn("--bare", args)  # --bare disables subscription authentication.
        self.assertEqual(result["recommendation"], "review")

    def test_remote_gate_applies_to_accounts_even_with_local_base_url(self):
        self.cfg["llm"].update(allow_remote_manuscripts=False, base_url="http://localhost:1234")
        with patch("llm_ae.accounts.assess") as run:
            with self.assertRaisesRegex(AEError, "disabled"):
                self.w.assess(self.sub["id"])
            run.assert_not_called()

    def test_account_failure_never_records_assessment(self):
        with (
            patch("llm_ae.accounts.require_account"),
            patch("shutil.which", return_value="/bin/codex"),
            patch("llm_ae.accounts.capture", return_value=(1, "", "SECRET DEBUG")),
        ):
            with self.assertRaises(AEError) as error:
                self.w.assess(self.sub["id"])
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(self.store.all("SELECT * FROM assessments"), [])

    def test_account_outputs_use_existing_evidence_validation(self):
        result = self.assessment()
        result["concerns"] = [
            {"issue": "Unsupported", "quote": "invented quote", "location": "page 1"}
        ]
        with patch("llm_ae.accounts.assess", return_value=result):
            checked = self.w.assess(self.sub["id"])
        self.assertEqual(checked["recommendation"], "needs_human")

    def test_capture_executes_without_shell_and_passes_stdin(self):
        code, output, _ = accounts.capture(
            [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
            self.cfg.home,
            "literal $(not-a-command)",
        )
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "literal $(not-a-command)")

    def test_capture_timeout_fails_cleanly(self):
        with self.assertRaisesRegex(AEError, "timed out"):
            accounts.capture(
                [sys.executable, "-c", "import time; time.sleep(10)"], self.cfg.home, timeout=0.05
            )


if __name__ == "__main__":
    unittest.main()
