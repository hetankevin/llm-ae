"""Mailbox secrets in the operating system credential store, never config files."""

from .config import AEError


def backend():
    try:
        import keyring

        selected = keyring.get_keyring()
        # Do not accept plaintext third-party backends or an unavailable backend.
        if type(selected).__module__ == "keyring.backends.chainer":
            candidates = selected.backends
        else:
            candidates = [selected]
        for candidate in candidates:
            if (
                type(candidate).__module__
                in {
                    "keyring.backends.macOS",
                    "keyring.backends.SecretService",
                    "keyring.backends.kwallet",
                    "keyring.backends.Windows",
                }
                and candidate.priority > 0
            ):
                return candidate
    except Exception:
        pass
    raise AEError(
        "No system keychain available. Install llm-ae with the setup extra and "
        "unlock your desktop keychain (on Linux, Secret Service or KWallet), then rerun setup. "
        "Headless installations can use the documented environment-variable configuration."
    )


def service(config):
    return f"llm-ae:{config.home}"


def get(config, reference):
    try:
        value = backend().get_password(service(config), reference)
    except AEError:
        raise
    except Exception as exc:
        raise AEError(
            "Could not read the mailbox password. Unlock your keychain and retry."
        ) from exc
    if not value:
        raise AEError("Mailbox credential missing from keychain. Run llm-ae setup again.")
    return value


def save(config, reference, value):
    try:
        backend().set_password(service(config), reference, value)
    except AEError:
        raise
    except Exception as exc:
        raise AEError(
            "Could not save the mailbox password. Unlock your keychain and retry."
        ) from exc
