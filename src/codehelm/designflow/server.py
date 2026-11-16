"""Loopback review server for one DesignFlow mockup.

The server binds to 127.0.0.1. Sending comments stores them. It does not
launch a provider.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import urlsplit

from codehelm.designflow.loop import send_comments
from codehelm.designflow.render import render_screen
from codehelm.designflow.spec import DesignFlowError
from codehelm.designflow.store import DesignFlowStore


class ReviewServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, store: DesignFlowStore, screen_id: str, port: int = 0):
        self.store = store
        self.screen_id = screen_id
        super().__init__(("127.0.0.1", port), ReviewHandler)


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/":
                screen = self.server.store.require(self.server.screen_id)
                comments = self.server.store.open_comments(screen.id)
                body = render_screen(screen.id, screen.revision, screen.spec, comments).encode("utf-8")
                self._send(200, "text/html; charset=utf-8", body)
                return
            if path == "/api/comments":
                screen = self.server.store.require(self.server.screen_id)
                comments = self.server.store.open_comments(screen.id)
                payload = {
                    "comments": [
                        {
                            "element": comment.element_uid,
                            "text": comment.text,
                            "queued": comment.queued,
                        }
                        for comment in comments
                    ]
                }
                self._send(200, "application/json", _json(payload))
                return
        except DesignFlowError as exc:
            self._send(404, "application/json", _json({"error": str(exc)}))
            return
        self._send(404, "application/json", _json({"error": "not found"}))

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            body = _read_json(self)
            if path == "/api/comments":
                extra = set(body) - {"element", "text"}
                if extra or "element" not in body or "text" not in body:
                    raise DesignFlowError("Comment JSON needs element and text")
                if not isinstance(body["element"], str) or not isinstance(body["text"], str):
                    raise DesignFlowError("Comment element and text must be strings")
                comment = self.server.store.add_comment(
                    self.server.screen_id, body["element"], body["text"])
                screen = self.server.store.require(self.server.screen_id)
                labels = {field.uid: field.label for field in screen.spec.fields}
                labels["screen"] = screen.spec.title
                self._send(200, "application/json", _json({
                    "comment": {
                        "element": comment.element_uid,
                        "label": labels.get(comment.element_uid, comment.element_uid),
                        "text": comment.text,
                        "queued": comment.queued,
                    }
                }))
                return
            if path == "/api/send":
                if body:
                    raise DesignFlowError("Send takes an empty JSON object")
                queued = send_comments(self.server.store, self.server.screen_id)
                command = f"codehelm designflow revise {self.server.screen_id} --execute"
                self._send(200, "application/json", _json({"queued": len(queued), "command": command}))
                return
        except DesignFlowError as exc:
            status = 409 if str(exc) == "No open comments to send" else 400
            self._send(status, "application/json", _json({"error": str(exc)}))
            return
        except _BodyError as exc:
            self._send(exc.status, "application/json", _json({"error": str(exc)}))
            return
        self._send(404, "application/json", _json({"error": "not found"}))

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


class _BodyError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _json(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _read_json(handler: ReviewHandler) -> dict:
    raw_length = handler.headers.get("Content-Length", "0")
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise _BodyError(400, "Content-Length must be a number") from exc
    if length < 0 or length > 16384:
        raise _BodyError(413, "Request body is too large")
    if length == 0:
        return {}
    content_type = handler.headers.get("Content-Type", "")
    media = content_type.split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise _BodyError(415, "Content-Type must be application/json")
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _BodyError(400, "Request body must be JSON") from exc
    if not isinstance(data, dict):
        raise _BodyError(400, "Request body must be a JSON object")
    return data


def serve_review(store: DesignFlowStore, screen_id: str, port: int = 8765) -> None:
    store.require(screen_id)
    if port < 0 or port > 65535:
        raise DesignFlowError("Port must be between 0 and 65535")
    try:
        httpd = ReviewServer(store, screen_id, port)
    except OSError as exc:
        raise DesignFlowError(f"Cannot listen on 127.0.0.1:{port}") from exc
    host, actual = httpd.server_address
    print(f"review-url: http://{host}:{actual}/", flush=True)
    print(
        f"Comments stay on this machine. Send them from the page, then run "
        f"codehelm designflow revise {screen_id} --execute",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return
    finally:
        httpd.server_close()
