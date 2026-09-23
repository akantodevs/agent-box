"""The ports are named in four files; they must all name the same numbers.

Nothing in the running container can detect a disagreement here. The session
list builds its terminal links from a port it is *told* about — the container
cannot see the host side of a `ports:` mapping — so a compose file that
publishes the terminal on one port while telling the page about another
produces links to a port this box does not answer on. If some *other* agent-box
answers there, its launcher is handed a session id from this box's list and
correctly reports "no transcript for <id>": a bug that reads as data loss and
is really a typo two files away.

So the agreement is pinned here instead:

    agent-box/ep.sh          the container ports the two servers listen on
    agent-box/scripts/       the defaults sessions.py falls back to
    agent-box/Dockerfile     the healthcheck's probes
    docker-compose.yml       the published mapping, and the port the page is told

The numbers themselves are pinned too, not just their agreement. They are part
of the product's interface: every deployment's compose file names them, so
changing one is a breaking change for existing boxes and has to be a deliberate
edit to this test rather than a side effect of editing a file.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import sessions  # noqa: E402  (path set above)

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE = os.path.join(HERE, "..")
REPO = os.path.join(IMAGE, "..")

# The container-side ports, and the host ports they are published on by default.
# Host and container are deliberately the same number on both surfaces: the
# mapping then has no side to get backwards, and a `ports:` entry that has been
# copied from an older compose file (8085:8081) fails loudly — nothing listens
# on the container port it names — instead of quietly serving wrong links.
SESSION_LIST_PORT = 8090
AGENT_TABS_PORT = 8091


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as handle:
        return handle.read()


class ContainerPortTest(unittest.TestCase):
    """What the two servers listen on inside the container."""

    def setUp(self):
        self.entrypoint = read(IMAGE, "ep.sh")

    def test_ttyd_serves_the_agent_tabs_port(self):
        self.assertRegex(self.entrypoint, r"ttyd -p %d\b" % AGENT_TABS_PORT)

    def test_the_session_list_serves_its_own_port(self):
        self.assertRegex(self.entrypoint, r"SESSIONS_PORT=%d\b" % SESSION_LIST_PORT)


class DefaultTest(unittest.TestCase):
    """sessions.py's fallbacks, for when it is started with no environment.

    ep.sh always sets both, so these defaults are what a developer running the
    server by hand gets — and what the page advertises if a deployment leaves
    the variable out of its compose file. Both have to be the numbers the image
    actually uses, or the fallback is a trap rather than a convenience.
    """

    def test_it_listens_on_the_session_list_port(self):
        self.assertEqual(SESSION_LIST_PORT, sessions.DEFAULT_LISTEN_PORT)

    def test_it_advertises_the_agent_tabs_port(self):
        self.assertEqual(AGENT_TABS_PORT, sessions.DEFAULT_TABS_PUBLIC_PORT)


class HealthcheckTest(unittest.TestCase):
    """The Dockerfile probes both servers, so it has to know where they are.

    A healthcheck pointed at a port nothing listens on marks a working
    container unhealthy for as long as nobody looks — and one pointed at a port
    that answers for the *wrong* reason never fires at all.
    """

    def setUp(self):
        self.dockerfile = read(IMAGE, "Dockerfile")
        self.probed = set(int(p) for p in re.findall(r"localhost:(\d+)", self.dockerfile))

    def test_it_probes_exactly_the_two_container_ports(self):
        self.assertEqual({SESSION_LIST_PORT, AGENT_TABS_PORT}, self.probed)

    def test_the_session_list_is_probed_on_its_health_endpoint(self):
        self.assertIn("localhost:%d/healthz" % SESSION_LIST_PORT, self.dockerfile)


class ComposeTest(unittest.TestCase):
    """The published mapping and the port the page is told about.

    These two are the pair that caused the bug this file exists to prevent: the
    container cannot see the host side of a `ports:` mapping, so the page
    advertises whatever `environment:` says — and if that is not the port the
    mapping publishes, every link on the page points somewhere this box does not
    answer. They sit far enough apart in the file to be edited independently,
    which is why their agreement is asserted here rather than trusted.

    Only the agreement is pinned, not how it is spelled. A deployment may use
    plain numbers (as this repo's own compose file does, publishing the two
    servers on 8095/8096 to stay clear of another box on the defaults) or a
    `${VAR:-default}` expression in both places; either passes, as long as the
    two sides say the same thing.
    """

    # env var in the container -> container port it describes
    SURFACES = (
        ("SESSION_LIST_PUBLIC_PORT", SESSION_LIST_PORT),
        ("AGENT_TABS_PUBLIC_PORT", AGENT_TABS_PORT),
    )

    def setUp(self):
        self.compose = read(REPO, "docker-compose.yml")

    def published(self, container_port):
        """The host side of the mapping onto this container port.

        A `ports:` entry is `[<host-ip>:]<host>:<container>[/proto]`, quoted or
        bare. Only the host port is returned: an address, if one is given, says
        where the box listens, not which port the links must name. The host port
        is either a bare value or a `${VAR:-default}` expression, which contains
        a colon of its own and so is matched as a whole.
        """
        found = re.findall(
            r'^\s*-\s*"?(?:\d+(?:\.\d+){3}:)?(\$\{[^}]*\}|[^":\s]+):%d(?:/\w+)?"?\s*(?:#.*)?$'
            % container_port,
            self.compose,
            re.MULTILINE,
        )
        self.assertEqual(
            1, len(found),
            "expected exactly one published mapping onto container port %d, found %r"
            % (container_port, found),
        )
        return found[0]

    def advertised(self, env_var):
        """The value handed to the container as this environment variable."""
        found = re.findall(
            r'^\s*%s:\s*"?([^"\s#]+)"?\s*(?:#.*)?$' % env_var,
            self.compose,
            re.MULTILINE,
        )
        self.assertEqual(
            1, len(found),
            "expected exactly one %s entry, found %r" % (env_var, found),
        )
        return found[0]

    def test_each_surface_publishes_and_advertises_one_value(self):
        for env_var, container_port in self.SURFACES:
            with self.subTest(surface=env_var):
                self.assertEqual(
                    self.published(container_port), self.advertised(env_var),
                    "the host port published onto %d and the %s the page is told "
                    "about must be the same value" % (container_port, env_var),
                )

    def test_the_two_surfaces_do_not_share_a_host_port(self):
        """One port cannot serve both, and the collision is silent: compose
        would start the box, and one of the two servers would be unreachable."""
        hosts = [self.published(container_port) for _, container_port in self.SURFACES]
        self.assertEqual(len(hosts), len(set(hosts)), hosts)

    def test_the_retired_variables_are_gone(self):
        # They were removed rather than aliased, so a compose file still setting
        # one is silently ignored. Nothing in the image may quietly resurrect
        # them: the README's upgrade note is the only place they belong.
        for retired in ("TTYD_PUBLIC_PORT", "ADMIN_PUBLIC_PORT"):
            with self.subTest(variable=retired):
                self.assertNotIn(retired, self.compose)
                self.assertNotIn(retired, read(IMAGE, "ep.sh"))
                self.assertNotIn(retired, read(IMAGE, "scripts", "sessions.py"))
                self.assertNotIn(retired, read(IMAGE, "scripts", "sessions_page.html"))


if __name__ == "__main__":
    unittest.main()
