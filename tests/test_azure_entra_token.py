from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from azure.core.exceptions import ClientAuthenticationError

import azure_entra_token


class AcquireTokenTests(unittest.TestCase):
    def test_azure_cli_mode_invokes_pythonw_without_a_console(self) -> None:
        run = Mock(
            return_value=Mock(
                returncode=0,
                stdout=json.dumps({"accessToken": "token-value"}),
                stderr="",
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            cli_pythonw = Path(temp_dir) / "pythonw.exe"
            cli_pythonw.touch()
            token = azure_entra_token.acquire_azure_cli_token(
                azure_entra_token.DEFAULT_SCOPE,
                cli_pythonw=str(cli_pythonw),
                run=run,
            )

        self.assertEqual(token, "token-value")
        command = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertEqual(command[0], str(cli_pythonw))
        self.assertEqual(command[1:4], ["-IBm", "azure.cli", "account"])
        resource_index = command.index("--resource") + 1
        self.assertEqual(
            command[resource_index],
            "https://cognitiveservices.azure.com",
        )
        self.assertTrue(
            kwargs.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW
        )
        startupinfo = kwargs.get("startupinfo")
        self.assertIsNotNone(startupinfo)
        self.assertTrue(startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(startupinfo.wShowWindow, subprocess.SW_HIDE)

    @patch("azure_entra_token.acquire_azure_cli_token")
    @patch("azure_entra_token.DefaultAzureCredential")
    def test_uses_default_scope_and_trims_token(
        self,
        credential_factory: Mock,
        acquire_cli_token: Mock,
    ) -> None:
        acquire_cli_token.return_value = " token-value \n"

        token = azure_entra_token.acquire_token()

        self.assertEqual(token, "token-value")
        acquire_cli_token.assert_called_once_with(
            azure_entra_token.DEFAULT_SCOPE,
        )
        credential_factory.assert_not_called()

    @patch.dict(
        os.environ,
        {azure_entra_token.SCOPE_ENV_VAR: "https://ai.azure.com/.default"},
        clear=False,
    )
    @patch("azure_entra_token.acquire_azure_cli_token")
    @patch("azure_entra_token.DefaultAzureCredential")
    def test_supports_scope_override(
        self,
        credential_factory: Mock,
        acquire_cli_token: Mock,
    ) -> None:
        acquire_cli_token.return_value = "token-value"

        azure_entra_token.acquire_token()

        acquire_cli_token.assert_called_once_with(
            "https://ai.azure.com/.default",
        )
        credential_factory.assert_not_called()

    @patch.dict(
        os.environ,
        {azure_entra_token.CREDENTIAL_MODE_ENV_VAR: "default"},
        clear=False,
    )
    def test_supports_full_default_credential_chain(self) -> None:
        self.assertEqual(
            azure_entra_token.credential_options(),
            {"process_timeout": azure_entra_token.PROCESS_TIMEOUT_SECONDS},
        )

    @patch.dict(
        os.environ,
        {azure_entra_token.CREDENTIAL_MODE_ENV_VAR: "invalid"},
        clear=False,
    )
    def test_rejects_invalid_credential_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "azure-cli.*default"):
            azure_entra_token.credential_options()

    @patch("azure_entra_token.time.sleep")
    @patch("azure_entra_token.acquire_azure_cli_token")
    def test_retries_transient_authentication_failure(
        self,
        acquire_cli_token: Mock,
        sleep: Mock,
    ) -> None:
        acquire_cli_token.side_effect = [
            ClientAuthenticationError("temporary failure"),
            "token-value",
        ]

        token = azure_entra_token.acquire_token()

        self.assertEqual(token, "token-value")
        sleep.assert_called_once_with(1)

    @patch("azure_entra_token.acquire_azure_cli_token")
    def test_rejects_empty_token(
        self,
        acquire_cli_token: Mock,
    ) -> None:
        acquire_cli_token.return_value = "  "

        with self.assertRaisesRegex(RuntimeError, "empty access token"):
            azure_entra_token.acquire_token()

    @patch(
        "azure_entra_token.acquire_token",
        side_effect=ClientAuthenticationError("credential unavailable"),
    )
    def test_main_reports_authentication_failure(self, _: Mock) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = azure_entra_token.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("credential unavailable", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
