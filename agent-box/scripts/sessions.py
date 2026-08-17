#!/usr/bin/env python3
"""Session administration page for agent-box.

Lists the Claude Code sessions in the mounted state volume and hands each one to
the ttyd terminal on its own port. Standard library only; runs as the `claude`
user, because it only ever touches ~/.claude and root-owned files there would
break Claude Code itself.
"""

import base64
import hmac
import html
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


# What the browser tab is called when the deployment has no name of its own.
DEFAULT_PAGE_TITLE = "Agent Box sessions"

# The port this server listens on inside the container, and the host port it
# tells the page the agent tabs are published on. They are separate numbers for
# separate surfaces — the session list and the terminal — and each is the same
# on the host side as on the container side by default, so a compose mapping has
# no side to get backwards. ep.sh sets both explicitly; these defaults are what
# a hand-started server, or a deployment that leaves the variable out, gets.
#
# The second one is the only value on this page the container cannot work out
# for itself: docker publishes ports on the host side, and nothing in here can
# see that. Get it wrong and every link on the page points at a port this box
# does not answer on — see test_ports.py for what that failure looks like.
DEFAULT_LISTEN_PORT = 8090
DEFAULT_TABS_PUBLIC_PORT = 8091

# One line of a title bar. The name comes from docker-compose.yml or from
# `docker inspect`, so this is not a security boundary — but it is foreign text
# going into markup, and a multi-line or endless one would be no more useful in
# a tab than in the page.
_TITLE_LIMIT = 60


def page_title(agent_name):
    """The browser tab's name for this box's session list.

    The name is AGENT_NAME, which ep.sh resolves once at boot (see
    agent_name.sh): the operator's own setting, else the container's name, else
    the hostname. It is what an operator with two boxes open tells these
    otherwise identical tabs apart by, so falling all the way back to the
    generic title means every one of those lookups failed.
    """
    name = " ".join(agent_name.split()) if isinstance(agent_name, str) else ""
    if not name:
        return DEFAULT_PAGE_TITLE
    return "Sessions: %s" % name[:_TITLE_LIMIT]


def build_page(template_path, agent_tabs_public_port, agent_name=""):
    """Load the UI, baking in what the container cannot discover for itself.

    The published port is one such thing — docker publishes it on the host side
    and nothing inside can see it — and the box's own name is the other.
    """
    with open(template_path, encoding="utf-8") as handle:
        page = handle.read()
    return page.replace(
        "__AGENT_TABS_PUBLIC_PORT__", str(agent_tabs_public_port)
    ).replace("__PAGE_TITLE__", html.escape(page_title(agent_name)))


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
        os.environ.get("AGENT_TABS_PUBLIC_PORT") or DEFAULT_TABS_PUBLIC_PORT,
        os.environ.get("AGENT_NAME", ""),
    )
    Handler.credential = (
        os.environ.get("TTYD_USER", ""),
        os.environ.get("TTYD_PASSWORD", ""),
    )

    port = int(os.environ.get("SESSIONS_PORT") or DEFAULT_LISTEN_PORT)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
