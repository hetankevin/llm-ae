"""Guided, resumable account and mailbox setup."""

import copy
import getpass
import imaplib
import os
import shlex
import shutil
import smtplib
import subprocess
import sys
import tempfile
import uuid
import warnings
import webbrowser
from pathlib import Path

from . import accounts, credentials, mail, oauth
from .config import AEError, Config, address, initialize, update_config
from .store import Store

PRESETS = {
    "icloud": (
        "imap.mail.me.com",
        "smtp.mail.me.com",
        587,
        "starttls",
        "https://account.apple.com/account/manage",
    ),
    "yahoo": (
        "imap.mail.yahoo.com",
        "smtp.mail.yahoo.com",
        465,
        "ssl",
        "https://login.yahoo.com/account/security",
    ),
}
INSTALLERS = {
    "codex": ("https://chatgpt.com/codex/install.sh", "sh"),
    "claude": ("https://claude.ai/install.sh", "bash"),
}


def ask(label, default="", validate=None):
    while True:
        value = input(f"{label}" + (f" [{default}]" if default else "") + ": ").strip() or default
        try:
            if not value:
                raise AEError("Please enter a value.")
            return validate(value) if validate else value
        except (AEError, ValueError) as exc:
            print(str(exc) if isinstance(exc, AEError) else "Please enter a valid number.")


def choose(label, choices, default):
    def valid(value):
        if value.lower() not in choices:
            raise AEError("Choose " + ", ".join(choices) + ".")
        return value.lower()

    return ask(label, default, valid)


def yes(label, default=False):
    return choose(label + " (yes/no)", ("yes", "no", "y", "n"), "yes" if default else "no") in {
        "yes",
        "y",
    }


def port(value):
    number = int(value)
    if not 1 <= number <= 65535:
        raise AEError("Port must be between 1 and 65535.")
    return number


def install_account_cli(provider):
    """Use the provider's official native installer; no Node or sudo required."""
    url, shell = INSTALLERS[provider]
    print(f"Installing {provider} with its official installer: {url}")
    with tempfile.TemporaryDirectory(prefix="llm-ae-install-") as directory:
        script = Path(directory) / "install.sh"
        fetched = subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--max-time",
                "120",
                url,
                "--output",
                str(script),
            ],
            env=accounts.environment(),
        )
        if fetched.returncode:
            raise AEError("Installer download failed. Rerun llm-ae setup to retry.")
        result = subprocess.run([shell, str(script)], cwd=directory, env=accounts.environment())
        if result.returncode:
            raise AEError(f"{provider} installation failed. Rerun llm-ae setup to retry.")
    # Native installers use ~/.local/bin; make the new CLI available immediately.
    os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + str(Path.home() / ".local/bin")
    if not shutil.which(provider):
        raise AEError(
            f"{provider} was not found after installation. Restart your terminal and rerun setup."
        )


def account(config, device_auth=False):
    print("\n1/3 · Sign in to your AI account")
    current = config["llm"]["provider"]
    provider = choose(
        "Account: openai / anthropic / offline",
        ("openai", "anthropic", "offline"),
        {"codex": "openai", "claude": "anthropic"}.get(current, "openai"),
    )
    if provider == "offline":
        return update_config(
            config, {"llm": {"provider": "offline", "allow_remote_manuscripts": False}}
        )
    selected = accounts.provider_name(provider)
    if not shutil.which(selected):
        if not yes(f"Install the official {selected} CLI now?", True):
            raise AEError("Setup paused. Rerun llm-ae setup when ready to install the account CLI.")
        install_account_cli(selected)
    if accounts.status(provider)["account_ready"]:
        print("Using your existing account login.")
        accounts.select(config, provider)
    else:
        print("Continue in the official sign-in window. An eligible account plan is required.")
        accounts.select(
            config, provider, login=True, device_auth=device_auth and selected == "codex"
        )
    config = Config(config.home)
    allowed = yes(
        "Does your journal permit sending manuscripts and reports to this AI provider?",
        config["llm"]["allow_remote_manuscripts"],
    )
    return update_config(config, {"llm": {"allow_remote_manuscripts": allowed}})


def check_mail(config):
    """Authenticate both directions and select the folder read-only; never fetch/send."""
    try:
        client = mail.connect_imap(config)
        try:
            status, _ = client.select(config["mail"]["imap_folder"], readonly=True)
            if status != "OK":
                raise AEError("The mail folder could not be opened. Check its name and try again.")
        finally:
            client.logout()
    except (OSError, imaplib.IMAP4.error) as exc:
        raise AEError(
            "Incoming mail failed. Check that you signed into the requested mailbox and that IMAP access is enabled."
        ) from exc
    try:
        client = mail.connect_smtp(config)
        client.close()
    except (OSError, smtplib.SMTPException) as exc:
        raise AEError(
            "Outgoing mail failed. Check SMTP settings and whether your provider allows SMTP authentication."
        ) from exc


def browser_mailbox(config, provider, email):
    record = oauth.sign_in(provider, email)
    settings = oauth.mail_settings(provider, email)
    reference = oauth.persist(config, record)
    settings["oauth_keyring"] = reference
    trial = copy.deepcopy(config)
    trial["mail"].update(settings)
    try:
        print("Checking incoming and outgoing mail…")
        check_mail(trial)
        result = update_config(config, {"ae": {"email": email}, "mail": settings})
    except BaseException:
        credentials.delete(config, reference)
        raise
    for old in (config["mail"]["oauth_keyring"], config["mail"]["password_keyring"]):
        credentials.delete(config, old)
    print(
        "Email connected. You will stay signed in using your system keychain. No message was sent."
    )
    return result


def mailbox(config):
    print("\n2/3 · Connect your email")
    if config["mail"]["imap_host"] and yes("Keep and test the current email connection?", True):
        try:
            check_mail(config)
        except AEError as exc:
            print(str(exc))
            if not yes("Reconnect this mailbox now?", True):
                raise AEError("Email setup paused. Rerun llm-ae setup to resume.") from exc
        else:
            print("Incoming and outgoing mail connected. No message was sent.")
            return config
    email = ask(
        "Your editorial email",
        "" if config["ae"]["email"] == "ae@example.org" else config["ae"]["email"],
        address,
    )
    domain = email.split("@")[1]
    suggested = (
        "gmail"
        if domain in {"gmail.com", "googlemail.com"}
        else (
            "icloud"
            if domain in {"icloud.com", "me.com", "mac.com"}
            else ("yahoo" if domain == "yahoo.com" else "custom")
        )
    )
    if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
        suggested = "outlook"
    kind = choose(
        "Mail provider: gmail / outlook / icloud / yahoo / custom",
        ("gmail", "outlook", *PRESETS, "custom", "microsoft"),
        suggested,
    )
    if kind in {"gmail", "outlook", "microsoft"}:
        return browser_mailbox(config, "google" if kind == "gmail" else "microsoft", email)
    credentials.backend()  # Check storage before asking for a secret.
    settings = {
        "username": email,
        "imap_port": 993,
        "imap_folder": "INBOX",
        "password_env": "",
        "oauth_token_env": "",
        "oauth_provider": "",
        "oauth_keyring": "",
        "transport": "smtp",
    }
    if kind in PRESETS:
        host, smtp, smtp_port, security, url = PRESETS[kind]
        settings.update(imap_host=host, smtp_host=smtp, smtp_port=smtp_port, smtp_security=security)
        print("Create an app password named LLM-AE; use it here instead of your usual password.")
        print(url)
        if yes("Open the app-password page in your browser?", True):
            webbrowser.open(url)
        if kind == "icloud":
            settings["username"] = ask("Primary iCloud email (for authentication)", email, address)
    else:
        settings.update(
            username=ask("Mail username", email),
            imap_host=ask("Incoming IMAP host"),
            imap_port=ask("IMAP TLS port", "993", port),
            imap_folder=ask("Mail folder", "INBOX"),
            smtp_host=ask("Outgoing SMTP host"),
            smtp_security=choose("SMTP security: starttls / ssl", ("starttls", "ssl"), "starttls"),
        )
        settings["smtp_port"] = ask(
            "SMTP port", "587" if settings["smtp_security"] == "starttls" else "465", port
        )
    while True:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                password = getpass.getpass("App password (hidden): ")
            except getpass.GetPassWarning as exc:
                raise AEError(
                    "A private terminal is required to enter the password. Run llm-ae setup in a terminal."
                ) from exc
        if not password:
            print("Please enter the app password.")
            continue
        trial = copy.deepcopy(config)
        trial["mail"].update(settings)
        # Use an in-memory secret during the check. Never put it in environment or disk.
        trial.secret = lambda section, key, secret=password: secret
        print("Checking incoming and outgoing mail…")
        try:
            check_mail(trial)
        except AEError as exc:
            print(str(exc))
            if yes("Try another app password?", True):
                continue
            raise AEError(
                "Email setup paused. Your previous connection is unchanged; rerun llm-ae setup."
            ) from exc
        reference = "mail-" + uuid.uuid4().hex
        credentials.save(config, reference, password)
        settings["password_keyring"] = reference
        config = update_config(config, {"ae": {"email": email}, "mail": settings})
        print("Email connected. Password saved in your system keychain. No message was sent.")
        return config


def run(home, device_auth=False):
    if not sys.stdin.isatty():
        raise AEError(
            "Setup is interactive. Run llm-ae setup in a terminal (without piping answers)."
        )
    print("Welcome to LLM-AE. We will connect your AI account and email, then set up your journal.")
    print("You can press Ctrl-C and rerun setup to resume. Editorial mail requires your approval.")
    config = Config(home) if (home / "config.toml").exists() else initialize(home)
    store = Store(config.home)
    store.close()
    config = account(config, device_auth)
    config = mailbox(config)
    print("\n3/3 · Your editorial desk")
    ae = config["ae"]
    name = ask("Your name", "" if ae["name"] == "Associate Editor" else ae["name"])
    journal = ask("Journal", ae["journal"])
    editors = ask(
        "Editor email(s), separated by commas",
        "" if ae["editors"] == ["editor@example.org"] else ", ".join(ae["editors"]),
        lambda value: [address(x) for x in value.split(",")],
    )
    config = update_config(config, {"ae": {"name": name, "journal": journal, "editors": editors}})
    command = "llm-ae"
    if config.home != Path("~/.llm-ae").expanduser().resolve():
        command += " --home " + shlex.quote(str(config.home))
    print("\nSetup complete. Your account and email are connected.")
    if config["llm"]["provider"] == "offline" or not config["llm"]["allow_remote_manuscripts"]:
        print(
            "AI assessment is disabled. You can still import submissions and manage the review process."
        )
    print(f"Next: {command} inbox sync")
    print(f"Then: {command} tasks")
    print("Emails stay in the outbox until you approve and send them.")
    return {
        "home": str(config.home),
        "provider": config["llm"]["provider"],
        "email_connected": True,
    }
