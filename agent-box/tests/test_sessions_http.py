"""Unit tests for the sessions HTTP layer.

Every test drives a *real* server: ThreadingHTTPServer on 127.0.0.1 port 0, in a
daemon thread, spoken to over urllib. Nothing here calls a handler method
directly — the things worth pinning (status codes, the WWW-Authenticate
challenge, a 204 with no body, concurrent requests) only exist once a socket and
the http.server plumbing are involved.

Fixtures are imported from test_session_store: the data layer's shapes are the
ones this layer serves, and a second copy of them would drift.

CLAUDE_HOME is never the real one. Every case builds a tempfile.mkdtemp() home,
because /api/sessions/<id>/delete unlinks files.
"""

import base64
import collections
import contextlib
import html.parser
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
# The scripts directory holds the modules under test; this directory holds the
# fixtures imported below. `unittest discover` puts the second one on the path
# by itself, but `python3 -m unittest tests.test_sessions_http` does not, and
# both have to work.
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, HERE)

import sessions
import session_store
from test_session_store import (
    FRESH,
    SESSION_A,
    SESSION_B,
    ai_title,
    write_registry,
    write_transcript,
)


USER = "operator"
PASSWORD = "s3cret-pw"
PAGE = "<!doctype html><title>sessions</title>"

Response = collections.namedtuple("Response", "status headers body")


def basic(user, password):
    raw = ("%s:%s" % (user, password)).encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


class ServerTestCase(unittest.TestCase):
    """A configured server on an ephemeral port, torn down after each test."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.store = session_store.SessionStore(self.home)
        self.serve(self.store, (USER, PASSWORD))

    def serve(self, store, credential, page=PAGE):
        """Start a server whose handler is configured with these three values.

        The handler is a throwaway subclass rather than sessions.Handler itself:
        the configuration lives on the class, so patching the module's own class
        would leak between tests. Everything under test is inherited.
        """
        handler = type(
            "ConfiguredHandler",
            (sessions.Handler,),
            {"store": store, "page": page, "credential": credential},
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        # serve_forever() polls for the shutdown flag, so the default interval
        # is half a second of teardown on every single test.
        thread = threading.Thread(target=server.serve_forever, args=(0.02,),
                                  daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 10)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def fetch(self, path, method="GET", auth=True, authorization=None):
        """One request. A 4xx/5xx comes back as a Response, not as an exception.

        `auth=True` sends the configured credentials, `auth=False` sends no
        Authorization header at all, and a (user, password) tuple sends those.
        `authorization` overrides all of it with a verbatim header value.
        """
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        # A POST with no body still needs data set, or urllib sends a GET.
        data = b"" if method == "POST" else None
        request = urllib.request.Request(url, data=data, method=method)
        if authorization is not None:
            request.add_header("Authorization", authorization)
        elif auth is True:
            request.add_header("Authorization", basic(USER, PASSWORD))
        elif auth:
            request.add_header("Authorization", basic(*auth))
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return Response(response.status, response.headers, response.read())
        except urllib.error.HTTPError as error:
            with error:
                return Response(error.status, error.headers, error.read())

    def payload(self, path="/api/sessions", **kwargs):
        response = self.fetch(path, **kwargs)
        self.assertEqual(200, response.status)
        return json.loads(response.body)

    def rows(self):
        return self.payload()["sessions"]


class HealthTest(ServerTestCase):
    """/healthz is the container's liveness probe, so it answers before auth."""

    def test_healthz_needs_no_credentials(self):
        response = self.fetch("/healthz", auth=False)
        self.assertEqual(200, response.status)
        self.assertEqual(b"ok\n", response.body)

    def test_healthz_answers_the_same_with_credentials(self):
        self.assertEqual(b"ok\n", self.fetch("/healthz").body)

    def test_healthz_is_plain_text(self):
        response = self.fetch("/healthz", auth=False)
        self.assertEqual("text/plain", response.headers["Content-Type"])


class AuthTest(ServerTestCase):
    """Basic auth is the only thing between the public internet and a shell.

    ttyd is published on a host port and this page hands out terminals, so every
    route except the health probe is gated, and every malformed way of asking
    has to end in a 401 rather than a traceback.
    """

    def test_no_credentials_is_challenged(self):
        response = self.fetch("/", auth=False)
        self.assertEqual(401, response.status)
        self.assertEqual('Basic realm="agent-box"',
                         response.headers["WWW-Authenticate"])

    def test_a_wrong_password_is_refused(self):
        self.assertEqual(401, self.fetch("/", auth=(USER, "wrong")).status)

    def test_a_wrong_user_is_refused(self):
        self.assertEqual(401, self.fetch("/", auth=("nobody", PASSWORD)).status)

    def test_both_halves_wrong_is_refused(self):
        self.assertEqual(401, self.fetch("/", auth=("nobody", "wrong")).status)

    def test_a_prefix_of_the_password_is_refused(self):
        # compare_digest is not a prefix match, and neither is ==.
        self.assertEqual(401, self.fetch("/", auth=(USER, PASSWORD[:-1])).status)
        self.assertEqual(401, self.fetch("/", auth=(USER, PASSWORD + "x")).status)

    def test_correct_credentials_get_the_page(self):
        response = self.fetch("/")
        self.assertEqual(200, response.status)
        self.assertEqual(PAGE.encode("utf-8"), response.body)
        self.assertEqual("text/html; charset=utf-8",
                         response.headers["Content-Type"])

    def test_malformed_authorization_headers_are_refused_not_crashes(self):
        # Each of these used to be a plausible way to raise out of _authorized()
        # instead of answering: no scheme, the wrong scheme, base64 that will not
        # decode, base64 of bytes that are not UTF-8, and a decoded value with no
        # colon in it. A raise here is a dropped connection, not a 401.
        malformed = (
            "",
            "Basic",
            "Basic ",
            "Bearer " + base64.b64encode(b"x:y").decode("ascii"),
            "basic " + base64.b64encode(b"x:y").decode("ascii"),  # scheme is exact
            "Basic aaaaa",  # length 5: incorrect padding
            "Basic !!!!!!",
            "Basic " + base64.b64encode(b"\xff\xfe\xfd").decode("ascii"),
            "Basic " + base64.b64encode(b"no-colon-here").decode("ascii"),
            "Basic " + base64.b64encode(b":").decode("ascii"),
        )
        for header in malformed:
            with self.subTest(header=header):
                response = self.fetch("/", authorization=header)
                self.assertEqual(401, response.status)
                self.assertEqual('Basic realm="agent-box"',
                                 response.headers["WWW-Authenticate"])

    def test_the_api_is_gated_too(self):
        self.assertEqual(401, self.fetch("/api/sessions", auth=False).status)

    def test_an_unknown_path_is_gated_before_it_is_resolved(self):
        # Auth first, then routing: otherwise the 404 tells an anonymous caller
        # which paths exist.
        self.assertEqual(401, self.fetch("/nope", auth=False).status)

    def test_an_unknown_path_is_a_404_once_authorized(self):
        response = self.fetch("/nope")
        self.assertEqual(404, response.status)
        self.assertEqual(b"not found\n", response.body)

    def test_a_challenge_carries_no_body(self):
        response = self.fetch("/", auth=False)
        self.assertEqual(b"", response.body)
        self.assertEqual("0", response.headers["Content-Length"])


class NoCredentialsConfiguredTest(ServerTestCase):
    """Unset TTYD_USER/TTYD_PASSWORD disable auth rather than locking everyone out.

    That is the documented behaviour of the ttyd side of agent-box, and this
    page has to match it: a deployment that publishes nothing to the host and
    sets no credentials still has to be usable.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.store = session_store.SessionStore(self.home)
        self.serve(self.store, ("", ""))

    def test_the_page_is_served_without_any_header(self):
        response = self.fetch("/", auth=False)
        self.assertEqual(200, response.status)

    def test_the_api_is_served_without_any_header(self):
        self.assertEqual(200, self.fetch("/api/sessions", auth=False).status)

    def test_a_nonsense_header_is_ignored_rather_than_refused(self):
        self.assertEqual(200, self.fetch("/", authorization="Basic !!!").status)


class HalfConfiguredCredentialTest(ServerTestCase):
    """One half set is still auth: only *both* halves empty opens the door."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.store = session_store.SessionStore(self.home)
        self.serve(self.store, ("", PASSWORD))

    def test_a_password_alone_still_gates_the_page(self):
        self.assertEqual(401, self.fetch("/", auth=False).status)

    def test_the_empty_user_and_the_password_are_accepted(self):
        self.assertEqual(200, self.fetch("/", auth=("", PASSWORD)).status)


class ApiTest(ServerTestCase):
    def test_an_empty_home_lists_no_sessions(self):
        body = self.payload()
        self.assertEqual([], body["sessions"])
        self.assertIsInstance(body["generatedAt"], int)

    def test_generated_at_is_an_integer_unix_time(self):
        # The UI subtracts lastActive from it to show "3m ago", so it has to be
        # a number in the same units, not an ISO string.
        body = self.payload()
        self.assertIsInstance(body["generatedAt"], int)
        self.assertGreater(body["generatedAt"], 1_600_000_000)

    def test_the_content_type_is_json(self):
        response = self.fetch("/api/sessions")
        self.assertEqual("application/json", response.headers["Content-Type"])

    def test_sessions_are_listed_with_their_derived_fields(self):
        write_transcript(self.home, SESSION_A, [ai_title("A named session")])
        row = self.rows()[0]
        self.assertEqual(SESSION_A, row["id"])
        self.assertEqual("A named session", row["name"])
        self.assertFalse(row["live"])
        self.assertEqual(0, row["messages"])
        self.assertIsNone(row["contextTokens"])

    def test_a_live_session_is_reported_live_with_its_status(self):
        write_transcript(self.home, SESSION_B, [ai_title("Running")])
        write_registry(
            self.home, "205.json", pid=os.getpid(), sessionId=SESSION_B,
            status="busy", updatedAt=FRESH,
        )
        row = self.rows()[0]
        self.assertTrue(row["live"])
        self.assertEqual("busy", row["status"])
        self.assertEqual(os.getpid(), row["pid"])

    def test_messages_and_context_tokens_reach_the_payload(self):
        write_transcript(
            self.home,
            SESSION_A,
            [
                ai_title("Counted"),
                {"type": "user", "message": {"content": "hello"}},
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-opus-4-8",
                        "content": [],
                        "usage": {"input_tokens": 10, "cache_read_input_tokens": 5},
                    },
                },
            ],
        )
        row = self.rows()[0]
        self.assertEqual(2, row["messages"])
        self.assertEqual(15, row["contextTokens"])

    def test_every_session_in_the_home_is_listed(self):
        write_transcript(self.home, SESSION_A, [ai_title("One")])
        write_transcript(self.home, SESSION_B, [ai_title("Two")])
        self.assertEqual({SESSION_A, SESSION_B},
                         {row["id"] for row in self.rows()})

    def test_the_payload_survives_a_strict_json_parser(self):
        # json.dumps() writes bare NaN and Infinity by default, and neither is
        # JSON: one of them anywhere in the document makes the browser's
        # JSON.parse throw on the *whole* response, so the page loses every row
        # rather than one field. The data layer already scrubs non-finite values
        # at source; this pins the outcome from the browser's side, with the
        # values that used to produce them present in the fixture.
        write_transcript(
            self.home,
            SESSION_A,
            [
                ai_title("Adversarial"),
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-opus-4-8",
                        "content": [],
                        "usage": {
                            "input_tokens": float("nan"),
                            "cache_read_input_tokens": float("inf"),
                            "cache_creation_input_tokens": 7,
                        },
                    },
                },
            ],
        )
        write_registry(
            self.home, "205.json", pid=os.getpid(), sessionId=SESSION_A,
            status="busy", updatedAt=float("nan"),
        )
        response = self.fetch("/api/sessions")
        self.assertEqual(200, response.status)
        body = strict_loads(response.body)
        self.assertEqual(7, body["sessions"][0]["contextTokens"])

    def test_a_non_finite_value_never_leaves_the_process_as_invalid_json(self):
        # Belt and braces on a contract the data layer already holds: if a row
        # ever did carry a NaN, allow_nan=False refuses to serialise it — the
        # request fails loudly — instead of shipping a document no JSON parser
        # will read. What must never happen is the third outcome: a 200 whose
        # body a strict parser rejects.
        self.serve(BrokenStore(self.home), (USER, PASSWORD))
        # socketserver prints the refusal's traceback to stderr before it closes
        # the connection, so the noise is captured rather than left in the run.
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                response = self.fetch("/api/sessions")
            except (urllib.error.URLError, OSError):
                return  # refused before anything was sent, which is the point
        strict_loads(response.body)


def strict_loads(body):
    """json.loads that refuses NaN/Infinity, the way every browser does."""

    def refuse(value):
        raise AssertionError("not valid JSON: %s" % value)

    return json.loads(body.decode("utf-8"), parse_constant=refuse)


class BrokenStore:
    """A store that hands back a row the data layer promises never to produce."""

    def __init__(self, claude_home):
        self.claude_home = claude_home

    def list_sessions(self):
        return [{"id": SESSION_A, "contextTokens": float("nan")}]

    def forget(self, path):
        pass


class DeleteTest(ServerTestCase):
    def path_for(self, session_id):
        return "/api/sessions/%s/delete" % session_id

    def transcript(self, session_id=SESSION_A, title="A session"):
        return write_transcript(self.home, session_id, [ai_title(title)])

    def delete(self, session_id, **kwargs):
        return self.fetch(self.path_for(session_id), method="POST", **kwargs)

    def test_deleting_a_session_returns_204_and_removes_the_transcript(self):
        path = self.transcript()
        response = self.delete(SESSION_A)
        self.assertEqual(204, response.status)
        self.assertEqual(b"", response.body)
        self.assertFalse(os.path.exists(path))

    def test_a_deleted_session_is_gone_from_the_listing(self):
        self.transcript()
        self.assertEqual([SESSION_A], [row["id"] for row in self.rows()])
        self.delete(SESSION_A)
        self.assertEqual([], self.rows())

    def test_deleting_evicts_the_parse_cache(self):
        # The store caches a parse under (mtime_ns, size), so a transcript
        # recreated with the same size and the same mtime is indistinguishable
        # from the deleted one to the cache. Only an evicted entry re-reads the
        # file — without the forget() call the listing would still show the old
        # name. Both titles are the same length on purpose.
        path = self.transcript(title="Old title")
        before = os.stat(path)
        self.assertEqual("Old title", self.rows()[0]["name"])

        self.assertEqual(204, self.delete(SESSION_A).status)

        write_transcript(self.home, SESSION_A, [ai_title("New title")])
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(before.st_size, os.stat(path).st_size)

        self.assertEqual("New title", self.rows()[0]["name"])

    def test_a_live_session_is_refused_with_409_and_keeps_its_transcript(self):
        path = self.transcript(SESSION_B)
        write_registry(
            self.home, "205.json", pid=os.getpid(), sessionId=SESSION_B,
            status="busy", updatedAt=FRESH,
        )
        response = self.delete(SESSION_B)
        self.assertEqual(409, response.status)
        self.assertEqual(b"session is running\n", response.body)
        self.assertTrue(os.path.exists(path))

    def test_a_malformed_id_is_a_400(self):
        for bad in ("not-a-uuid", "..", "%2e%2e", "1234", SESSION_A[:-1]):
            with self.subTest(bad=bad):
                response = self.delete(bad)
                self.assertEqual(400, response.status)
                self.assertEqual(b"not a session id\n", response.body)

    def test_an_unknown_session_is_a_404(self):
        response = self.delete(SESSION_B)
        self.assertEqual(404, response.status)
        self.assertEqual(b"no such session\n", response.body)

    def test_deleting_requires_credentials_and_does_not_delete(self):
        path = self.transcript()
        response = self.delete(SESSION_A, auth=False)
        self.assertEqual(401, response.status)
        self.assertEqual('Basic realm="agent-box"',
                         response.headers["WWW-Authenticate"])
        self.assertTrue(os.path.exists(path))

    def test_a_wrong_password_does_not_delete(self):
        path = self.transcript()
        self.assertEqual(401, self.delete(SESSION_A, auth=(USER, "wrong")).status)
        self.assertTrue(os.path.exists(path))

    def test_unknown_post_paths_are_404(self):
        for path in ("/", "/api", "/api/sessions", "/healthz",
                     "/api/sessions/%s" % SESSION_A,
                     "/api/sessions/%s/delete/extra" % SESSION_A,
                     "/api/sessions/%s/rename" % SESSION_A,
                     "/api/other/%s/delete" % SESSION_A):
            with self.subTest(path=path):
                response = self.fetch(path, method="POST")
                self.assertEqual(404, response.status)
                self.assertEqual(b"not found\n", response.body)

    def test_a_post_to_an_unknown_path_is_gated_too(self):
        self.assertEqual(401, self.fetch("/api", method="POST", auth=False).status)


class DeleteFailureRedactionTest(ServerTestCase):
    """A 500 tells the operator everything and the browser nothing.

    delete_session()'s 500 message interpolates the raw OSError, which carries
    the absolute transcript path — the encoded workspace directory and the
    session uuid. agent-box is a public image and error strings travel much
    further than pages do: screenshots, browser consoles, pasted bug reports.
    The operator's log is the right place for it, and the log runs inside the
    container.
    """

    def failing_transcript(self):
        """A directory named <uuid>.jsonl: os.unlink() raises IsADirectoryError.

        _iter_transcripts() lists names, so the id resolves; the unlink then
        fails with an errno that is neither ENOENT nor a refusal, which is
        exactly the 500 branch.
        """
        path = os.path.join(self.home, "projects", "-workspace",
                            SESSION_A + ".jsonl")
        os.makedirs(path)
        return path

    def test_the_body_carries_no_filesystem_path_but_the_log_does(self):
        path = self.failing_transcript()
        with self.assertLogs("sessions", level="ERROR") as logged:
            response = self.fetch("/api/sessions/%s/delete" % SESSION_A,
                                  method="POST")

        self.assertEqual(500, response.status)
        self.assertEqual(b"could not remove the transcript\n", response.body)

        text = response.body.decode("utf-8")
        self.assertNotIn(self.home, text)
        self.assertNotIn(path, text)
        self.assertNotIn(SESSION_A, text)
        self.assertNotIn("/", text)

        logline = "\n".join(logged.output)
        self.assertIn(path, logline)
        self.assertIn(SESSION_A, logline)
        self.assertTrue(os.path.isdir(path))  # nothing was removed

    def test_the_400_404_and_409_messages_are_forwarded_verbatim(self):
        # Only the 500 is redacted: the other three are path-free by
        # construction and are the whole of what the UI can tell the user.
        self.assertEqual(b"not a session id\n",
                         self.fetch("/api/sessions/nope/delete",
                                    method="POST").body)
        self.assertEqual(b"no such session\n",
                         self.fetch("/api/sessions/%s/delete" % SESSION_B,
                                    method="POST").body)


class ConcurrencyTest(ServerTestCase):
    """ThreadingHTTPServer is the deployment shape, so it is the test shape.

    Every open browser tab polls this server every few seconds, and a delete
    lands while those polls are in flight.
    """

    def test_concurrent_reads_all_succeed(self):
        for index, session_id in enumerate((SESSION_A, SESSION_B)):
            write_transcript(self.home, session_id, [ai_title("Session %d" % index)])

        def read(_):
            response = self.fetch("/api/sessions")
            body = strict_loads(response.body)
            return response.status, len(body["sessions"])

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(read, range(24)))

        self.assertEqual([(200, 2)] * 24, results)

    def test_concurrent_health_probes_all_succeed(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            bodies = list(pool.map(lambda _: self.fetch("/healthz", auth=False).body,
                                   range(24)))
        self.assertEqual([b"ok\n"] * 24, bodies)


class BuildPageTest(unittest.TestCase):
    """The container cannot discover the host port ttyd was published on.

    docker publishes it on the host side, and nothing inside the container can
    see it, so the port is passed in and baked into the page at startup.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def template(self, text):
        path = os.path.join(self.directory, "page.html")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_the_placeholder_is_replaced_everywhere(self):
        path = self.template("a __AGENT_TABS_PUBLIC_PORT__ b __AGENT_TABS_PUBLIC_PORT__")
        self.assertEqual("a 9999 b 9999", sessions.build_page(path, 9999))

    def test_the_port_may_arrive_as_a_string(self):
        # It comes from os.environ, so it usually does.
        path = self.template("port=__AGENT_TABS_PUBLIC_PORT__")
        self.assertEqual("port=8091", sessions.build_page(path, "8091"))

    def test_a_template_without_the_placeholder_is_returned_unchanged(self):
        path = self.template("<p>no port here</p>")
        self.assertEqual("<p>no port here</p>", sessions.build_page(path, 8091))


class PageTitleTest(unittest.TestCase):
    """The name of the browser tab this page is open in.

    An operator with several boxes open has several of these tabs, and they are
    otherwise identical. The name is AGENT_NAME, resolved once at boot by
    agent_name.sh — from the operator's own setting, the container's name, or
    the hostname — so this page is never handed nothing to be called.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def title(self, box_name):
        path = os.path.join(self.directory, "page.html")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("<title>__PAGE_TITLE__</title>")
        built = sessions.build_page(path, 8091, box_name)
        return built[len("<title>"): -len("</title>")]

    def test_the_box_name_becomes_the_tab_name(self):
        self.assertEqual("Sessions: agent-box-dev", self.title("agent-box-dev"))

    def test_a_box_with_no_name_keeps_the_generic_one(self):
        # Only reachable when even the hostname lookup failed, but a title bar
        # reading "Sessions: " would be worse than the generic name.
        for value in ("", None, "   "):
            with self.subTest(value=value):
                self.assertEqual("Agent Box sessions", self.title(value))

    def test_the_name_is_escaped_on_its_way_into_the_markup(self):
        # It comes from docker-compose.yml or from `docker inspect` rather than
        # from a browser, but it is foreign text going into HTML, and this page
        # is a public image.
        title = self.title('</title><script>alert(1)</script>')
        self.assertNotIn("<script>", title)
        self.assertNotIn("</title>", title)

    def test_the_name_is_one_bounded_line(self):
        self.assertEqual("Sessions: a b", self.title("a\n b"))
        self.assertLessEqual(len(self.title("x" * 500)), 100)


TEMPLATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "sessions_page.html"
)

# A title is whatever the model wrote and reaches the page verbatim: the data
# layer bounds its length and collapses its whitespace, and deliberately does
# not sanitise it. Both halves matter — the tag closes an attribute the page
# might interpolate into, and the tag opens a script the page must never run.
HOSTILE = '<script>alert(1)</script> "><img src=x onerror=alert(1)>'

# A minimal DOM, so the page's own render() can be driven without a browser.
# Everything the script touches at load is stubbed; nothing is simulated beyond
# what the page actually uses, because a fake that guesses is worse than no fake.
HARNESS = """
const nodes = {};
const element = () => ({
  hidden: false, textContent: "", innerHTML: "", href: "", dataset: {},
  addEventListener() {},
});
globalThis.document = {
  hidden: false,
  addEventListener() {},
  getElementById(id) { return (nodes[id] = nodes[id] || element()); },
};
globalThis.location = { protocol: "http:", hostname: "box.example" };
globalThis.window = globalThis;
globalThis.fetch = () => Promise.reject(new Error("the harness has no network"));
globalThis.setTimeout = () => 0;
globalThis.setInterval = () => 0;
globalThis.__nodes = nodes;
"""


def built_page(port=sessions.DEFAULT_TABS_PUBLIC_PORT):
    return sessions.build_page(TEMPLATE, port)


def page_script(page=None):
    """The contents of the page's single <script> element."""
    scripts = re.findall(r"<script>(.*?)</script>", page or built_page(), re.S)
    if len(scripts) != 1:
        raise AssertionError("expected exactly one <script>, found %d" % len(scripts))
    return scripts[0]


def run_in_node(probe, page=None):
    """Evaluate the page's script under the DOM stub, then run `probe`.

    The script is plain top-level code, so its consts are in scope for anything
    appended after it — which is how render() and escapeHtml() are reached
    without exporting them from a file that has to stay browser-loadable.
    """
    directory = tempfile.mkdtemp()
    try:
        path = os.path.join(directory, "harness.js")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(HARNESS + page_script(page) + "\n" + probe)
        result = subprocess.run(
            ["node", path], capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            raise AssertionError("node failed:\n%s" % result.stderr)
        return result.stdout
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def render_html(rows, page=None):
    """The list HTML the page builds for these sessions."""
    probe = "render(%s);\nconsole.log(__nodes.list.innerHTML);\n" % json.dumps(rows)
    return run_in_node(probe, page)


def row_builder(page=None):
    """The source of rowHtml(), the one function that writes markup."""
    script = page_script(page)
    start = script.index("const rowHtml")
    return script[start:script.index("\n  };", start)]


def interpolations(text):
    """Every `${...}` in `text`, brace-matched so nested literals stay whole."""
    found = []
    index = text.find("${")
    while index != -1:
        depth, cursor = 0, index + 1
        while cursor < len(text):
            if text[cursor] == "{":
                depth += 1
            elif text[cursor] == "}":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        found.append(text[index + 2:cursor])
        index = text.find("${", cursor)
    return found


class Markup(html.parser.HTMLParser):
    """Reads rendered markup back the way a browser would.

    The point is what the browser *concludes* from the string, not what the
    string looks like: an escaped <img> is text, and the only way to assert that
    without hand-waving is to parse it and find no img element.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.attributes = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(name for name, _value in attrs)

    def handle_data(self, data):
        self.text.append(data)


def parse_markup(markup):
    parser = Markup()
    parser.feed(markup)
    parser.close()
    return parser


VOID_ELEMENTS = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)


class TagBalance(html.parser.HTMLParser):
    """Enough of a validator to catch the mistake that matters: an unclosed tag.

    A stray or mismatched close tag is recorded rather than raised so one
    failure reports every problem in the document, not just the first.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.problems = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID_ELEMENTS:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID_ELEMENTS:
            return  # <br/> reaches here through handle_startendtag
        if not self.stack:
            self.problems.append("stray </%s>" % tag)
        elif self.stack[-1] != tag:
            self.problems.append("</%s> closes <%s>" % (tag, self.stack[-1]))
        else:
            self.stack.pop()


class PageTemplateTest(unittest.TestCase):
    """The template on disk: the port is baked in and the markup is well formed."""

    def test_the_public_port_is_baked_into_the_page(self):
        page = built_page(9999)
        self.assertIn("9999", page)
        self.assertNotIn("__AGENT_TABS_PUBLIC_PORT__", page)
        self.assertIn("/api/sessions", page)

    def test_the_page_has_a_title_and_no_placeholder_left_in_it(self):
        page = built_page()
        self.assertNotIn("__PAGE_TITLE__", page)
        self.assertIn("<title>Agent Box sessions</title>", page)

    def test_the_terminal_url_is_built_from_the_browsers_hostname(self):
        # Only the port can be baked in: the container has no idea what
        # hostname the operator reached it by.
        page = built_page(9999)
        self.assertIn("location.hostname", page)
        self.assertIn("?arg=", page)

    def test_the_template_parses_as_balanced_html(self):
        parser = TagBalance()
        parser.feed(built_page())
        parser.close()
        self.assertEqual([], parser.problems)
        self.assertEqual([], parser.stack, "unclosed tags")

    def test_the_page_carries_no_external_reference(self):
        # No CDN, no webfont, no build step: the box is often reached over a
        # tunnel with no internet on the other side.
        page = built_page()
        for pattern in ("http://", "https://", "//cdn", "<img", "@import"):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, page)

    def test_the_entry_count_is_never_labelled_messages(self):
        # A tool result is counted too, so the number runs at about twice a
        # human's idea of turns. "msgs" would be a lie; "entries" is not.
        page = built_page()
        self.assertIn("entries", page)
        self.assertNotIn("msgs", page)

    def test_no_context_percentage_survived(self):
        # The context window size is recorded nowhere, so a percentage could
        # only ever be measured against a guess.
        page = built_page()
        self.assertNotIn("%<", page)
        self.assertNotIn("contextPercent", page)
        self.assertNotIn("<progress", page)

    def test_the_poll_does_not_cache_bust_with_a_query_string(self):
        # The server matches /api/sessions exactly, so /api/sessions?t=1 is a
        # 404 and the page would show nothing at all.
        page = built_page()
        self.assertIn('fetch("/api/sessions", { cache: "no-store" })', page)
        self.assertNotIn("/api/sessions?", page)


class PageServedTest(ServerTestCase):
    """The real template, served the way the container serves it."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.store = session_store.SessionStore(self.home)
        self.page = built_page()
        self.serve(self.store, (USER, PASSWORD), page=self.page)

    def test_the_page_comes_back_intact_as_html(self):
        response = self.fetch("/")
        self.assertEqual(200, response.status)
        self.assertEqual("text/html; charset=utf-8", response.headers["Content-Type"])
        self.assertEqual(self.page, response.body.decode("utf-8"))

    def test_the_page_needs_credentials(self):
        response = self.fetch("/", auth=False)
        self.assertEqual(401, response.status)
        self.assertEqual(b"", response.body)


class EscapingTest(ServerTestCase):
    """The API hands out raw strings; the page is what makes them safe.

    Escaping in the JSON would be the wrong layer — a consumer other than this
    page would then have to unescape — so the contract is: the API is verbatim,
    and every single field is escaped on its way into HTML.
    """

    def test_the_api_serves_the_title_verbatim(self):
        write_transcript(self.home, SESSION_A, [ai_title(HOSTILE)])
        self.assertEqual(HOSTILE, self.rows()[0]["name"])
        self.assertIn("<script>alert(1)</script>",
                      self.fetch("/api/sessions").body.decode("utf-8"))

    def test_every_interpolation_in_a_row_is_escaped(self):
        # A static reading of the function that builds a row: no field of a
        # session may reach the markup without going through escapeHtml. The
        # rendered assertions below prove today's markup is safe; this one is
        # what still holds after someone adds a column next year.
        offenders = [
            part
            for part in interpolations(row_builder())
            if "session." in part and "escapeHtml(" not in part
        ]
        self.assertEqual([], offenders)

    def test_the_values_a_row_interpolates_indirectly_are_escaped_too(self):
        # `hint` and `label` are the two session-derived locals the template
        # interpolates by name, so the check above cannot see into them.
        body = row_builder()
        self.assertIn("const hint = escapeHtml(", body)
        self.assertIn("const label = escapeHtml(", body)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class NodeRenderTest(unittest.TestCase):
    """The page's own functions, driven under node with a stub DOM.

    There is no browser here, and asserting on hand-written expectations of what
    the page "would" produce would only test the expectations. Running the real
    script is the difference between believing it escapes and knowing it does.
    """

    def render(self, rows):
        return render_html(rows)

    def session(self, **overrides):
        row = {
            "id": SESSION_A,
            "projectDir": "-workspace",
            "name": "Add Terraform to container image",
            "nameSource": "ai-title",
            "lastActive": int(time.time()) - 5400,
            "messages": 382,
            "bytes": 1659722,
            "contextTokens": 63883,
            "lastPrompt": "Lets add terraform to the image",
            "activity": "Bash: Check ttyd flags",
            "live": False,
            "status": None,
            "waitingFor": None,
            "pid": None,
        }
        row.update(overrides)
        return row

    def test_the_script_is_syntactically_valid(self):
        # A syntax error anywhere in the script leaves the page blank, with the
        # only evidence in a console nobody has open.
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = os.path.join(directory, "page.js")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(page_script())
        result = subprocess.run(["node", "--check", path],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_a_hostile_title_is_escaped_into_text(self):
        markup = self.render([self.session(name=HOSTILE)])
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", markup)

        # Parsed rather than string-matched: "onerror" appears in the output
        # either way, and only a parser can say whether it is an attribute or
        # the text of a row. The title has to survive as text, character for
        # character, and produce no element and no handler.
        parsed = parse_markup(markup)
        self.assertIn(HOSTILE, "".join(parsed.text))
        self.assertEqual(["li", "span", "a", "span", "span", "button", "div",
                          "span", "span"], parsed.tags)
        self.assertEqual([], [name for name in parsed.attributes
                              if name.startswith("on")])

    def test_the_escape_function_handles_every_dangerous_character(self):
        out = run_in_node(
            'console.log(escapeHtml(`<script>alert("x") & \'y\'</script>`));\n'
        )
        self.assertEqual(
            "&lt;script&gt;alert(&quot;x&quot;) &amp; &#39;y&#39;&lt;/script&gt;",
            out.strip(),
        )

    def test_control_characters_and_bidi_overrides_are_dropped(self):
        # Neither can be escaped into harmlessness: U+202E reverses the rest of
        # the line it lands in, which is how "gnp.exe" reads as "exe.png".
        out = run_in_node(
            'console.log(JSON.stringify(escapeHtml("a\\u202Eb\\u0007c\\u2066d")));\n'
        )
        self.assertEqual('"abcd"', out.strip())

    def test_a_hostile_status_from_the_registry_is_escaped_too(self):
        # status and waitingFor are foreign JSON written by another process.
        markup = self.render([
            self.session(live=True, status="waiting", waitingFor=HOSTILE)
        ])
        self.assertNotIn("<img", markup)
        self.assertIn("&lt;script&gt;", markup)

    def test_an_idle_session_links_to_its_own_terminal(self):
        markup = self.render([self.session()])
        self.assertIn(
            'href="http://box.example:8091/?arg=%s"' % SESSION_A, markup
        )
        self.assertIn('target="_blank"', markup)
        self.assertIn('rel="noopener"', markup)

    def test_a_live_session_is_not_a_link_and_cannot_be_deleted(self):
        # Two Claude processes on one transcript corrupt it, so the launcher
        # refuses a second client — the page must not offer what it would refuse.
        markup = self.render([self.session(live=True, status="busy")])
        self.assertNotIn("<a", markup)
        self.assertNotIn("?arg=", markup)
        self.assertIn("disabled", markup)

    def test_an_idle_row_keeps_its_last_activity(self):
        markup = self.render([self.session()])
        self.assertIn("idle · 1h ago · Bash: Check ttyd flags", markup)

    def test_context_tokens_are_absolute_with_no_percentage(self):
        markup = self.render([self.session(contextTokens=485341)])
        self.assertIn("485k ctx", markup)
        self.assertNotIn("%", markup)

    def test_a_small_context_is_not_rounded_away_to_zero(self):
        self.assertIn("820 ctx", self.render([self.session(contextTokens=820)]))
        self.assertIn("1.2k ctx", self.render([self.session(contextTokens=1234)]))

    def test_a_session_with_no_usage_yet_shows_no_context(self):
        self.assertNotIn("ctx", self.render([self.session(contextTokens=None)]))

    def test_entries_are_labelled_entries(self):
        self.assertIn("382 entries", self.render([self.session()]))
        self.assertIn("1 entry", self.render([self.session(messages=1)]))

    def test_a_derived_name_is_styled_apart_from_a_real_title(self):
        title = self.render([self.session()])
        prompt = self.render([self.session(name="ping", nameSource="first-prompt")])
        none = self.render([self.session(name="(untitled)", nameSource="none")])
        self.assertNotIn("untitled", title)
        self.assertIn("derived", prompt)
        self.assertIn("untitled", none)

    def test_the_project_directory_appears_only_when_they_differ(self):
        one = self.render([self.session(), self.session(id=SESSION_B)])
        self.assertNotIn("-workspace", one)
        many = self.render([
            self.session(),
            self.session(id=SESSION_B, projectDir="-workspace-other"),
        ])
        self.assertIn("-workspace-other", many)

    def test_an_empty_list_renders_nothing_and_shows_the_empty_state(self):
        out = run_in_node(
            'render([]);\n'
            'console.log(JSON.stringify([__nodes.list.innerHTML,'
            ' __nodes.empty.hidden, __nodes.count.textContent]));\n'
        )
        self.assertEqual(["", False, ""], json.loads(out))

    def test_the_header_counts_sessions_and_live_ones(self):
        rows = json.dumps([self.session(), self.session(id=SESSION_B, live=True)])
        out = run_in_node(
            "render(%s);\nconsole.log(__nodes.count.textContent);\n" % rows
        )
        self.assertEqual("2 sessions · 1 live", out.strip())

    def test_the_dom_is_left_alone_when_nothing_changed(self):
        # A five-second poll that rewrote the list every time would drop
        # keyboard focus and cancel a hover mid-read.
        rows = json.dumps([self.session()])
        out = run_in_node(
            "const rows = %s;\n"
            "render(rows);\n"
            "const first = __nodes.list.innerHTML;\n"
            "__nodes.list.innerHTML = 'UNTOUCHED';\n"
            "render(rows);\n"
            "console.log(__nodes.list.innerHTML === 'UNTOUCHED');\n" % rows
        )
        self.assertEqual("true", out.strip())

    def test_relative_times_follow_the_servers_clock_not_the_browsers(self):
        # lastActive is the container's clock; a browser an hour off would
        # otherwise report every session as an hour old, or in the future.
        out = run_in_node(
            "clockSkew = 3600;\n"
            "console.log(ago(Math.floor(Date.now() / 1000)));\n"
        )
        self.assertEqual("1h ago", out.strip())


class NoContentHeaderTest(ServerTestCase):
    """A 204 is bodyless by definition, so it carries no body headers.

    RFC 7230 §3.3.2: a server MUST NOT send Content-Length in a 204. Browsers
    and urllib both cope, but an intermediary is entitled not to.
    """

    def test_the_delete_204_carries_no_content_length(self):
        write_transcript(self.home, SESSION_A, [ai_title("Doomed")])
        response = self.fetch("/api/sessions/%s/delete" % SESSION_A, method="POST")
        self.assertEqual(204, response.status)
        self.assertIsNone(response.headers["Content-Length"])
        self.assertEqual(b"", response.body)

    def test_a_response_with_a_body_still_declares_its_length(self):
        response = self.fetch("/healthz", auth=False)
        self.assertEqual("3", response.headers["Content-Length"])

    def test_the_connection_survives_a_204(self):
        # No Content-Length on a keep-alive connection is only safe because 204
        # implies no body; if that ever stopped being true the next request on
        # the same socket would hang or read the wrong bytes.
        write_transcript(self.home, SESSION_A, [ai_title("Doomed")])
        self.assertEqual(204, self.fetch("/api/sessions/%s/delete" % SESSION_A,
                                         method="POST").status)
        self.assertEqual([], self.rows())


class LoggingConfigurationTest(unittest.TestCase):
    """main() has to install a handler, or its one log line is unusable.

    The redacted delete failure is the only thing this process ever logs. With
    no handler configured it goes out through logging's lastResort: bare text on
    stderr, no timestamp, no level, nothing to line it up with the ttyd output
    around it in `docker compose logs`.
    """

    def setUp(self):
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        self.addCleanup(setattr, root, "level", level)
        self.addCleanup(root.handlers.extend, handlers)
        self.addCleanup(root.handlers.clear)
        root.handlers.clear()
        self.root = root

    def test_a_handler_is_installed(self):
        sessions.configure_logging()
        self.assertTrue(self.root.handlers)

    def test_the_format_carries_a_timestamp_the_level_and_the_logger(self):
        sessions.configure_logging()
        record = logging.LogRecord(
            "sessions", logging.ERROR, __file__, 1, "delete failed", None, None
        )
        line = self.root.handlers[0].format(record)
        self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertIn("ERROR", line)
        self.assertIn("sessions", line)
        self.assertIn("delete failed", line)

    def test_main_configures_logging_before_it_serves(self):
        # Ordering matters: a failure during startup has to be logged the same
        # way as one during a request.
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        stop = RuntimeError("far enough")
        with mock.patch.dict(os.environ, {"CLAUDE_HOME": home}, clear=False):
            with mock.patch.object(sessions, "ThreadingHTTPServer", side_effect=stop):
                with self.assertRaises(RuntimeError):
                    sessions.main()
        self.assertTrue(self.root.handlers)


if __name__ == "__main__":
    unittest.main()
