"""The NPort web-console client: the login handshake and the page parsers.

The HTML in this file is trimmed straight from an NPort W2250A running
firmware 2.2 Build 18082311, and :class:`FakeConsole` implements the same
GoAhead login dance the real device does - challenge, md5(user + password
+ challenge), CSRF token, ChallID cookie - so the client is tested end to
end without a device on the bench.
"""

from __future__ import annotations

import hashlib
import http.server
import re
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moxaserial.nport_console import (  # noqa: E402
    ConsoleError,
    NPortConsole,
    parse_device_info,
    parse_opmode,
    parse_serial_port_settings,
)

CHALLENGE = "52470aabe1d300a501dc4e5e3faf7364f9cb52754ca4aa8ae8d7f330a928b316"
TOKEN = "VL4Zt9esbp2CzIJv"

LOGIN_TEMPLATE = """<HTML><HEAD><TITLE>Login</TITLE>
<SCRIPT language="javaScript">
function SetCookie(){
	document.all.FakeChallenge.value = "%(challenge)s";
	document.all.UserName.value = document.login.UserName_sel.value;
}
</SCRIPT></HEAD><Body>
<Script>
	if ("%(reason)s" == "exceed")
	{
		document.write("Maximun number of login user has been reached<BR>Please try again later!");
	}
	else if ("%(reason)s" == "expired")
	{
		document.write("Webpage has expired!");
	}
	else if ("%(reason)s" == "password")
	{
		document.write("Wrong account or password!");
	}
</Script>
<table class=table1>
	<tr>
		<td width="5%%"></td>
		<td width="13%%"><li>Model</li></td>
		<td width="20%%">-&nbsp;NPort W2250A</td>
		<td width="13%%"><li>IP</li></td>
		<td width="20%%">-&nbsp;192.168.2.210</td>
		<td width="13%%"><li>MAC Address</li></td>
		<td width="16%%">-&nbsp;40:2C:F4:FD:49:33</td>
	</tr>
	<tr>
		<td></td>
		<td><li>Name</li></td>
		<td>-&nbsp;KIA_Lathe</td>
		<td><li>Serial No.</li></td>
		<td>-&nbsp;9645</td>
		<td><li>Firmware</li></td>
		<td>-&nbsp;2.2 Build 18082311</td>
	</tr>
	<tr>
		<td></td>
		<td><li>Location</li></td>
		<td colspan='5'>-&nbsp;</td>
	</tr>
</table>
<FORM name="login" method="POST" action="/goform/webLogin" onsubmit="SetCookie();">
	<INPUT type="username" name="UserName_sel" size="20">
	<INPUT type="password" name="Passwd" size="20">
	<Input type="image" name="Loginin" src="image/login.gif">
	<Input type=hidden name="UserName">
	<Input type=hidden name="FakeChallenge">
</FORM>
<script type="text/javascript">setToken('%(token)s');</script></BODY></HTML>
"""


def login_page(reason: str = "") -> str:
    """The login page as the device serves it. *reason* is what the NPort
    substitutes when it bounces an attempt back: exceed / expired / password."""
    return LOGIN_TEMPLATE % {"challenge": CHALLENGE, "token": TOKEN, "reason": reason}


LOGIN_PAGE = login_page()

SERIAL_PORT_SET_PAGE = """<html><head><title>Serial Port Settings</title></head><body>
<form name="moniterserialport" method="POST" target="mid" >
<table width="95%" style="margin-left: 5%;" >
<tbody>
<tr >
<td style="text-align: center;" width="5%" class="block_title" rowspan="2">Port</td>
<td style="text-align: center;" width="12%" class="block_title" rowspan="2">Baud Rate</td>
<td style="text-align: center;" width="12%" class="block_title" rowspan="2">Data Bits</td>
<td style="text-align: center;" width="12%" class="block_title" rowspan="2">Stop Bits</td>
<td style="text-align: center;" width="9%" class="block_title" rowspan="2">Parity</td>
<td style="text-align: center;" width="18%" class="block_title" colspan="2">Flow Control</td>
<td style="text-align: center;" width="12%" class="block_title" rowspan="2">FIFO</td>
<td style="text-align: center;" width="20%" class="block_title" rowspan="2">Interface</td>
</tr>
<tr >
<td style="text-align: center;" width="9%" class="block_title">RTS/CTS</td>
<td style="text-align: center;" width="9%" class="block_title">XON/XOFF</td>
</tr>
<tr >
<td style="text-align: center;" nowrap>1</td>
<td nowrap>4800</td>
<td nowrap>7</td>
<td nowrap>1</td>
<td nowrap>Even</td>
<td nowrap>OFF</td>
<td nowrap>OFF</td>
<td nowrap>Enable</td>
<td nowrap>RS-232</td>
</tr>
<tr >
<td style="text-align: center;" nowrap>2</td>
<td nowrap>115200</td>
<td nowrap>8</td>
<td nowrap>2</td>
<td nowrap>None</td>
<td nowrap>ON</td>
<td nowrap>ON</td>
<td nowrap>Enable</td>
<td nowrap>RS-422</td>
</tr>
</form>
<script type="text/javascript">setToken('50GocGRJ1cszF8VD');</script></BODY></HTML>
"""

OPMODE_PAGE = """<html><head><title>OpMode</title>
	<SCRIPT languae="JavaScript">
	function init()
	{
		var opmode = %d;
		var querystring= "Port=%s";
		if( opmode == 256 ) {
			document.location = "OpmodeDriver.asp?" + querystring;
		}
    }
	</SCRIPT>
</Head><Body onload="init()"></Body></Html>
"""


class TestParsers:
    def test_device_info_from_the_unauthenticated_login_page(self):
        info = parse_device_info(LOGIN_PAGE)
        assert info["model"] == "NPort W2250A"
        assert info["ip"] == "192.168.2.210"
        assert info["mac"] == "40:2c:f4:fd:49:33"
        assert info["name"] == "KIA_Lathe"
        assert info["serial_number"] == 9645
        assert info["firmware"] == "2.2 Build 18082311"
        assert info["location"] == ""

    def test_device_info_of_a_non_nport_page_is_empty_not_fatal(self):
        assert parse_device_info("<html><body>hello</body></html>") == {}

    def test_serial_port_settings(self):
        ports = parse_serial_port_settings(SERIAL_PORT_SET_PAGE)
        assert len(ports) == 2
        first, second = ports
        assert first == {
            "port": 1, "baud": 4800, "data_bits": 7, "stop_bits": "1", "parity": "even",
            "flow_control": "none", "rtscts": False, "xonxoff": False,
            "fifo": "Enable", "interface": "RS-232", "summary": "4800 7E1",
        }
        assert second["baud"] == 115200
        assert second["flow_control"] == "both"
        assert second["summary"] == "115200 8N2"

    def test_the_two_row_header_is_not_mistaken_for_a_port(self):
        ports = parse_serial_port_settings(SERIAL_PORT_SET_PAGE)
        assert [p["port"] for p in ports] == [1, 2]

    def test_settings_page_without_rows_is_empty(self):
        assert parse_serial_port_settings("<html><table></table></html>") == []

    @pytest.mark.parametrize(
        "code, label",
        [(0, "Disabled"), (256, "Real COM"), (512, "TCP Server"), (769,
         "Pair Connection Slave"), (1536, "Reverse Terminal")],
    )
    def test_opmode_codes(self, code, label):
        assert parse_opmode(OPMODE_PAGE % (code, "01")) == {"code": code, "label": label}

    def test_an_unknown_opmode_still_reports_its_number(self):
        assert parse_opmode(OPMODE_PAGE % (9999, "01"))["label"] == "Unknown (9999)"

    def test_a_page_without_an_opmode_raises(self):
        with pytest.raises(ConsoleError, match="operation mode"):
            parse_opmode("<html></html>")


# ---------------------------------------------------------------------------
# A fake GoAhead console
# ---------------------------------------------------------------------------
class FakeConsole:
    """The device half of the login handshake, on a loopback port."""

    def __init__(self, username: str = "admin", password: str = "", max_users: int = 4):
        self.username, self.password = username, password
        self.max_users = max_users
        self.logins = 0
        self.requests: list[str] = []
        self.last_fields: dict[str, str] = {}
        console = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):  # keep pytest output clean
                pass

            def _send(self, body: str, status: int = 200, headers=()):
                raw = body.encode("latin-1")
                self.send_response(status)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(raw)))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802 - http.server's naming
                console.requests.append(self.path)
                path = self.path.split("?")[0]
                authed = "ChallID=session" in (self.headers.get("Cookie") or "")
                if path == "/login.asp":
                    reason = "expired" if "expired" in self.path else ""
                    self._send(
                        login_page(reason),
                        headers=[("Set-Cookie", "ChallID=00000; Path=/; HttpOnly")],
                    )
                    return
                if not authed:
                    self._send("", 302, [("Location", "/login.asp?expired")])
                    return
                if path == "/index.asp":
                    self._send("<HTML><FRAMESET></FRAMESET></HTML>")
                    return
                if path == "/MonitorSerialPortSet.asp":
                    self._send(SERIAL_PORT_SET_PAGE)
                    return
                if path == "/opmode.asp":
                    query = urllib.parse.parse_qs(self.path.partition("?")[2])
                    self._send(OPMODE_PAGE % (256, query.get("Port", ["01"])[0]))
                    return
                self._send("not found", 404)

            def do_POST(self):  # noqa: N802
                console.requests.append(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                fields = {
                    k: v[0]
                    for k, v in urllib.parse.parse_qs(
                        self.rfile.read(length).decode("latin-1")
                    ).items()
                }
                console.last_fields = fields
                if self.path != "/goform/webLogin":
                    self._send("not found", 404)
                    return
                if console.logins >= console.max_users:
                    self._send(login_page("exceed"), 200)
                    return
                expected = hashlib.md5(
                    (console.username + console.password + CHALLENGE).encode()
                ).hexdigest()
                ok = (
                    fields.get("UserName_sel") == console.username
                    and fields.get("Passwd") == expected
                    and fields.get("FakeChallenge") == CHALLENGE
                )
                if not ok:
                    self._send(login_page("password"), 200)
                    return
                console.logins += 1
                self._send("<html>ok</html>", 302,
                           [("Location", "/index.asp"),
                            ("Set-Cookie", "ChallID=session; Path=/; HttpOnly")])

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> FakeConsole:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


class TestLogin:
    def test_device_info_needs_no_credentials(self):
        with FakeConsole() as fake, NPortConsole(fake.host) as console:
            info = console.device_info()
        assert info["model"] == "NPort W2250A"
        assert fake.logins == 0

    def test_login_with_an_empty_password(self):
        with FakeConsole(password="") as fake, NPortConsole(fake.host, "admin", "") as console:
            console.login()
            assert console.logged_in
        assert fake.logins == 1

    def test_login_sends_exactly_the_fields_the_browser_sends(self):
        with FakeConsole() as fake, NPortConsole(fake.host, "admin", "") as console:
            console.login()
        fields = fake.last_fields
        assert set(fields) >= {
            "UserName_sel", "Passwd", "UserName", "FakeChallenge",
            "Loginin.x", "Loginin.y", "token_text",
        }
        assert fields["UserName"] == "admin"
        assert fields["token_text"] == TOKEN
        assert re.fullmatch(r"[0-9a-f]{32}", fields["Passwd"])

    def test_the_password_is_md5_of_user_password_and_challenge(self):
        with FakeConsole(password="s3cret") as fake, \
                NPortConsole(fake.host, "admin", "s3cret") as console:
            console.login()
        assert fake.last_fields["Passwd"] == hashlib.md5(
            ("admin" + "s3cret" + CHALLENGE).encode()
        ).hexdigest()

    def test_the_plain_password_is_never_put_on_the_wire(self):
        with FakeConsole(password="s3cret") as fake, \
                NPortConsole(fake.host, "admin", "s3cret") as console:
            console.login()
        assert "s3cret" not in "".join(fake.last_fields.values())

    def test_a_wrong_password_raises(self):
        with FakeConsole(password="right") as fake, \
                NPortConsole(fake.host, "admin", "wrong") as console, \
                pytest.raises(ConsoleError, match="rejected the account"):
            console.login()

    def test_too_many_sessions_says_so(self):
        with FakeConsole(max_users=0) as fake, \
                NPortConsole(fake.host, "admin", "") as console, \
                pytest.raises(ConsoleError, match="maximum number"):
            console.login()

    def test_an_unreachable_device_raises_rather_than_hanging(self):
        console = NPortConsole("127.0.0.1:1", timeout=1.0)
        with pytest.raises(ConsoleError, match="Could not reach"):
            console.device_info()

    def test_no_host_is_refused(self):
        with pytest.raises(ConsoleError):
            NPortConsole("")

    def test_close_forgets_the_session_and_the_password(self):
        with FakeConsole() as fake:
            console = NPortConsole(fake.host, "admin", "")
            console.login()
            console.close()
            assert console.logged_in is False
            assert console._password == ""


class TestAuthenticatedReads:
    def test_serial_port_settings_map(self):
        with FakeConsole() as fake, NPortConsole(fake.host, "admin", "") as console:
            console.login()
            settings = console.port_settings_map()
        assert sorted(settings) == [1, 2]
        assert settings[1]["baud"] == 4800

    def test_opmode_map_asks_for_each_port(self):
        with FakeConsole() as fake, NPortConsole(fake.host, "admin", "") as console:
            console.login()
            modes = console.opmode_map(2)
        assert modes == {1: "Real COM", 2: "Real COM"}
        assert "/opmode.asp?Port=01" in fake.requests
        assert "/opmode.asp?Port=02" in fake.requests

    def test_reads_without_a_login_are_reported_not_parsed(self):
        with FakeConsole() as fake, NPortConsole(fake.host, "admin", "") as console:
            # The device bounces to login.asp; the settings table is
            # simply not there, so the map comes back empty.
            assert console.port_settings_map() == {}

    def test_read_port_details_helper(self):
        from moxaserial import nport_console

        with FakeConsole() as fake:
            settings, modes = nport_console.read_port_details(fake.host, "admin", "", 2)
        assert settings[1]["summary"] == "4800 7E1"
        assert modes[1] == "Real COM"

    def test_device_info_helper(self):
        from moxaserial import nport_console

        with FakeConsole() as fake:
            assert nport_console.device_info(fake.host)["name"] == "KIA_Lathe"
