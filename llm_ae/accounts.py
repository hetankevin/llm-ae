"""Use official CLI account sessions without reading or copying their credentials."""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from .config import AEError

ALIASES = {
    "openai": "codex",
    "oai": "codex",
    "codex": "codex",
    "anthropic": "claude",
    "ant": "claude",
    "claude": "claude",
}


def provider_name(name):
    try:
        return ALIASES[name]
    except KeyError as exc:
        raise AEError("Choose openai or anthropic") from exc


def environment():
    # CLI credentials stay with their official owner. Do not pass API keys, mail
    # passwords, alternate model gateways, or parent agent configuration to a child.
    keep = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "TERM",
        "COLORTERM",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
        "no_proxy",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
    }
    return {key: value for key, value in os.environ.items() if key in keep or key.startswith("LC_")}


def executable(provider):
    path = shutil.which(provider)
    if not path:
        raise AEError(f"Install the official {provider} CLI and put it on PATH, then retry")
    return path


def capture(args, cwd, prompt="", timeout=30):
    """Private temporary output, bounded reads, and cleanup of timed-out child processes."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=environment(),
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=errors,
            start_new_session=True,
        )
        try:
            process.communicate(prompt.encode(), timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            if sys.exc_info()[0] is KeyboardInterrupt:
                raise
            raise AEError(
                "Account CLI timed out; no assessment was recorded. Retry or increase llm.cli_timeout_seconds."
            ) from None
        output.seek(0)
        errors.seek(0)
        stdout, stderr = output.read(2_000_001), errors.read(2_000_001)
        if len(stdout) > 2_000_000 or len(stderr) > 2_000_000:
            raise AEError("Account CLI output exceeded the size limit")
        return (
            process.returncode,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )


def auth_command(provider, action):
    binary = executable(provider)
    if provider == "codex":
        return [binary, "login"] + (["status"] if action == "status" else [])
    return [binary, "--safe-mode", "--setting-sources", "", "auth", action] + (
        ["--json"] if action == "status" else ["--claudeai"]
    )


def status(name):
    provider = provider_name(name)
    if not shutil.which(provider):
        return {
            "provider": provider,
            "installed": False,
            "account_ready": False,
            "action": f"Install {provider}, then run llm-ae auth login {name}",
        }
    with tempfile.TemporaryDirectory(prefix="llm-ae-auth-") as directory:
        code, output, error = capture(auth_command(provider, "status"), directory)
    if provider == "codex":
        ready = code == 0 and "logged in using chatgpt" in (output + error).casefold()
    else:
        try:
            data = json.loads(output)
            ready = (
                code == 0
                and data.get("loggedIn") is True
                and data.get("authMethod") in {"claude.ai", "oauth", "oauth_token"}
                and data.get("apiProvider", "firstParty") == "firstParty"
            )
        except (ValueError, AttributeError):
            ready = False
    # Never relay raw auth status: it can contain account details or key hints.
    return {
        "provider": provider,
        "installed": True,
        "account_ready": ready,
        "action": "Ready"
        if ready
        else f"Run llm-ae auth login {name} to sign in with your account",
    }


def require_account(provider):
    info = status(provider)
    if not info["account_ready"]:
        raise AEError(info["action"])


def save_selection(config, provider, model="", allow_remote=False):
    path = config.home / "config.toml"
    before = path.read_text()
    updates = {"provider": provider, "model": model}
    if allow_remote:
        updates["allow_remote_manuscripts"] = True
    # Preserve comments and unrelated settings. Refuse unusual TOML layouts rather
    # than rewriting a journal's entire configuration or guessing at its structure.
    section = re.search(r"(?m)^\[llm\]\s*(?:#[^\n]*)?$", before)
    if section:
        start = section.end()
        following = re.search(r"(?m)^\[", before[start:])
        end = start + following.start() if following else len(before)
        content = before[start:end]
        for key, value in updates.items():
            line = f"{key} = {json.dumps(value)}"
            pattern = rf"(?m)^\s*{key}\s*=.*$"
            if re.search(pattern, content):
                content = re.sub(
                    pattern, lambda _, replacement=line: "\n" + replacement, content, count=1
                )
            else:
                content += "\n" + line + "\n"
        after = before[:start] + content + "\n" + before[end:]
    else:
        after = (
            before
            + "\n[llm]\n"
            + "\n".join(f"{k} = {json.dumps(v)}" for k, v in updates.items())
            + "\n"
        )
    try:
        expected = tomllib.loads(before)
        expected.setdefault("llm", {}).update(updates)
        if tomllib.loads(after) != expected:
            raise ValueError("Configuration changed unexpectedly")
    except ValueError as exc:
        raise AEError(
            "Cannot update this TOML layout automatically; set llm.provider and llm.model manually"
        ) from exc
    with tempfile.NamedTemporaryFile(mode="w", dir=config.home, delete=False) as temporary:
        temp_path = Path(temporary.name)
        try:
            temporary.write(after)
            temporary.flush()
            temp_path.chmod(0o600)
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
    return {
        "provider": provider,
        "model": model or "CLI default",
        "account_ready": True,
        "remote_manuscripts_allowed": allow_remote or config["llm"]["allow_remote_manuscripts"],
        "next": "Use submissions assess or run --analyze. Remote processing must be enabled under your journal policy.",
    }


def select(config, name, login=False, device_auth=False, model="", allow_remote=False):
    provider = provider_name(name)
    if device_auth and (provider != "codex" or not login):
        raise AEError("--device-auth is available for OpenAI login only")
    if login:
        args = auth_command(provider, "login")
        if device_auth:
            args.append("--device-auth")
        with tempfile.TemporaryDirectory(prefix="llm-ae-auth-") as directory:
            # The official login owns the browser/terminal exchange; credentials
            # never pass through LLM-AE. Keep its UI separate from JSON stdout.
            completed = subprocess.run(args, cwd=directory, env=environment(), stdout=sys.stderr)
        if completed.returncode:
            raise AEError("Account login did not complete; provider selection was not changed")
    require_account(provider)
    return save_selection(config, provider, model, allow_remote)


def assess(config, instructions, source, output_schema):
    provider = config["llm"]["provider"]
    require_account(provider)
    with tempfile.TemporaryDirectory(prefix="llm-ae-assess-") as directory:
        root = Path(directory)
        binary = executable(provider)
        if provider == "codex":
            schema_path, result_path = root / "schema.json", root / "result.json"
            schema_path.write_text(json.dumps(output_schema))
            args = [
                binary,
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--color",
                "never",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(result_path),
            ]
            for option in (
                'forced_login_method="chatgpt"',
                'approval_policy="never"',
                'web_search="disabled"',
                "features.shell_tool=false",
                "features.apps=false",
                "features.hooks=false",
                "features.multi_agent=false",
                "features.skill_search=false",
                "mcp_servers={}",
                'history.persistence="none"',
            ):
                args.extend(["-c", option])
            args.extend(["-c", "developer_instructions=" + json.dumps(instructions)])
            prompt = source
        else:
            args = [
                binary,
                "--print",
                "--safe-mode",
                "--setting-sources",
                "",
                "--tools",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--disallowedTools",
                "mcp__*",
                "--no-session-persistence",
                "--no-chrome",
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(output_schema),
                "--system-prompt",
                instructions,
            ]
            prompt = source
        if config["llm"]["model"]:
            args.extend(["--model", config["llm"]["model"]])
        if provider == "codex":
            args.append("-")
        code, output, _ = capture(args, directory, prompt, config["llm"]["cli_timeout_seconds"])
        if code:
            raise AEError(
                f"{provider} assessment failed (exit {code}). Check llm-ae auth status, account limits, and CLI updates; no assessment recorded."
            )
        try:
            if provider == "codex":
                with result_path.open("rb") as stream:
                    raw = stream.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise AEError("Account assessment exceeded the size limit")
                return json.loads(raw)
            result = json.loads(output)
            if result.get("is_error") or result.get("subtype") != "success":
                raise AEError("Claude did not complete the assessment; no assessment recorded")
            return result["structured_output"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise AEError(
                "Account CLI returned no valid structured assessment; update the CLI and retry"
            ) from exc
