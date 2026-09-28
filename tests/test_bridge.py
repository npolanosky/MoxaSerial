"""The action router shared by the Fusion palette and the dev server."""

from __future__ import annotations

from pathlib import Path

import pytest

from moxaserial.bridge import Bridge, Host
from moxaserial.config import ConfigStore, default_machine
from moxaserial.dnc.sender import SendState
from tests.conftest import wait_until

PROGRAM = "%\nO0001 (BRIDGE TEST)\nN10 G0 X1\nN20 M30\n%\n"


class RecordingHost(Host):
    name = "test"

    def __init__(self, file_path: str = "") -> None:
        self.toasts: list[tuple[str, str]] = []
        self.file_path = file_path
        self.revealed: list[str] = []

    def pick_file(self, title: str = "", initial_dir: str = "", extensions: str = "") -> str:
        self.browse_calls = getattr(self, "browse_calls", []) + [(initial_dir, extensions)]
        return self.file_path

    def last_posted_file(self) -> dict:
        if not self.file_path:
            return {"path": "", "source": "none", "error": "nothing posted", "candidates": []}
        return {"path": self.file_path, "source": "nc-program", "candidates": []}

    def toast(self, message: str, level: str = "info", title: str = "MoxaSerial") -> None:
        self.toasts.append((level, message))

    def reveal(self, path: str) -> bool:
        self.revealed.append(path)
        return True


@pytest.fixture
def program(tmp_path):
    path = tmp_path / "O0001.nc"
    path.write_text(PROGRAM, encoding="ascii")
    return str(path)


@pytest.fixture
def bridge(bus, tmp_path, program):
    store = ConfigStore(tmp_path / "settings.json")
    sim = store.machine("simulator")
    sim["receive"]["folder"] = str(tmp_path / "in")
    sim["send"]["wait_for_ready"] = "immediate"
    store.upsert_machine(sim)
    host = RecordingHost(program)
    return Bridge(bus=bus, store=store, host=host)


def ok(reply: dict) -> dict:
    assert reply["ok"], reply.get("error")
    return reply["data"]


# -- dispatch ---------------------------------------------------------------

def test_unknown_action_is_rejected_not_raised(bridge):
    reply = bridge.handle("no.such.action")
    assert reply["ok"] is False
    assert "Unknown action" in reply["error"]


def test_handle_json_returns_a_json_string(bridge):
    import json

    parsed = json.loads(bridge.handle_json("state.get"))
    assert parsed["ok"] is True


def test_payload_accepts_a_json_string(bridge):
    data = ok(bridge.handle("machines.new", '{"name": "From JSON"}'))
    assert data["machine"]["name"] == "From JSON"


def test_ui_ready_returns_the_whole_world(bridge):
    data = ok(bridge.handle("ui.ready"))
    assert set(data) >= {"version", "settings", "machines", "enums", "send", "receive", "log"}
    assert data["enums"]["bauds"]
    assert data["activeMachineId"]


# -- machines ---------------------------------------------------------------

def test_machine_crud_round_trip(bridge):
    made = ok(bridge.handle("machines.new", {"name": "Lathe"}))["machine"]
    made["host"] = "192.0.2.5"
    saved = ok(bridge.handle("machines.save", {"machine": made}))
    assert saved["machine"]["host"] == "192.0.2.5"
    assert any(m["id"] == saved["machine"]["id"] for m in saved["machines"])

    dup = ok(bridge.handle("machines.duplicate", {"id": saved["machine"]["id"]}))["machine"]
    assert dup["id"] != saved["machine"]["id"]

    removed = ok(bridge.handle("machines.delete", {"id": dup["id"]}))
    assert removed["removed"] is True


def test_saving_an_invalid_machine_returns_an_error(bridge):
    reply = bridge.handle("machines.save", {"machine": {"name": "", "type": "moxa"}})
    assert reply["ok"] is False
    assert "name" in reply["error"].lower()


def test_deleting_the_simulator_is_refused(bridge):
    reply = bridge.handle("machines.delete", {"id": "simulator"})
    assert reply["ok"] is False


def test_set_default_machine(bridge):
    made = ok(bridge.handle("machines.save", {"machine": default_machine("Mill")}))["machine"]
    data = ok(bridge.handle("machines.setDefault", {"id": made["id"]}))
    assert data["settings"]["default_machine_id"] == made["id"]


def test_machines_test_opens_and_closes_a_transport(bridge):
    data = ok(bridge.handle("machines.test", {"id": "simulator", "sync": True}))
    assert data["ok"] is True
    assert data["info"]["kind"] == "simulator"
    assert "elapsed_ms" in data["info"]


def test_machines_test_runs_off_the_ui_thread_and_pushes_the_result(bridge):
    import time

    pushed = []
    bridge.subscribe_outbound(lambda action, payload: pushed.append((action, payload)))
    data = ok(bridge.handle("machines.test", {"id": "simulator"}))
    assert data["started"] is True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(a == "machines.testResult" for a, _ in pushed):
        time.sleep(0.02)
    results = [p for a, p in pushed if a == "machines.testResult"]
    assert results and results[0]["ok"] is True and results[0]["machineId"] == "simulator"


# -- settings ---------------------------------------------------------------

def test_settings_save_updates_the_log_level(bridge):
    ok(bridge.handle("settings.save", {"settings": {"log_level": "WARNING"}}))
    assert bridge.logs.level == "WARNING"


def test_theme_set_persists(bridge):
    data = ok(bridge.handle("theme.set", {"theme": "light"}))
    assert data["settings"]["theme"] == "light"


# -- files ------------------------------------------------------------------

def test_last_post_returns_a_described_file(bridge, program):
    data = ok(bridge.handle("file.lastPost"))
    assert data["file"]["path"] == program
    assert data["file"]["name"] == "O0001.nc"
    assert data["file"]["size"] > 0


def test_last_post_with_nothing_posted_reports_why(bus, tmp_path):
    store = ConfigStore(tmp_path / "s.json")
    b = Bridge(bus=bus, store=store, host=RecordingHost(""))
    data = ok(b.handle("file.lastPost"))
    assert data["file"] == {}
    assert data["error"] == "nothing posted"


def test_browse_cancelled_is_not_an_error(bus, tmp_path):
    b = Bridge(bus=bus, store=ConfigStore(tmp_path / "s.json"), host=RecordingHost(""))
    data = ok(b.handle("file.browse"))
    assert data["cancelled"] is True


def test_preview_returns_processed_lines(bridge, program):
    data = ok(bridge.handle("file.preview", {"path": program, "machineId": "simulator"}))
    assert data["lines"][0] == "%"
    assert "O0001 (BRIDGE TEST)" in data["lines"]
    assert data["lineCount"] == len(data["lines"])
    assert data["byteCount"] > 0


def test_preview_without_a_file_is_an_error(bridge):
    reply = bridge.handle("file.preview", {"path": "/nope.nc"})
    assert reply["ok"] is False


def test_reveal_delegates_to_the_host(bridge, program):
    ok(bridge.handle("file.reveal", {"path": program}))
    assert bridge.host.revealed == [program]


# -- send -------------------------------------------------------------------

def test_send_start_runs_to_completion_and_toasts(bridge, program):
    data = ok(bridge.handle("send.start", {"machineId": "simulator", "path": program}))
    assert data["started"] is True
    assert wait_until(lambda: not bridge.sender.is_running, 15.0)
    assert bridge.sender.state is SendState.DONE
    assert any(level == "success" for level, _ in bridge.host.toasts)


def test_send_start_records_the_machine_as_last_used(bridge, program):
    ok(bridge.handle("send.start", {"machineId": "simulator", "path": program}))
    assert wait_until(lambda: not bridge.sender.is_running, 15.0)
    assert bridge.store.get("last_used_machine_id") == "simulator"


def test_send_start_with_no_file_is_rejected(bus, tmp_path):
    b = Bridge(bus=bus, store=ConfigStore(tmp_path / "s.json"), host=RecordingHost(""))
    reply = b.handle("send.start", {"machineId": "simulator"})
    assert reply["ok"] is False
    assert "No file selected" in reply["error"]


def test_send_start_with_a_missing_file_is_rejected(bridge):
    reply = bridge.handle("send.start", {"machineId": "simulator", "path": "/gone.nc"})
    assert reply["ok"] is False
    assert "not found" in reply["error"]


def test_send_start_with_an_unknown_machine_is_rejected(bridge, program):
    reply = bridge.handle("send.start", {"machineId": "ghost", "path": program})
    assert reply["ok"] is False


def test_use_last_post_flag_resolves_the_path(bridge, program):
    data = ok(bridge.handle("send.start", {"machineId": "simulator", "useLastPost": True}))
    assert data["path"] == program
    assert wait_until(lambda: not bridge.sender.is_running, 15.0)


def test_resend_without_a_prior_send_is_rejected(bridge):
    reply = bridge.handle("send.resend")
    assert reply["ok"] is False


def test_pause_resume_stop_are_always_answerable(bridge):
    for action in ("send.pause", "send.resume", "send.stop"):
        assert bridge.handle(action)["ok"] is True


# -- receive ----------------------------------------------------------------

def test_receive_start_and_stop(bridge):
    data = ok(bridge.handle("receive.start", {"machineId": "simulator"}))
    assert data["started"] is True
    assert wait_until(lambda: bridge.receiver.is_running, 5.0)
    assert bridge.handle("receive.start", {"machineId": "simulator"})["ok"] is False
    ok(bridge.handle("receive.stop"))
    assert wait_until(lambda: not bridge.receiver.is_running, 10.0)


def test_overwrite_response_with_a_stale_token_is_reported(bridge):
    data = ok(bridge.handle("receive.overwriteResponse", {"token": "x", "decision": "rename"}))
    assert data["accepted"] is False


# -- log / about ------------------------------------------------------------

def test_log_list_filters_and_counts(bridge):
    from moxaserial.log import get_logger

    get_logger("unit").warning("a test warning")
    data = ok(bridge.handle("log.list", {"level": "WARNING"}))
    assert any("a test warning" in e["message"] for e in data["entries"])
    assert data["counts"]["WARNING"] >= 1


def test_log_clear_empties_the_ring(bridge):
    ok(bridge.handle("log.clear"))
    # clear() itself logs one line, so the ring holds at most that.
    assert len(bridge.logs.ring.records()) <= 1


def test_about_reports_the_protocol_status(bridge):
    data = ok(bridge.handle("about.get"))
    assert data["protocol"]["implemented"] is True
    assert Path(data["paths"]["settings"]).name == "settings.json"


# -- outbound push ----------------------------------------------------------

def test_engine_events_are_pushed_to_the_ui(bridge, program):
    pushed: list[tuple[str, dict]] = []
    bridge.subscribe_outbound(lambda action, payload: pushed.append((action, payload)))

    ok(bridge.handle("send.start", {"machineId": "simulator", "path": program}))
    assert wait_until(lambda: not bridge.sender.is_running, 15.0)

    actions = [a for a, _ in pushed]
    assert "send.state" in actions
    assert "send.progress" in actions
    assert "send.done" in actions
    assert "toast" in actions


def test_a_failing_outbound_sink_cannot_break_the_bridge(bridge):
    bridge.subscribe_outbound(lambda a, p: (_ for _ in ()).throw(RuntimeError("sink died")))
    good: list[str] = []
    bridge.subscribe_outbound(lambda a, p: good.append(a))
    bridge.push("test", {})
    assert good == ["test"]


def test_unsubscribe_stops_the_push(bridge):
    seen: list[str] = []
    unsub = bridge.subscribe_outbound(lambda a, p: seen.append(a))
    bridge.push("one", {})
    unsub()
    bridge.push("two", {})
    assert seen == ["one"]


def test_shutdown_is_idempotent(bridge):
    bridge.shutdown()
    bridge.shutdown()


# -- auto-update -------------------------------------------

def test_about_carries_the_update_status(bridge):
    data = ok(bridge.handle("about.get"))
    assert "update" in data
    status = data["update"]
    assert status["settings"]["repo"] == "npolanosky/MoxaSerial"
    assert status["settings"]["auto_check"] is True
    assert status["settings"]["auto_install"] is False
    assert status["current"]
    assert isinstance(status["developmentInstall"], bool)


def test_update_status_action(bridge):
    assert ok(bridge.handle("update.status"))["settings"]["check_interval_hours"] == 24


def test_update_check_is_async_by_default(bridge, monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(bridge.updates, "check_async", lambda force=True: calls.append(force))
    assert ok(bridge.handle("update.check", {})) == {"started": True}
    assert calls == [True]


def test_update_check_sync_returns_the_result(bridge, monkeypatch):
    from moxaserial import update as update_mod

    monkeypatch.setattr(
        update_mod,
        "check_for_update",
        lambda **kw: {"available": True, "latest": "9.9.9", "checked": 1.0, "error": ""},
    )
    data = ok(bridge.handle("update.check", {"sync": True}))
    assert data["latest"] == "9.9.9"
    # The result is remembered for the About page, not just returned.
    assert bridge.store.get("update")["last_seen_version"] == "9.9.9"


def test_update_check_pushes_the_result(bridge, monkeypatch):
    from moxaserial import update as update_mod

    seen: list[tuple[str, dict]] = []
    bridge.subscribe_outbound(lambda a, p: seen.append((a, p)))
    monkeypatch.setattr(
        update_mod,
        "check_for_update",
        lambda **kw: {
            "available": True, "latest": "9.9.9", "current": "0.1.0",
            "checked": 1.0, "error": "",
        },
    )
    bridge.handle("update.check", {"sync": True})
    actions = [a for a, _ in seen]
    assert "update.checked" in actions and "update.available" in actions


def test_update_install_starts_a_worker(bridge, monkeypatch):
    seen: list = []

    def fake(release=None):
        seen.append(release)
        return {"started": True}

    monkeypatch.setattr(bridge.updates, "install_async", fake)
    assert ok(bridge.handle("update.install", {}))["started"] is True
    assert seen == [None]


def test_update_install_refuses_a_second_run(bridge):
    bridge.updates._busy = True
    data = ok(bridge.handle("update.install", {}))
    assert data["started"] is False
    assert "already in progress" in data["error"]


def test_update_settings_save_is_partial(bridge):
    ok(bridge.handle("settings.save", {"settings": {"update": {"repo": "acme/thing"}}}))
    ok(bridge.handle("settings.save", {"settings": {"update": {"auto_install": True}}}))
    cfg = bridge.store.get("update")
    assert cfg["repo"] == "acme/thing"
    assert cfg["auto_install"] is True
    assert cfg["auto_check"] is True  # untouched by either save


def test_update_settings_reject_a_nonsense_repo(bridge):
    ok(bridge.handle("settings.save", {"settings": {"update": {"repo": "not-a-repo"}}}))
    assert bridge.store.get("update")["repo"] == "npolanosky/MoxaSerial"


def test_update_settings_accept_a_pasted_github_url(bridge):
    ok(bridge.handle(
        "settings.save", {"settings": {"update": {"repo": "https://github.com/acme/thing.git"}}}
    ))
    assert bridge.store.get("update")["repo"] == "acme/thing"


def test_the_default_host_cannot_restart_the_addin():
    host = Host()
    assert host.restart_addin() is False
    assert host.addin_dir() == ""


def test_machines_test_accepts_an_unsaved_machine_form(bridge):
    """GitHub issue #3: Test on a brand-new profile failed with "No machine
    with id" because the form had never been saved."""
    form = ok(bridge.handle("machines.new", {"name": "Fresh"}))["machine"]
    assert bridge.store.machine(form["id"]) is None
    form["type"] = "simulator"
    data = ok(bridge.handle("machines.test", {"id": form["id"], "machine": form, "sync": True}))
    assert data["ok"] is True and data["info"]["kind"] == "simulator"


def test_machines_test_rejects_an_invalid_form(bridge):
    form = ok(bridge.handle("machines.new", {"name": "Bad"}))["machine"]
    form["host"] = ""
    reply = bridge.handle("machines.test", {"id": form["id"], "machine": form, "sync": True})
    assert reply["ok"] is False and "Host" in reply["error"]


def test_machines_test_ignores_unrelated_form_errors(bridge):
    """A bad receive/send field must not stop the operator checking the link."""
    form = ok(bridge.handle("machines.new", {"name": "Odd"}))["machine"]
    form["type"] = "simulator"
    form["send"]["line_ending"] = "CUSTOM"
    form["send"]["line_ending_custom"] = ""
    data = ok(bridge.handle("machines.test", {"id": form["id"], "machine": form, "sync": True}))
    assert data["ok"] is True


# -- settings import/export ---------------------------------------------------

def test_settings_export_and_import_round_trip(bridge, tmp_path):
    m = ok(bridge.handle("machines.new", {"name": "Exported"}))["machine"]
    m["host"] = "192.0.2.9"
    ok(bridge.handle("machines.save", {"machine": m}))
    out = tmp_path / "all.json"
    data = ok(bridge.handle("settings.export", {"scope": "all", "path": str(out)}))
    assert data["path"] == str(out) and "Exported" in data["machines"]
    assert out.exists()

    preview = ok(bridge.handle("settings.importPreview", {"path": str(out)}))
    assert preview["scope"] == "all" and "Exported" in preview["machines"]

    # Change it locally, then bring the export back: merge restores the host.
    m["host"] = "192.0.2.1"
    ok(bridge.handle("machines.save", {"machine": m}))
    dry = ok(bridge.handle("settings.import", {"path": str(out), "mode": "merge", "dryRun": True}))
    assert dry["applied"] is False and dry["report"]["updated"] == ["Exported"]
    assert bridge.store.machine(m["id"])["host"] == "192.0.2.1"
    data = ok(bridge.handle("settings.import", {"path": str(out), "mode": "merge"}))
    assert data["applied"] is True and data["state"]["machines"]
    assert bridge.store.machine(m["id"])["host"] == "192.0.2.9"


def test_settings_export_one_machine_adds_json_suffix(bridge, tmp_path):
    out = tmp_path / "sim"
    data = ok(bridge.handle("settings.export", {"scope": "machine", "machineId": "simulator", "path": str(out)}))
    assert data["path"].endswith(".json") and data["machines"] == ["Simulator"]


def test_settings_export_cancelled_is_not_an_error(bridge):
    data = ok(bridge.handle("settings.export", {"scope": "all"}))
    assert data["cancelled"] is True
    data = ok(bridge.handle("settings.importPreview", {}))
    assert data["cancelled"] is True


def test_settings_import_accepts_a_bom_prefixed_file(bridge, tmp_path):
    out = tmp_path / "bom.json"
    ok(bridge.handle("settings.export", {"scope": "machine", "machineId": "simulator", "path": str(out)}))
    out.write_bytes(b"\xef\xbb\xbf" + out.read_bytes())
    data = ok(bridge.handle("settings.importPreview", {"path": str(out)}))
    assert data["machines"] == ["Simulator"]


def test_settings_export_refuses_to_clobber_via_added_suffix(bridge, tmp_path):
    (tmp_path / "x.json").write_text("{}", encoding="utf-8")
    reply = bridge.handle("settings.export", {"scope": "all", "path": str(tmp_path / "x")})
    assert reply["ok"] is False and "already exists" in reply["error"]
    assert (tmp_path / "x.json").read_text() == "{}"


def test_settings_import_rejects_a_non_export(bridge, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    reply = bridge.handle("settings.importPreview", {"path": str(bad)})
    assert reply["ok"] is False and "MoxaSerial settings file" in reply["error"]
