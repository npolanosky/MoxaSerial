"""A read-only client for the NPort's GoAhead web console.

Port probing can tell you that a serial port answers and what its modem
lines are doing, but not what baud rate the device is configured for, or
which operation mode it is in. The web console knows both, so this module
logs into it the way the browser does and scrapes the two pages that
matter.

The login handshake (verified against an NPort W2250A, firmware 2.2)
-------------------------------------------------------------------
1. ``GET /login.asp``. The page sets a ``ChallID`` cookie and contains
   two values the form needs::

       document.all.FakeChallenge.value = "<64 hex chars>";
       ...
       <script>setToken('<16 chars>')</script>

   ``setToken`` (from ``valid.js``) appends a hidden ``token_text`` field
   to every form on the page - the console's CSRF token.

2. ``POST /goform/webLogin`` with

   =================  ======================================================
   ``UserName_sel``   the account, e.g. ``admin``
   ``Passwd``         ``md5(username + password + FakeChallenge)``, hex
   ``UserName``       the account again (the page copies it across)
   ``FakeChallenge``  the value from step 1
   ``Loginin.x/y``    the coordinates of the image submit button
   ``token_text``     the ``setToken`` value
   =================  ======================================================

   A successful login replaces the ``ChallID`` cookie with a session id
   and redirects to ``/index.asp``; a failure lands back on
   ``/login.asp``, which is how :meth:`NPortConsole.login` tells them
   apart.

The device *also* serves ``/login.asp`` without any authentication at
all, and that page carries the model, name, serial number, firmware and
both MAC addresses - which is why :func:`device_info` needs no password
and discovery can use it to put a real model name on a found device.

Only the standard library is used, and nothing here ever writes to the
device: every request is a ``GET`` except the login ``POST``.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from moxaserial.discovery import OPMODES
from moxaserial.log import get_logger

log = get_logger("console")

DEFAULT_TIMEOUT = 8.0
#: The console is a single-threaded GoAhead server on a small embedded
#: CPU; over Wi-Fi the first connection after an idle spell sometimes just
#: never completes. One retry turns that into a non-event.
RETRIES = 1
USER_AGENT = "MoxaSerial/1.0"

_CHALLENGE_RE = re.compile(r'FakeChallenge\.value\s*=\s*"([0-9a-fA-F]+)"')
_TOKEN_RE = re.compile(r"setToken\('([^']*)'\)")
_OPMODE_RE = re.compile(r"var\s+opmode\s*=\s*(\d+)\s*;")
_INFO_ROW_RE = re.compile(
    r"<li>\s*([^<]+?)\s*</li>\s*</td>\s*<td[^>]*>\s*-(?:&nbsp;|\s)*([^<]*?)\s*</td>",
    re.I | re.S,
)
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.I | re.S)
_CELL_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")

#: ``login.asp`` label -> the key we hand back.
_INFO_KEYS = {
    "model": "model",
    "ip": "ip",
    "mac address": "mac",
    "name": "name",
    "serial no.": "serial_number",
    "firmware": "firmware",
    "location": "location",
}

#: Column order of the ``MonitorSerialPortSet.asp`` table.
_PORT_COLUMNS = (
    "port", "baud", "data_bits", "stop_bits", "parity",
    "rtscts", "xonxoff", "fifo", "interface",
)


class ConsoleError(Exception):
    """The web console could not be reached, or refused the login."""


def _text(html: str) -> str:
    return _TAG_RE.sub("", html).replace("&nbsp;", " ").strip()


class NPortConsole:
    """One short-lived session against one device's web console.

    Use it as a context manager so the cookie jar is dropped promptly -
    the console allows only a handful of simultaneous logins and expires
    idle ones on its own schedule::

        with NPortConsole("192.168.1.100", "admin", "") as console:
            console.login()
            ports = console.serial_port_settings()
    """

    def __init__(
        self,
        host: str,
        username: str = "admin",
        password: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        scheme: str = "http",
    ) -> None:
        self.host = str(host or "").strip()
        if not self.host:
            raise ConsoleError("No host given for the NPort web console.")
        self.username = str(username or "")
        # Held only for the lifetime of this object, never logged.
        self._password = str(password or "")
        self.timeout = float(timeout)
        self.base = f"{scheme}://{self.host}"
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )
        self.logged_in = False

    # -- context manager -------------------------------------------------
    def __enter__(self) -> NPortConsole:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Forget the session. The console has no logout endpoint; it
        expires the session itself once the cookie stops being used."""
        self._jar.clear()
        self._password = ""
        self.logged_in = False

    # -- plumbing --------------------------------------------------------
    def _open(
        self, request: urllib.request.Request, what: str, retries: int = RETRIES
    ) -> tuple[str, str]:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    return response.read().decode("latin-1"), response.geturl()
            except urllib.error.HTTPError as exc:
                raise ConsoleError(f"{what} returned HTTP {exc.code}.") from exc
            except (urllib.error.URLError, OSError) as exc:
                last = exc
                if attempt < retries:
                    log.debug("Retrying %s on %s after %s", what, self.host, exc)
        raise ConsoleError(
            f"Could not reach the web console at {self.host} - {last}."
        ) from last

    def _get(self, path: str) -> str:
        url = self.base + ("" if path.startswith("/") else "/") + path
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        return self._open(request, path)[0]

    def _post(self, path: str, fields: list[tuple[str, str]]) -> tuple[str, str]:
        url = self.base + path
        body = urllib.parse.urlencode(fields).encode("ascii")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": self.base + "/login.asp",
                "User-Agent": USER_AGENT,
            },
        )
        # Never retried: a login POST that timed out while reading the
        # reply may well have succeeded, and repeating it burns one of the
        # device's few simultaneous-login slots.
        return self._open(request, path, retries=0)

    # -- unauthenticated -------------------------------------------------
    def device_info(self) -> dict[str, Any]:
        """Model / name / MAC / serial / firmware from the login page.

        No credentials needed - the NPort prints this above the login form.
        """
        return parse_device_info(self._get("/login.asp"))

    # -- login -----------------------------------------------------------
    def login(self) -> None:
        """Authenticate. Raises :class:`ConsoleError` when refused."""
        page = self._get("/login.asp")
        challenge = _CHALLENGE_RE.search(page)
        if not challenge:
            raise ConsoleError(
                "The login page did not contain a challenge - this may not be an "
                "NPort web console."
            )
        token = _TOKEN_RE.search(page)
        digest = hashlib.md5(  # noqa: S324 - the device's scheme, not ours
            (self.username + self._password + challenge.group(1)).encode("latin-1")
        ).hexdigest()
        fields = [
            ("UserName_sel", self.username),
            ("Passwd", digest),
            ("UserName", self.username),
            ("FakeChallenge", challenge.group(1)),
            ("Loginin.x", "30"),
            ("Loginin.y", "10"),
        ]
        if token:
            fields.append(("token_text", token.group(1)))
        body, final_url = self._post("/goform/webLogin", fields)
        if "login.asp" in final_url or "webLogin" in final_url:
            raise ConsoleError(_login_failure(body))
        self.logged_in = True
        log.info("Logged in to the NPort web console at %s as '%s'.", self.host, self.username)

    # -- authenticated reads ---------------------------------------------
    def serial_port_settings(self) -> list[dict[str, Any]]:
        """One dict per serial port from ``MonitorSerialPortSet.asp``."""
        return parse_serial_port_settings(self._get("/MonitorSerialPortSet.asp"))

    def port_opmode(self, port_index: int) -> dict[str, Any]:
        """Operation mode of one port from ``opmode.asp?Port=NN``."""
        return parse_opmode(self._get(f"/opmode.asp?Port={int(port_index):02d}"))

    def port_settings_map(self) -> dict[int, dict[str, Any]]:
        """``{port_index: settings}``, empty when the page cannot be read."""
        try:
            rows = self.serial_port_settings()
        except ConsoleError as exc:
            log.debug("Could not read the serial port settings from %s: %s", self.host, exc)
            return {}
        return {int(row["port"]): row for row in rows if row.get("port")}

    def opmode_map(self, port_count: int) -> dict[int, str]:
        """``{port_index: "Real COM"}`` for the first *port_count* ports."""
        out: dict[int, str] = {}
        for index in range(1, max(1, port_count) + 1):
            try:
                out[index] = self.port_opmode(index)["label"]
            except (ConsoleError, KeyError) as exc:
                log.debug("Could not read opmode for port %d on %s: %s", index, self.host, exc)
        return out


# ---------------------------------------------------------------------------
# Parsers - separate from the transport so tests can feed them saved pages
# ---------------------------------------------------------------------------
#: ``login.asp`` always ships the three-way test below; the device
#: substitutes the reason for the empty string when it bounces you back::
#:
#:     if ("exceed" == "exceed") { ... }
_REASON_RE = re.compile(r'if\s*\(\s*"(\w*)"\s*==\s*"exceed"\s*\)')

_REASONS = {
    "exceed": (
        "The NPort has reached its maximum number of logged-in users. "
        "Log out of the web console and try again."
    ),
    "expired": "The NPort web session expired before the login completed.",
    "password": "The NPort rejected the account or password.",
}


def _login_failure(body: str) -> str:
    match = _REASON_RE.search(body)
    if match:
        return _REASONS.get(match.group(1), "The NPort rejected the account or password.")
    return "The NPort rejected the account or password."


def parse_device_info(html: str) -> dict[str, Any]:
    """Model / IP / MAC / name / serial / firmware from ``login.asp``."""
    out: dict[str, Any] = {}
    for label, value in _INFO_ROW_RE.findall(html):
        key = _INFO_KEYS.get(label.strip().lower())
        if key:
            out[key] = value.strip()
    if out.get("serial_number", "").isdigit():
        out["serial_number"] = int(out["serial_number"])
    if out.get("mac"):
        out["mac"] = out["mac"].lower()
    return out


def parse_serial_port_settings(html: str) -> list[dict[str, Any]]:
    """Rows of ``MonitorSerialPortSet.asp`` -> a list of port dicts.

    The table has a two-row header (Flow Control spans two columns) and
    then one row per port, so rows are selected by shape: nine cells whose
    first is a port number.
    """
    out: list[dict[str, Any]] = []
    for row_html in _ROW_RE.findall(html):
        cells = [_text(c) for c in _CELL_RE.findall(row_html)]
        if len(cells) != len(_PORT_COLUMNS) or not cells[0].isdigit():
            continue
        row = dict(zip(_PORT_COLUMNS, cells, strict=True))
        port: dict[str, Any] = {
            "port": int(row["port"]),
            "baud": _int_or(row["baud"]),
            "data_bits": _int_or(row["data_bits"]),
            "stop_bits": row["stop_bits"] or "1",
            "parity": row["parity"].lower() or "none",
            "flow_control": _flow_control(row["rtscts"], row["xonxoff"]),
            "rtscts": row["rtscts"].upper() == "ON",
            "xonxoff": row["xonxoff"].upper() == "ON",
            "fifo": row["fifo"],
            "interface": row["interface"],
        }
        port["summary"] = (
            f"{port['baud'] or '?'} {port['data_bits'] or '?'}"
            f"{(port['parity'][:1] or 'n').upper()}{port['stop_bits']}"
        )
        out.append(port)
    return out


def _int_or(value: str, fallback: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _flow_control(rtscts: str, xonxoff: str) -> str:
    hw = rtscts.strip().upper() == "ON"
    sw = xonxoff.strip().upper() == "ON"
    if hw and sw:
        return "both"
    if hw:
        return "rtscts"
    if sw:
        return "xonxoff"
    return "none"


def parse_opmode(html: str) -> dict[str, Any]:
    """``opmode.asp`` -> ``{"code": 256, "label": "Real COM"}``.

    The page is a redirector: it carries the numeric mode in a JavaScript
    variable and then bounces the browser to the matching settings page.
    """
    match = _OPMODE_RE.search(html)
    if not match:
        raise ConsoleError("Could not read the operation mode from the console page.")
    code = int(match.group(1))
    return {"code": code, "label": OPMODES.get(code, f"Unknown ({code})")}


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------
def device_info(host: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Model / name / firmware for *host* without any credentials."""
    with NPortConsole(host, timeout=timeout) as console:
        return console.device_info()


def read_port_details(
    host: str,
    username: str,
    password: str,
    port_count: int = 2,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[dict[int, dict[str, Any]], dict[int, str]]:
    """``(settings_by_port, opmode_label_by_port)``; both may be empty.

    Used by port probing when credentials are stored: the line settings
    let the ``PORT_INIT`` probe echo back what the device already has
    instead of imposing new ones.
    """
    with NPortConsole(host, username, password, timeout=timeout) as console:
        console.login()
        return console.port_settings_map(), console.opmode_map(port_count)
