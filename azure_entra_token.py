from __future__ import annotations

import json
import os
import shutil
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
AZURE_CLI_PYTHONW_ENV_VAR = "AZURE_CLI_PYTHONW"


def acquire_azure_cli_token(
    scope: str,
    *,
    cli_pythonw: str | None = None,
    run=subprocess.run,
) -> str:
    if not scope.endswith("/.default"):
        raise ValueError("Azure CLI mode requires a scope ending in '/.default'")
    resource = scope[: -len("/.default")]

    if cli_pythonw is None:
        cli_pythonw = os.environ.get(AZURE_CLI_PYTHONW_ENV_VAR)
    if cli_pythonw is None:
        az_cmd = shutil.which("az.cmd") or shutil.which("az")
        if az_cmd:
            cli_pythonw = str(Path(az_cmd).resolve().parent.parent / "pythonw.exe")
    if not cli_pythonw or not Path(cli_pythonw).is_file():
        raise ClientAuthenticationError(
            "Azure CLI pythonw.exe was not found; set "
            f"{AZURE_CLI_PYTHONW_ENV_VAR}"
        )

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    result = run(
        [
            cli_pythonw,
            "-IBm",
            "azure.cli",
            "account",
            "get-access-token",
            "--output",
            "json",
            "--resource",
            resource,
        ],
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT_SECONDS,
        cwd=os.environ.get("SYSTEMROOT"),
        env={
            **os.environ,
            "AZ_INSTALLER": "MSI",
            "AZURE_CORE_NO_COLOR": "true",
        },
        creationflags=subprocess.CREATE_NO_WINDOW,
        startupinfo=startupinfo,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or "Azure CLI token request failed"
        raise ClientAuthenticationError(message)

    try:
        token = json.loads(result.stdout)["accessToken"].strip()
    except (json.JSONDecodeError, KeyError, AttributeError) as exc:
        raise ClientAuthenticationError(
            "Azure CLI returned an invalid token response"
        ) from exc
    return token


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
        return {}
    if mode == "default":
        return {"process_timeout": PROCESS_TIMEOUT_SECONDS}
    raise ValueError(
        f"{CREDENTIAL_MODE_ENV_VAR} must be 'azure-cli' or 'default'"
    )


def acquire_token() -> str:
    scope = os.environ.get(SCOPE_ENV_VAR, DEFAULT_SCOPE).strip()
    if not scope:
        raise ValueError(f"{SCOPE_ENV_VAR} must not be empty")
    mode = os.environ.get(CREDENTIAL_MODE_ENV_VAR, "azure-cli").strip().lower()
    options = credential_options()

    with credential_lock():
        last_error = None
        for attempt in range(TOKEN_ATTEMPTS):
            try:
                if mode == "azure-cli":
                    token = acquire_azure_cli_token(scope).strip()
                else:
                    credential = DefaultAzureCredential(**options)
                    try:
                        token_provider = get_bearer_token_provider(
                            credential,
                            scope,
                        )
                        token = token_provider().strip()
                    finally:
                        credential.close()
                break
            except ClientAuthenticationError as exc:
                last_error = exc
                if attempt + 1 < TOKEN_ATTEMPTS:
                    time.sleep(1)
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
