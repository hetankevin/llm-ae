"""Explicit configuration. Secrets are environment references, never TOML values."""

import os
import re
import tomllib
from pathlib import Path


class AEError(Exception):
    """A recoverable, user-facing workflow error."""


def address(value: str) -> str:
    value = value.strip().lower()
    if not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", value):
        raise AEError(f"Expected a single email address, got {value!r}")
    return value


DEFAULT_CONFIG = """# Private runtime configuration. Do not commit this file.
[ae]
name = "Associate Editor"
email = "ae@example.org"
journal = "Statistics journal"
scope = "Statistical methodology, theory, and applications."
editors = ["editor@example.org"]

[mail]
imap_host = ""
imap_port = 993
imap_folder = "INBOX"
username = ""
password_env = "LLM_AE_MAIL_PASSWORD"
# Set oauth_token_env instead of password_env to use an OAuth2 access token.
oauth_token_env = ""
smtp_host = ""
smtp_port = 465
smtp_security = "ssl" # ssl or starttls; plaintext is not supported
transport = "file" # file writes .eml previews; smtp sends as the AE
submission_subject = "(?i)(submission|manuscript|assignment)"
lookback_days = 30
max_messages = 100
max_message_bytes = 25000000

[llm]
provider = "offline" # offline produces a checklist, never a scientific verdict
base_url = "https://api.openai.com/v1"
model = "" # choose a Responses-compatible model available to your account
api_key_env = "OPENAI_API_KEY"
allow_remote_manuscripts = false # enable only under your journal's policy
max_input_chars = 180000

[workflow]
invitation_days = 7
review_days = 28
reminder_days = 7
max_reminders = 2
target_reviews = 2
auto_approve_reminders = false

[discovery]
openalex_key_env = "OPENALEX_API_KEY"
"""


class Config:
    def __init__(self, home: Path):
        self.home = home.expanduser().resolve()
        path = self.home / "config.toml"
        if not path.exists():
            raise AEError(f"Not initialized: {self.home}. Run llm-ae init first.")
        try:
            self.data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise AEError(f"Invalid config.toml: {exc}") from exc
        defaults = tomllib.loads(DEFAULT_CONFIG)
        for section, values in defaults.items():
            if not isinstance(self.data.get(section, {}), dict):
                raise AEError(f"Config section {section} must be a table")
            for key, default in values.items():
                value = self.data.get(section, {}).get(key, default)
                if type(value) is not type(default):
                    raise AEError(f"Invalid type for {section}.{key}")
                if isinstance(default, list) and any(not isinstance(item, str) for item in value):
                    raise AEError(f"{section}.{key} must contain strings")
            values.update(self.data.get(section, {}))
            self.data[section] = values
        self["ae"]["email"] = address(self["ae"]["email"])
        self["ae"]["editors"] = [address(x) for x in self["ae"]["editors"]]
        for key in ("imap_port", "smtp_port"):
            if not 1 <= self["mail"][key] <= 65535:
                raise AEError(f"mail.{key} must be a valid port")
        for key in ("invitation_days", "review_days", "reminder_days", "target_reviews"):
            if type(self["workflow"][key]) is not int or self["workflow"][key] < 1:
                raise AEError(f"workflow.{key} must be a positive integer")
        if (
            type(self["workflow"]["max_reminders"]) is not int
            or self["workflow"]["max_reminders"] < 0
        ):
            raise AEError("workflow.max_reminders must be a nonnegative integer")
        for key in ("lookback_days", "max_messages", "max_message_bytes"):
            if type(self["mail"][key]) is not int or self["mail"][key] < 1:
                raise AEError(f"mail.{key} must be a positive integer")
        if self["mail"]["transport"] not in {"file", "smtp"}:
            raise AEError("mail.transport must be file or smtp")
        if self["mail"]["smtp_security"] not in {"ssl", "starttls"}:
            raise AEError("mail.smtp_security must be ssl or starttls")
        for section, key in (
            ("llm", "allow_remote_manuscripts"),
            ("workflow", "auto_approve_reminders"),
        ):
            if type(self[section][key]) is not bool:
                raise AEError(f"{section}.{key} must be a TOML boolean")
        if type(self["llm"]["max_input_chars"]) is not int or self["llm"]["max_input_chars"] < 1:
            raise AEError("llm.max_input_chars must be a positive integer")
        if self["llm"]["provider"] not in {"offline", "openai"}:
            raise AEError("llm.provider must be offline or openai")
        try:
            re.compile(self["mail"]["submission_subject"])
        except re.error as exc:
            raise AEError("mail.submission_subject must be a valid regular expression") from exc

    def __getitem__(self, section):
        return self.data[section]

    def secret(self, section, key):
        name = self[section][key]
        value = os.environ.get(name, "")
        if not value:
            raise AEError(f"Set environment variable {name or '(not configured)'}")
        return value


def initialize(home: Path):
    home = home.expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = home / "config.toml"
    if path.exists():
        raise AEError(f"Already initialized: {home}")
    with path.open("x") as stream:
        stream.write(DEFAULT_CONFIG)
    path.chmod(0o600)
    for folder in ("documents", "outbox"):
        (home / folder).mkdir(mode=0o700, exist_ok=True)
    return Config(home)
