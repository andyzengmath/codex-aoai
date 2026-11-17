from __future__ import annotations

import base64
import http.client
import json
import io
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

from azure_openai_router import (
    EntraTokenCache,
    RouterConfig,
    create_server,
    load_config,
    main,
)


class RecordingUpstreamHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        self.__class__.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "authorization_values": self.headers.get_all("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "body": body,
            }
        )

        chunks = (
            b"event: response.output_text.delta\n",
            b'data: {"delta":"OK"}\n\n',
            b"event: response.completed\n",
            b"data: {}\n\n",
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(chunk)
            self.wfile.flush()

    def log_message(self, _: str, *args: object) -> None:
        del args


@contextmanager
def running_server(server: ThreadingHTTPServer):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request_json(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def unsigned_jwt(exp: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode()
    ).rstrip(b"=")
    return f"header.{payload.decode()}.signature"


class AzureOpenAIRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        RecordingUpstreamHandler.requests = []
        self.upstream = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            RecordingUpstreamHandler,
        )
        upstream_port = self.upstream.server_address[1]
        self.config = RouterConfig(
            listen_host="127.0.0.1",
            listen_port=0,
            routes={
                "MODEL_PRIMARY": f"http://127.0.0.1:{upstream_port}/openai/v1",
                "MODEL_SECONDARY": f"http://127.0.0.1:{upstream_port}/openai/v1",
            },
        )
        self.router = create_server(
            self.config,
            token_provider=lambda: "entra-token",
        )

    def tearDown(self) -> None:
        self.upstream.server_close()
        self.router.server_close()

    def test_routes_responses_by_model_and_forwards_bearer_token(self) -> None:
        payload = json.dumps(
            {"model": "MODEL_SECONDARY", "input": "hello", "stream": True}
        ).encode()

        with running_server(self.upstream), running_server(self.router):
            router_port = self.router.server_address[1]
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                router_port,
                timeout=5,
            )
            connection.putrequest("POST", "/v1/responses")
            connection.putheader("authorization", "Bearer chatgpt-token")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(payload)))
            connection.endheaders(payload)
            response = connection.getresponse()
            try:
                status = response.status
                content_type = response.headers.get_content_type()
                body = response.read()
            finally:
                response.close()
                connection.close()

        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/event-stream")
        self.assertIn(b"response.output_text.delta", body)
        self.assertEqual(
            RecordingUpstreamHandler.requests,
            [
                {
                    "path": "/openai/v1/responses",
                    "authorization": "Bearer entra-token",
                    "authorization_values": ["Bearer entra-token"],
                    "content_type": "application/json",
                    "body": payload,
                }
            ],
        )

    def test_rejects_unknown_model_without_contacting_upstream(self) -> None:
        payload = json.dumps({"model": "MODEL_UNKNOWN", "input": "hello"}).encode()

        with running_server(self.upstream), running_server(self.router):
            router_port = self.router.server_address[1]
            request = urllib.request.Request(
                f"http://127.0.0.1:{router_port}/v1/responses",
                data=payload,
                method="POST",
                headers={
                    "Authorization": "Bearer entra-token",
                    "Content-Type": "application/json",
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)

        response = caught.exception
        self.assertEqual(response.code, 400)
        error = json.loads(response.read())
        response.close()
        self.assertEqual(error["error"]["code"], "unknown_model")
        self.assertEqual(RecordingUpstreamHandler.requests, [])

    def test_passes_prefixed_models_to_a_loopback_gateway_without_entra(self) -> None:
        upstream_port = self.upstream.server_address[1]
        config = RouterConfig(
            listen_host="127.0.0.1",
            listen_port=0,
            routes=self.config.routes,
            passthrough={"gateway/": f"http://127.0.0.1:{upstream_port}/v1"},
        )
        router = create_server(config, token_provider=lambda: "entra-token")
        payload = json.dumps({"model": "gateway/model-x", "input": "hello"}).encode()

        with running_server(self.upstream), running_server(router):
            request = urllib.request.Request(
                f"http://127.0.0.1:{router.server_address[1]}/v1/responses",
                data=payload,
                method="POST",
                headers={
                    "Authorization": "Bearer client-token",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                status = response.status

        self.assertEqual(status, 200)
        self.assertEqual(len(RecordingUpstreamHandler.requests), 1)
        forwarded = RecordingUpstreamHandler.requests[0]
        self.assertEqual(forwarded["path"], "/v1/responses")
        self.assertEqual(forwarded["authorization"], "Bearer not-needed")
        self.assertEqual(forwarded["body"], payload)

    def test_rejects_non_loopback_passthrough(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routes.json"
            path.write_text(
                json.dumps(
                    {
                        "routes": {
                            "MODEL_PRIMARY": "https://resource.openai.azure.com/openai/v1"
                        },
                        "passthrough": {"gateway/": "https://gateway.example/v1"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_config(path)

    def test_reports_a_token_failure_instead_of_dropping_the_connection(self) -> None:
        def failing_token_provider() -> str:
            raise OSError("Azure CLI is not installed")

        router = create_server(self.config, token_provider=failing_token_provider)
        payload = json.dumps({"model": "MODEL_PRIMARY", "input": "hello"}).encode()

        with running_server(self.upstream), running_server(router):
            request = urllib.request.Request(
                f"http://127.0.0.1:{router.server_address[1]}/v1/responses",
                data=payload,
                method="POST",
                headers={
                    "Authorization": "Bearer client-token",
                    "Content-Type": "application/json",
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)

        response = caught.exception
        self.assertEqual(response.code, 502)
        error = json.loads(response.read())
        response.close()
        self.assertEqual(error["error"]["code"], "entra_authentication_failed")
        self.assertEqual(RecordingUpstreamHandler.requests, [])

    def test_rejects_missing_bearer_token(self) -> None:
        payload = json.dumps({"model": "MODEL_PRIMARY", "input": "hello"}).encode()

        with running_server(self.upstream), running_server(self.router):
            router_port = self.router.server_address[1]
            request = urllib.request.Request(
                f"http://127.0.0.1:{router_port}/v1/responses",
                data=payload,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)

        response = caught.exception
        self.assertEqual(response.code, 401)
        error = json.loads(response.read())
        response.close()
        self.assertEqual(error["error"]["code"], "missing_bearer_token")
        self.assertEqual(RecordingUpstreamHandler.requests, [])

    def test_exposes_health_and_model_list(self) -> None:
        with running_server(self.router):
            router_port = self.router.server_address[1]
            health = request_json(f"http://127.0.0.1:{router_port}/health")
            models = request_json(f"http://127.0.0.1:{router_port}/v1/models")

        self.assertEqual(health, {"status": "ok", "route_count": 2})
        self.assertEqual(models["object"], "list")
        self.assertEqual(
            [model["id"] for model in models["data"]],
            ["MODEL_PRIMARY", "MODEL_SECONDARY"],
        )

    def test_entra_token_cache_reuses_and_refreshes_before_expiry(self) -> None:
        now = [100.0]
        loader = Mock(
            side_effect=[
                unsigned_jwt(1000),
                unsigned_jwt(2000),
                unsigned_jwt(3000),
            ]
        )
        cache = EntraTokenCache(
            loader,
            clock=lambda: now[0],
            refresh_skew_seconds=300,
        )

        first = cache.get_token()
        now[0] = 600
        cached = cache.get_token()
        now[0] = 701
        refreshed = cache.get_token()

        self.assertEqual(first, cached)
        self.assertNotEqual(refreshed, first)
        self.assertEqual(loader.call_count, 2)

    def test_loads_placeholder_route_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "routes.json"
            config_path.write_text(
                json.dumps(
                    {
                        "listen_host": "127.0.0.1",
                        "listen_port": 8765,
                        "routes": {
                            "MODEL_PRIMARY": "https://RESOURCE_PRIMARY.example/openai/v1",
                            "MODEL_SECONDARY": "https://RESOURCE_SECONDARY.example/openai/v1",
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.listen_host, "127.0.0.1")
        self.assertEqual(config.listen_port, 8765)
        self.assertEqual(
            config.routes["MODEL_SECONDARY"],
            "https://RESOURCE_SECONDARY.example/openai/v1",
        )

    def test_rejects_non_loopback_listener(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "routes.json"
            config_path.write_text(
                json.dumps(
                    {
                        "listen_host": "0.0.0.0",
                        "listen_port": 8765,
                        "routes": {
                            "MODEL_PRIMARY": "https://RESOURCE_PRIMARY.example/openai/v1"
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "loopback"):
                load_config(config_path)

    def test_check_mode_validates_config_without_starting_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "routes.json"
            config_path.write_text(
                json.dumps(
                    {
                        "listen_host": "127.0.0.1",
                        "listen_port": 8765,
                        "routes": {
                            "MODEL_PRIMARY": "https://RESOURCE_PRIMARY.example/openai/v1"
                        },
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(["--config", str(config_path), "--check"])

        self.assertEqual(exit_code, 0)
        self.assertIn("1 route", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
