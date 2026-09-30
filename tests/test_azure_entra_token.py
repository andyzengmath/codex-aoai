from __future__ import annotations

import io
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from azure.core.exceptions import ClientAuthenticationError

import azure_entra_token


class AcquireTokenTests(unittest.TestCase):
    def test_hides_azure_cli_subprocess_window(self) -> None:
        captured: dict[str, object] = {}

        def fake_check_output(*args: object, **kwargs: object) -> bytes:
            captured["args"] = args
            captured["kwargs"] = kwargs
            return b"ok"

        hidden_check_output = azure_entra_token.hide_windows_subprocess(
            fake_check_output
        )
        result = hidden_check_output(["az.cmd", "account"])

        self.assertEqual(result, b"ok")
        kwargs = captured["kwargs"]
        self.assertIsInstance(kwargs, dict)
        self.assertTrue(
            kwargs.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW
        )
        startupinfo = kwargs.get("startupinfo")
        self.assertIsNotNone(startupinfo)
        self.assertTrue(startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(startupinfo.wShowWindow, subprocess.SW_HIDE)

    @patch("azure_entra_token.get_bearer_token_provider")
    @patch("azure_entra_token.DefaultAzureCredential")
    def test_uses_default_scope_and_trims_token(
        self,
        credential_factory: Mock,
        provider_factory: Mock,
    ) -> None:
        credential = credential_factory.return_value
        provider_factory.return_value = lambda: " token-value \n"

        token = azure_entra_token.acquire_token()

        self.assertEqual(token, "token-value")
        provider_factory.assert_called_once_with(
            credential,
            azure_entra_token.DEFAULT_SCOPE,
        )
        credential_factory.assert_called_once_with(
            process_timeout=azure_entra_token.PROCESS_TIMEOUT_SECONDS,
            **azure_entra_token.AZURE_CLI_CREDENTIAL_OPTIONS,
        )
        credential.close.assert_called_once_with()

    @patch.dict(
        os.environ,
        {azure_entra_token.SCOPE_ENV_VAR: "https://ai.azure.com/.default"},
        clear=False,
    )
    @patch("azure_entra_token.get_bearer_token_provider")
    @patch("azure_entra_token.DefaultAzureCredential")
    def test_supports_scope_override(
        self,
        credential_factory: Mock,
        provider_factory: Mock,
    ) -> None:
        credential = credential_factory.return_value
        provider_factory.return_value = lambda: "token-value"

        azure_entra_token.acquire_token()

        provider_factory.assert_called_once_with(
            credential,
            "https://ai.azure.com/.default",
        )

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
    @patch("azure_entra_token.get_bearer_token_provider")
    @patch("azure_entra_token.DefaultAzureCredential")
    def test_retries_transient_authentication_failure(
        self,
        credential_factory: Mock,
        provider_factory: Mock,
        sleep: Mock,
    ) -> None:
        first_credential = Mock()
        second_credential = Mock()
        credential_factory.side_effect = [first_credential, second_credential]
        provider_factory.side_effect = [
            lambda: (_ for _ in ()).throw(
                ClientAuthenticationError("temporary failure")
            ),
            lambda: "token-value",
        ]

        token = azure_entra_token.acquire_token()

        self.assertEqual(token, "token-value")
        sleep.assert_called_once_with(1)
        first_credential.close.assert_called_once_with()
        second_credential.close.assert_called_once_with()

    @patch("azure_entra_token.get_bearer_token_provider")
    @patch("azure_entra_token.DefaultAzureCredential")
    def test_rejects_empty_token(
        self,
        credential_factory: Mock,
        provider_factory: Mock,
    ) -> None:
        credential = credential_factory.return_value
        provider_factory.return_value = lambda: "  "

        with self.assertRaisesRegex(RuntimeError, "empty access token"):
            azure_entra_token.acquire_token()

        credential.close.assert_called_once_with()

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
