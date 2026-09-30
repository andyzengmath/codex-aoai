from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from azure.core.exceptions import ClientAuthenticationError
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

DEFAULT_SCOPE = "https://cognitiveservices.azure.com/.default"
SCOPE_ENV_VAR = "AZURE_OPENAI_TOKEN_SCOPE"
CREDENTIAL_MODE_ENV_VAR = "AZURE_OPENAI_CREDENTIAL_MODE"
LOCK_TIMEOUT_SECONDS = 90
PROCESS_TIMEOUT_SECONDS = 30
TOKEN_ATTEMPTS = 2
AZURE_CLI_CREDENTIAL_OPTIONS = {
    "exclude_environment_credential": True,
    "exclude_workload_identity_credential": True,
    "exclude_managed_identity_credential": True,
    "exclude_shared_token_cache_credential": True,
    "exclude_visual_studio_code_credential": True,
    "exclude_powershell_credential": True,
    "exclude_developer_cli_credential": True,
    "exclude_broker_credential": True,
}


def hide_windows_subprocess(check_output):
    if getattr(check_output, "_codex_hidden_window", False):
        return check_output

    def hidden_check_output(*args, **kwargs):
        startupinfo = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = (
            kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        )
        return check_output(*args, **kwargs)

    hidden_check_output._codex_hidden_window = True
    return hidden_check_output


@contextmanager
def credential_lock():
    import msvcrt

    lock_path = Path(tempfile.gettempdir()) / "codex-azure-openai-entra.lock"
    lock_file = lock_path.open("a+b")
    if lock_file.seek(0, os.SEEK_END) == 0:
        lock_file.write(b"\0")
        lock_file.flush()

    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError:
            if time.monotonic() >= deadline:
                lock_file.close()
                raise RuntimeError("Timed out waiting for another Entra token request")
            time.sleep(0.1)

    try:
        yield
    finally:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        lock_file.close()


def credential_options() -> dict[str, bool | int]:
    mode = os.environ.get(CREDENTIAL_MODE_ENV_VAR, "azure-cli").strip().lower()
    if mode == "azure-cli":
        return {
            "process_timeout": PROCESS_TIMEOUT_SECONDS,
            **AZURE_CLI_CREDENTIAL_OPTIONS,
        }
    if mode == "default":
        return {"process_timeout": PROCESS_TIMEOUT_SECONDS}
    raise ValueError(
        f"{CREDENTIAL_MODE_ENV_VAR} must be 'azure-cli' or 'default'"
    )


def acquire_token() -> str:
    scope = os.environ.get(SCOPE_ENV_VAR, DEFAULT_SCOPE).strip()
    if not scope:
        raise ValueError(f"{SCOPE_ENV_VAR} must not be empty")
    if sys.platform == "win32":
        subprocess.check_output = hide_windows_subprocess(
            subprocess.check_output
        )
    options = credential_options()

    with credential_lock():
        last_error = None
        for attempt in range(TOKEN_ATTEMPTS):
            credential = DefaultAzureCredential(**options)
            try:
                token_provider = get_bearer_token_provider(credential, scope)
                token = token_provider().strip()
                break
            except ClientAuthenticationError as exc:
                last_error = exc
                if attempt + 1 < TOKEN_ATTEMPTS:
                    time.sleep(1)
            finally:
                credential.close()
        else:
            assert last_error is not None
            raise last_error

    if not token:
        raise RuntimeError("Microsoft Entra returned an empty access token")
    return token


def main() -> int:
    try:
        token = acquire_token()
    except (ClientAuthenticationError, RuntimeError, ValueError) as exc:
        print(f"Unable to acquire an Azure OpenAI Entra token: {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
