"""Settings store: defaults, validation, normalisation, migration, round-trip."""

from __future__ import annotations

import json

import pytest

from moxaserial import config
from moxaserial.config import (
    SCHEMA_VERSION,
    ConfigStore,
    ValidationError,
    default_machine,
    default_settings,
    migrate,
    normalize_machine,
    normalize_settings,
    validate_machine,
)

# -- defaults ---------------------------------------------------------------

def test_defaults_ship_a_simulator_machine():
    settings = default_settings()
    assert settings["schema_version"] == SCHEMA_VERSION
    assert [m["id"] for m in settings["machines"]] == ["simulator"]
    assert settings["default_machine_id"] == "simulator"


def test_default_machine_has_every_section():
    m = default_machine()
    assert set(m) >= {"id", "name", "type", "host", "serial", "send", "receive"}
    assert m["serial"]["baud"] == 9600
    assert m["send"]["line_ending"] == "CRLF"
    assert m["receive"]["overwrite"] == "ask"


# -- normalisation ----------------------------------------------------------

def test_normalize_fills_missing_sections():
    m = normalize_machine({"name": "Bare"})
    assert m["serial"]["parity"] == "none"
    assert m["send"]["chunk_size"] == 256
    assert m["receive"]["idle_timeout_s"] == 5  # CIMCO RECV_TIMEOUT


def test_normalize_drops_unknown_keys():
    m = normalize_machine({"name": "X", "bogus": 1, "serial": {"baud": 19200, "junk": 2}})
    assert "bogus" not in m
    assert "junk" not in m["serial"]
    assert m["serial"]["baud"] == 19200


@pytest.mark.parametrize(
    "field,given,expected",
    [
        ("baud", "not a number", 9600),
        ("baud", 5, 50),            # clamped to the low bound
        ("baud", 99_000_000, 3_000_000),
        ("data_bits", 9, 8),        # not a valid choice -> default
        ("parity", "EVEN", "even"), # case-insensitive choice
        ("parity", "weird", "none"),
        ("stop_bits", 2, "2"),      # coerced to the string form
        ("flow_control", "RTSCTS", "rtscts"),
        ("xon_char", 999, 255),
    ],
)
def test_normalize_serial_coercion(field, given, expected):
    m = normalize_machine({"name": "X", "serial": {field: given}})
    assert m["serial"][field] == expected


def test_normalize_booleans_accept_strings_and_numbers():
    m = normalize_machine({"name": "X", "send": {"uppercase": "no", "strip_spaces": 1}})
    assert m["send"]["uppercase"] is False
    assert m["send"]["strip_spaces"] is True


def test_normalize_derives_ports_from_the_port_index():
    m = normalize_machine({"name": "X", "port_index": 3})
    assert m["data_port"] == config.DEFAULT_DATA_PORT_BASE + 2
    assert m["cmd_port"] == config.DEFAULT_CMD_PORT_BASE + 2


def test_normalize_settings_deduplicates_machine_ids():
    raw = {"machines": [{"id": "dup", "name": "A"}, {"id": "dup", "name": "B"}]}
    out = normalize_settings(raw)
    ids = [m["id"] for m in out["machines"]]
    assert len(set(ids)) == len(ids)


def test_normalize_settings_always_restores_the_simulator():
    out = normalize_settings({"machines": [{"id": "x", "name": "Only"}]})
    assert any(m["id"] == "simulator" for m in out["machines"])


def test_normalize_settings_repairs_dangling_default_id():
    out = normalize_settings({"machines": [{"id": "a", "name": "A"}], "default_machine_id": "gone"})
    assert out["default_machine_id"] in {m["id"] for m in out["machines"]}


# -- validation -------------------------------------------------------------

def test_validate_accepts_a_good_machine():
    assert validate_machine(normalize_machine(default_machine())) == []


def test_validate_requires_a_name():
    m = normalize_machine(default_machine())
    m["name"] = "   "
    assert any("name is required" in p for p in validate_machine(m))


def test_validate_requires_a_host_for_moxa():
    m = normalize_machine({"name": "M", "type": "moxa", "host": ""})
    assert any("Host" in p for p in validate_machine(m))


def test_validate_flags_wait_for_xon_without_software_flow_control():
    m = normalize_machine(
        {"name": "M", "serial": {"flow_control": "rtscts"}, "send": {"wait_for_ready": "xon"}}
    )
    assert any("XON" in p for p in validate_machine(m))


def test_validate_flags_a_pattern_with_no_placeholder():
    m = normalize_machine({"name": "M", "receive": {"filename_pattern": "fixed.nc"}})
    assert any("placeholder" in p for p in validate_machine(m))


# -- migration --------------------------------------------------------------

def test_migrate_versionless_file_is_upgraded():
    data, notes = migrate({"machines": []})
    assert data["schema_version"] == SCHEMA_VERSION
    assert notes and "0 -> 1" in notes[0]


def test_migrate_current_version_is_a_no_op():
    data, notes = migrate({"schema_version": SCHEMA_VERSION, "machines": []})
    assert notes == []
    assert data["schema_version"] == SCHEMA_VERSION


def test_migrate_future_version_is_not_destroyed():
    data, notes = migrate({"schema_version": 999, "machines": [{"id": "z", "name": "Z"}]})
    assert notes and "newer version" in notes[0]
    assert data["machines"][0]["id"] == "z"


def test_migrate_unknown_intermediate_version_falls_back_to_defaults(monkeypatch):
    monkeypatch.setattr(config, "SCHEMA_VERSION", 5)
    monkeypatch.setattr(config, "_MIGRATIONS", {})
    data, notes = migrate({"schema_version": 2})
    assert any("falling back to defaults" in n for n in notes)
    assert "machines" in data


# -- store round-trip -------------------------------------------------------

def test_store_creates_and_reloads(tmp_path):
    path = tmp_path / "settings.json"
    store = ConfigStore(path)
    store.save()
    assert path.is_file()

    reloaded = ConfigStore(path)
    assert [m["id"] for m in reloaded.machines()] == [m["id"] for m in store.machines()]


def test_store_roundtrips_a_custom_machine(tmp_path):
    path = tmp_path / "settings.json"
    store = ConfigStore(path)
    m = default_machine("Haas VF-2")
    m["host"] = "192.0.2.7"
    m["serial"]["baud"] = 19200
    m["send"]["strip_comments"] = True
    saved = store.upsert_machine(m)

    again = ConfigStore(path).machine(saved["id"])
    assert again["name"] == "Haas VF-2"
    assert again["host"] == "192.0.2.7"
    assert again["serial"]["baud"] == 19200
    assert again["send"]["strip_comments"] is True


def test_store_rejects_an_invalid_machine(tmp_path):
    store = ConfigStore(tmp_path / "s.json")
    with pytest.raises(ValidationError):
        store.upsert_machine({"name": "", "type": "moxa"})


def test_store_will_not_delete_the_simulator(tmp_path):
    store = ConfigStore(tmp_path / "s.json")
    with pytest.raises(ValidationError):
        store.delete_machine("simulator")


def test_store_delete_and_duplicate(tmp_path):
    store = ConfigStore(tmp_path / "s.json")
    original = store.upsert_machine(default_machine("Lathe"))
    clone = store.duplicate_machine(original["id"])
    assert clone["id"] != original["id"]
    assert clone["name"] == "Lathe copy"
    assert clone["type"] == "moxa"

    second = store.duplicate_machine(original["id"])
    assert second["name"] == "Lathe copy 2"

    assert store.delete_machine(original["id"]) is True
    assert store.machine(original["id"]) is None


def test_active_machine_follows_the_selection_mode(tmp_path):
    store = ConfigStore(tmp_path / "s.json")
    other = store.upsert_machine(default_machine("Mill"))

    store.set_default_machine("simulator")
    store.note_machine_used(other["id"])
    assert store.active_machine()["id"] == "simulator"

    store.update_globals({"machine_selection": "last_used"})
    assert store.active_machine()["id"] == other["id"]


def test_update_globals_ignores_unknown_keys(tmp_path):
    store = ConfigStore(tmp_path / "s.json")
    store.update_globals({"theme": "light", "not_a_setting": 1})
    assert store.get("theme") == "light"
    assert "not_a_setting" not in store.data


def test_store_recovers_from_a_corrupt_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = ConfigStore(path)
    assert store.machines()  # defaults, not an exception
    assert path.with_suffix(".json.corrupt").exists()


def test_saved_file_is_valid_json_with_the_schema_version(tmp_path):
    path = tmp_path / "settings.json"
    store = ConfigStore(path)
    store.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == SCHEMA_VERSION
    assert isinstance(data["machines"], list)


def test_data_is_a_copy_not_a_live_reference(tmp_path):
    store = ConfigStore(tmp_path / "s.json")
    snapshot = store.data
    snapshot["machines"][0]["name"] = "mutated"
    assert store.machines()[0]["name"] != "mutated"
