import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_modem import Port

from call_secretary import locking, modem
from call_secretary.modem import ModemError


@pytest.mark.parametrize("response", [b"ERROR\r\n", b"+CME ERROR: 10\r\n", b"x" * 16385])
def test_bad_modem_responses_are_bounded(response):
    with pytest.raises(ModemError):
        modem.at_query(Port([response]), "AT")


def test_inventory_does_not_open_a_port(monkeypatch):
    from serial.tools import list_ports

    monkeypatch.setattr(
        list_ports,
        "comports",
        lambda: [SimpleNamespace(device="fake", vid=1, pid=2, description="synthetic")],
    )
    assert modem.inventory() == [{"device": "fake", "vid": 1, "pid": 2, "description": "synthetic"}]


def test_explicit_windows_serial_port_and_sanitized_open_error(monkeypatch):
    import serial

    monkeypatch.setattr(modem, "os", SimpleNamespace(name="nt"))
    with pytest.raises(ModemError, match="COM"):
        modem.check_device("auto")
    modem.check_device("COM12")
    fake = Mock(return_value="opened")
    monkeypatch.setattr(serial, "Serial", fake)
    assert modem.open_serial("COM12") == "opened"
    assert fake.call_args.kwargs == {"baudrate": 115200, "timeout": 0.1, "write_timeout": 1}
    fake.side_effect = serial.SerialException("secret device detail")
    with pytest.raises(ModemError, match="exclusively") as err:
        modem.open_serial("COM12")
    assert "secret" not in str(err.value)


def test_linux_requires_stable_char_device_and_known_ownership(monkeypatch):
    monkeypatch.setattr(modem, "os", SimpleNamespace(name="posix"))
    path = Mock()
    path.__str__ = Mock(return_value="/dev/serial/by-id/synthetic")
    path.is_symlink.return_value = True
    path.resolve.return_value = path
    path.is_char_device.return_value = True
    monkeypatch.setattr(modem, "Path", lambda value: path)
    monkeypatch.setattr(modem.shutil, "which", lambda value: "/usr/bin/fuser")
    query = Mock(return_value=SimpleNamespace(returncode=1))
    monkeypatch.setattr(modem.subprocess, "run", query)
    modem.check_device("synthetic")
    for code in (0, 2):
        query.return_value = SimpleNamespace(returncode=code)
        with pytest.raises(ModemError):
            modem.check_device("synthetic")
    monkeypatch.setattr(modem.shutil, "which", lambda value: None)
    with pytest.raises(ModemError, match="psmisc"):
        modem.check_device("synthetic")
    path.is_char_device.return_value = False
    with pytest.raises(ModemError, match="character"):
        modem.check_device("synthetic")
    path.is_symlink.return_value = False
    with pytest.raises(ModemError, match="stable"):
        modem.check_device("synthetic")
    monkeypatch.setattr(modem, "Path", Path)
    with pytest.raises(ModemError, match="stable"):
        modem.check_device("/dev/ttyUSB0")


def test_linux_open_is_exclusive_and_inspection_always_closes(monkeypatch):
    import serial

    monkeypatch.setattr(modem, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(modem, "check_device", lambda device: None)
    port = Mock()
    serial_open = Mock(return_value=port)
    monkeypatch.setattr(serial, "Serial", serial_open)
    monkeypatch.setattr(modem, "at_query", lambda port, command: [command])
    assert modem.inspect("synthetic")["H1"] == "not_tested"
    assert serial_open.call_args.kwargs["exclusive"] is True
    port.close.assert_called_once()
    monkeypatch.setattr(modem, "at_query", Mock(side_effect=ModemError("failed")))
    with pytest.raises(ModemError):
        modem.inspect("synthetic")
    assert port.close.call_count == 2


def test_posix_lock_and_repeated_release_with_fake_system_module(tmp_path, monkeypatch):
    flock = Mock()
    monkeypatch.setattr(locking, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(flock=flock, LOCK_EX=2, LOCK_NB=4))
    lock = locking.InstanceLock(tmp_path / "lock")
    with lock:
        flock.assert_called_once()
    lock.__exit__()
    flock.side_effect = OSError("busy")
    with pytest.raises(RuntimeError, match="Another"):
        lock.__enter__()


def test_windows_lock_initializes_the_lock_byte_once_on_either_platform(tmp_path, monkeypatch):
    acquire = Mock()
    monkeypatch.setattr(locking, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(locking=acquire, LK_NBLCK=2))
    path = tmp_path / "lock"
    for _ in range(2):
        with locking.InstanceLock(path):
            assert path.read_bytes() == b"0"
    assert acquire.call_count == 2
    acquire.side_effect = OSError("busy")
    with pytest.raises(RuntimeError, match="Another"):
        locking.InstanceLock(path).__enter__()
