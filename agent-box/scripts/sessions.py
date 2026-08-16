#!/usr/bin/env python3
"""Session administration page for agent-box.

Lists the Claude Code sessions in the mounted state volume and hands each one to
the ttyd terminal on its own port. Standard library only; runs as the `claude`
user, because it only ever touches ~/.claude and root-owned files there would
break Claude Code itself.
"""

import base64
import hmac
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from session_store import DeleteError, SessionStore, delete_session


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-box-sessions"

    # Configured on the class before the server starts serving.
    store = None
    page = ""
    credential = ("", "")

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, b"ok\n", "text/plain")
        if not self._authorized():
            return self._challenge()
        if self.path == "/":
            return self._send(200, self.page.encode("utf-8"), "text/html; charset=utf-8")
        if self.path == "/api/sessions":
            # allow_nan=False: json.dumps otherwise emits bare NaN/Infinity,
            # which are not JSON. One of them anywhere in the document makes the
            # browser's JSON.parse throw on the *entire* response, so the page
            # loses every row rather than one field. The data layer already
            # scrubs non-finite values at source; this is the second wall.
            body = json.dumps(
                {"sessions": self.store.list_sessions(), "generatedAt": int(time.time())},
                allow_nan=False,
            ).encode("utf-8")
            return self._send(200, body, "application/json")
        return self._send(404, b"not found\n", "text/plain")

    def do_POST(self):
        if not self._authorized():
            return self._challenge()
        parts = self.path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "sessions" and parts[3] == "delete":
            try:
                path = delete_session(self.store.claude_home, parts[2])
            except DeleteError as error:
                if error.status == 500:
                    # The 500 text carries the raw OSError, including the absolute
                    # transcript path. Operators get it in the log; browsers do not —
                    # agent-box is a public image, and error strings travel further
                    # than pages do (screenshots, consoles, pasted bug reports).
                    logging.getLogger(__name__).error(
                        "delete %s failed: %s", parts[2], error
                    )
                    return self._send(500, b"could not remove the transcript\n", "text/plain")
                return self._send(error.status, ("%s\n" % error).encode("utf-8"), "text/plain")
            # The parse cache is keyed by path, so the deleted transcript's entry
            # would sit there until the process restarts. forget() exists for
            # exactly this caller; delete_session() hands back the path it
            # unlinked so nothing has to be recomputed to make the call.
            self.store.forget(path)
            return self._send(204, b"", "text/plain")
        return self._send(404, b"not found\n", "text/plain")

    def _authorized(self):
        user, password = self.credential
        if not user and not password:
            return True  # unset credentials disable auth
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        got_user, _, got_password = decoded.partition(":")
        # Both halves are always compared, so the reply time does not reveal
        # which one was wrong.
        return bool(
            hmac.compare_digest(got_user, user)
            & hmac.compare_digest(got_password, password)
        )

    def _challenge(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="agent-box"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    # 204 and 304 carry no body by definition, and RFC 7230 §3.3.2 says a server
    # MUST NOT send Content-Length with them. Every real client tolerates the
    # header, but a proxy is entitled not to.
    _BODYLESS = frozenset((204, 304))

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if status not in self._BODYLESS:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # the container log is for the terminal, not for every poll


def build_page(template_path, ttyd_public_port):
    """Load the UI, baking in the host port the terminal was published on.

    The container cannot discover its own published port, so it is told.
    """
    with open(template_path, encoding="utf-8") as handle:
        return handle.read().replace("__TTYD_PUBLIC_PORT__", str(ttyd_public_port))


def configure_logging():
    """Give the container log a timestamped handler of its own.

    Without this the only log line this process ever writes — the redacted
    delete failure — goes out through logging's lastResort handler: bare text on
    stderr, no timestamp, no level, and nothing to correlate it with the ttyd
    lines around it. Request lines stay unlogged on purpose (see log_message);
    every open tab polls every five seconds and would bury this.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def main():
    configure_logging()
    claude_home = os.environ.get("CLAUDE_HOME") or os.path.expanduser("~/.claude")
    here = os.path.dirname(os.path.abspath(__file__))

    Handler.store = SessionStore(claude_home)
    Handler.page = build_page(
        os.path.join(here, "sessions_page.html"),
        os.environ.get("TTYD_PUBLIC_PORT", "8085"),
    )
    Handler.credential = (
        os.environ.get("TTYD_USER", ""),
        os.environ.get("TTYD_PASSWORD", ""),
    )

    port = int(os.environ.get("SESSIONS_PORT") or 8082)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
