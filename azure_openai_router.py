from __future__ import annotations

import argparse
import http.client
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
MAX_REQUEST_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class RouterConfig:
    listen_host: str
    listen_port: int
    routes: dict[str, str]


def load_config(path: Path) -> RouterConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Router config must be a JSON object")

    listen_host = raw.get("listen_host", "127.0.0.1")
    if listen_host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("Router listener must use a loopback host")

    listen_port = raw.get("listen_port", 8765)
    if not isinstance(listen_port, int) or not 1 <= listen_port <= 65535:
        raise ValueError("listen_port must be an integer from 1 to 65535")

    raw_routes = raw.get("routes")
    if not isinstance(raw_routes, dict) or not raw_routes:
        raise ValueError("Router config must define at least one model route")

    routes: dict[str, str] = {}
    for model, endpoint in raw_routes.items():
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Every route must have a non-empty model name")
        if not isinstance(endpoint, str):
            raise ValueError(f"Route {model!r} must have a string endpoint")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError(f"Route {model!r} has an invalid endpoint")
        routes[model] = endpoint.rstrip("/")

    return RouterConfig(
        listen_host=listen_host,
        listen_port=listen_port,
        routes=routes,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Route Codex models to Azure OpenAI")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration without starting the server",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Invalid router configuration: {exc}", file=sys.stderr)
        return 2

    route_label = "route" if len(config.routes) == 1 else "routes"
    if args.check:
        print(
            f"Router configuration is valid: "
            f"{len(config.routes)} {route_label} on "
            f"{config.listen_host}:{config.listen_port}"
        )
        return 0

    server = create_server(config)
    print(
        f"Azure OpenAI router listening on "
        f"http://{config.listen_host}:{config.listen_port} "
        f"with {len(config.routes)} {route_label}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def create_server(config: RouterConfig) -> ThreadingHTTPServer:
    class RouterHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/health":
                self._send_json(
                    200,
                    {"status": "ok", "route_count": len(config.routes)},
                )
                return
            if path == "/v1/models":
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {"id": model, "object": "model"}
                            for model in sorted(config.routes)
                        ],
                    },
                )
                return
            self._send_json(
                404,
                {"error": {"code": "not_found", "message": "Route not found"}},
            )

        def do_POST(self) -> None:
            authorization = self.headers.get("Authorization", "")
            if not authorization.startswith("Bearer "):
                self._send_json(
                    401,
                    {
                        "error": {
                            "code": "missing_bearer_token",
                            "message": "Authorization bearer token is required",
                        }
                    },
                )
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length > MAX_REQUEST_BYTES:
                self._send_json(
                    413,
                    {
                        "error": {
                            "code": "request_too_large",
                            "message": "Request body is too large",
                        }
                    },
                )
                return
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(
                    400,
                    {
                        "error": {
                            "code": "invalid_json",
                            "message": "Request body must be valid JSON",
                        }
                    },
                )
                return

            model = payload.get("model")
            endpoint = config.routes.get(model)
            if endpoint is None:
                self._send_json(
                    400,
                    {
                        "error": {
                            "code": "unknown_model",
                            "message": f"No Azure route is configured for model {model!r}",
                        }
                    },
                )
                return

            self._proxy(endpoint, body)

        def _proxy(self, endpoint: str, body: bytes) -> None:
            endpoint_url = urlsplit(endpoint)
            request_url = urlsplit(self.path)
            if not request_url.path.startswith("/v1/"):
                self._send_json(
                    404,
                    {"error": {"code": "not_found", "message": "Route not found"}},
                )
                return

            suffix = request_url.path[len("/v1") :]
            target_path = f"{endpoint_url.path.rstrip('/')}{suffix}"
            if request_url.query:
                target_path = f"{target_path}?{request_url.query}"

            connection_type = (
                http.client.HTTPSConnection
                if endpoint_url.scheme == "https"
                else http.client.HTTPConnection
            )
            connection = connection_type(
                endpoint_url.hostname,
                endpoint_url.port,
                timeout=300,
            )
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower()
                not in HOP_BY_HOP_HEADERS | {"host", "content-length"}
            }
            headers["Content-Length"] = str(len(body))

            try:
                connection.request(
                    self.command,
                    target_path,
                    body=body,
                    headers=headers,
                )
                upstream = connection.getresponse()
                self.send_response(upstream.status, upstream.reason)
                for key, value in upstream.getheaders():
                    if key.lower() not in HOP_BY_HOP_HEADERS:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                while chunk := upstream.read(64 * 1024):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (OSError, http.client.HTTPException, TimeoutError) as exc:
                if not self.wfile.closed:
                    self._send_json(
                        502,
                        {
                            "error": {
                                "code": "upstream_error",
                                "message": str(exc),
                            }
                        },
                    )
            finally:
                connection.close()
                self.close_connection = True

        def _send_json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def log_message(self, _: str, *args: object) -> None:
            del args

    return ThreadingHTTPServer(
        (config.listen_host, config.listen_port),
        RouterHandler,
    )


if __name__ == "__main__":
    raise SystemExit(main())
