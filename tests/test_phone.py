"""The phone bridge: sending over KDE Connect, finding through Find My. busctl
is faked with a small in-memory bus — nothing here reaches a real phone."""
import json
import sys

import pytest

import friday.phone as phone

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="KDE Connect over D-Bus")

IPHONE = "d572ece5_iphone"
LAPTOP = "aa11_laptop"


class FakeBus:
    """Answers busctl --json=short the way kdeconnectd does, and records every call."""

    def __init__(self):
        self.devices = {}
        self.calls = []
        self.running = True

    def add(self, device_id, name, kind="phone", reachable=True):
        self.devices[device_id] = dict(name=name, type=kind, isReachable=reachable)

    def __call__(self, argv):
        assert argv[:3] == ["busctl", "--user", "--json=short"]
        _verb, _service, path, _interface, member, *rest = argv[3:]
        if not self.running:
            raise RuntimeError("Unit dbus-org.kde.kdeconnect.service not found.")
        self.calls.append((path, member, *rest))
        if member == "devices":
            return json.dumps({"type": "as", "data": [list(self.devices)]})
        if member == "GetAll":
            device = self.devices[path.rsplit("/", 1)[1]]
            props = {k: {"type": "x", "data": v} for k, v in device.items()}
            return json.dumps({"type": "a{sv}", "data": [props]})
        return ""

    def sent(self):
        """The calls that did something to a device (not the lookups)."""
        return [c for c in self.calls if c[1] not in ("devices", "GetAll")]


@pytest.fixture
def bus(monkeypatch):
    fake = FakeBus()
    monkeypatch.setattr(phone, "_run", fake)
    monkeypatch.setattr(phone, "PHONE_NAME", "")
    opened = []
    monkeypatch.setattr(phone.plat, "open_target", opened.append)
    fake.opened = opened
    return fake


def test_find_prefers_the_connected_phone_over_other_devices(bus):
    bus.add(LAPTOP, "Work laptop", kind="laptop", reachable=True)
    bus.add(IPHONE, "shivp’s iPhone", reachable=False)
    bus.add("bb22", "Old Pixel", reachable=True)
    assert phone.device().id == "bb22"


def test_phone_name_picks_the_device(bus, monkeypatch):
    bus.add(IPHONE, "shivp’s iPhone")
    bus.add("bb22", "Old Pixel")
    monkeypatch.setattr(phone, "PHONE_NAME", "iphone")
    assert phone.device().id == IPHONE


def test_no_daemon_and_no_pairing_say_so(bus):
    assert phone.send("hi") == phone.NOT_PAIRED
    bus.running = False
    assert phone.send("hi") == phone.NOT_RUNNING


def test_find_my_phone_opens_find_my_without_kde_connect(bus):
    bus.running = False
    assert "Play Sound" in phone.find_my_phone()
    assert bus.opened == [phone.PHONE_FIND_URL]


def test_a_link_is_shared_as_a_url(bus):
    bus.add(IPHONE, "shivp’s iPhone")
    assert phone.send("https://example.com/a") == "Link sent to your phone, sir."
    assert bus.sent() == [(f"/modules/kdeconnect/devices/{IPHONE}/share", "shareUrl",
                           "s", "https://example.com/a")]


def test_a_file_inside_the_roots_is_sent(bus, tmp_path, monkeypatch):
    monkeypatch.setattr(phone, "FRIDAY_FILE_ROOTS", [tmp_path])
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF")
    bus.add(IPHONE, "shivp’s iPhone")
    assert phone.send(str(report)) == "Sending report.pdf to your phone, sir."
    assert bus.sent()[0][1:] == ("shareUrl", "s", report.as_uri())


def test_a_file_outside_the_roots_is_refused(bus, tmp_path, monkeypatch):
    monkeypatch.setattr(phone, "FRIDAY_FILE_ROOTS", [tmp_path / "allowed"])
    secret = tmp_path / "id_rsa"
    secret.write_text("key")
    bus.add(IPHONE, "shivp’s iPhone")
    assert "outside the folders" in phone.send(str(secret))
    assert bus.sent() == []


def test_a_missing_file_is_not_sent_as_a_note(bus):
    bus.add(IPHONE, "shivp’s iPhone")
    assert phone.send("/nope/missing.pdf") == "I can't find that file, sir."
    assert bus.sent() == []


def test_text_arrives_as_a_notification(bus):
    bus.add(IPHONE, "shivp’s iPhone")
    assert phone.send("buy milk") == "Sent to your phone, sir."
    assert bus.sent() == [(f"/modules/kdeconnect/devices/{IPHONE}/ping", "sendPing",
                           "s", "buy milk")]


def test_nothing_is_sent_to_a_phone_that_dropped_off(bus):
    bus.add(IPHONE, "shivp’s iPhone", reachable=False)
    assert phone.send("buy milk") == phone.OFFLINE
    assert bus.sent() == []


def test_replies_never_say_the_device_name(bus):
    bus.add(IPHONE, "shivp’s iPhone")
    assert "shivp" not in phone.send("hi")
    bus.devices[IPHONE]["isReachable"] = False
    assert "shivp" not in phone.send("hi")
