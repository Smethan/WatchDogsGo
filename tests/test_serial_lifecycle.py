"""SerialManager close/read behavior against real POSIX PTYs."""
import os
import select
import threading
from types import SimpleNamespace

import pytest
import serial

from watchdogs.serial_manager import SerialManager


@pytest.mark.skipif(not __import__('sys').platform.startswith('linux'), reason="Linux cdc_acm")
def test_native_jtag_reopen_never_applies_rts_only_state(monkeypatch):
    """Model cdc_acm asserting both lines; exercise actual pyserial PTY opens."""
    import fcntl
    import struct
    import termios
    from watchdogs import serial_manager as sm

    master_fd, slave_fd = os.openpty()
    path = os.ttyname(slave_fd)
    os.close(slave_fd)
    monkeypatch.setattr(sm.serial.tools.list_ports, 'comports', lambda: [
        SimpleNamespace(device=path, vid=0x303A, pid=0x1001)])
    original_ioctl = fcntl.ioctl
    pair = termios.TIOCM_DTR | termios.TIOCM_RTS
    state = pair
    transitions = []

    def cdc_ioctl(fd, command, argument, *args):
        nonlocal state
        if command in (termios.TIOCMBIC, termios.TIOCMBIS, termios.TIOCMSET):
            bits = struct.unpack('I', argument)[0]
            if command == termios.TIOCMBIC:
                state &= ~bits
            elif command == termios.TIOCMBIS:
                state |= bits
            else:
                state = bits
            transitions.append(state & pair)
            return 0
        return original_ioctl(fd, command, argument, *args)

    monkeypatch.setattr(fcntl, 'ioctl', cdc_ioctl)
    manager = SerialManager(path)
    try:
        for _ in range(3):
            state = pair  # Linux asserts both when opening cdc_acm.
            manager.setup()
            assert isinstance(manager.serial_conn, sm._NativeJTAGSerial)
            os.write(master_fd, b'capability reply\r\n')
            assert select.select([manager.fd], [], [], 1)[0]
            assert manager.read_available() == ['capability reply']
            manager.close()
        assert transitions and set(transitions) == {0}
        assert termios.TIOCM_RTS not in transitions
    finally:
        manager.close()
        os.close(master_fd)


@pytest.mark.parametrize('identity,platform', [
    ((0x10C4, 0xEA60), 'linux'),  # External USB-UART bridge.
    ((0x303A, 0x4001), 'linux'),  # USB CDC without Serial/JTAG.
    ((0x303A, 0x1001), 'darwin'),
    ((None, None), 'linux'),
])
def test_other_endpoints_keep_standard_serial_open(monkeypatch, identity, platform):
    from unittest.mock import Mock
    from watchdogs import serial_manager as sm

    path = '/dev/test-serial'
    monkeypatch.setattr(sm.os.path, 'exists', lambda _path: True)
    monkeypatch.setattr(sm.os, 'access', lambda *_args: True)
    monkeypatch.setattr(sm.sys, 'platform', platform)
    monkeypatch.setattr(sm.serial.tools.list_ports, 'comports', lambda: [
        SimpleNamespace(device=path, vid=identity[0], pid=identity[1])])
    standard, native = Mock(), Mock()
    monkeypatch.setattr(sm.serial, 'Serial', standard)
    monkeypatch.setattr(sm, '_NativeJTAGSerial', native)
    manager = SerialManager(path)
    manager.setup()
    standard.return_value.open.assert_called_once_with()
    native.assert_not_called()
    assert standard.return_value.dtr is False
    assert standard.return_value.rts is False


@pytest.fixture
def pty_manager():
    master_fd, slave_fd = os.openpty()
    slave = os.ttyname(slave_fd)
    os.close(slave_fd)
    manager = SerialManager(slave)
    manager.serial_conn = serial.Serial(slave, timeout=0, write_timeout=0.2)
    try:
        yield manager, master_fd
    finally:
        manager.close()
        os.close(master_fd)


def test_read_available_reads_pty_and_closed_manager_does_not_touch_handle(pty_manager):
    manager, master_fd = pty_manager
    os.write(master_fd, b"line one\r\n")
    assert select.select([manager.serial_conn.fileno()], [], [], 1)[0]
    assert manager.read_available() == ["line one"]

    manager.close()
    assert manager.read_available() == []


def test_fd_none_from_open_pyserial_transport_is_a_disconnect(pty_manager):
    manager, _master_fd = pty_manager
    conn = manager.serial_conn
    original_fd = conn.fd
    try:
        # Model pyserial's POSIX object surviving while its transport fd has
        # already been invalidated by USB reset/disconnect.
        conn.fd = None
        with pytest.raises(ConnectionError, match="file descriptor"):
            manager.read_available()
    finally:
        conn.fd = original_fd


def test_unrelated_type_error_from_open_transport_is_not_hidden(pty_manager):
    manager, _master_fd = pty_manager
    conn = manager.serial_conn

    class BrokenReader:
        is_open = True
        fd = 123

        @property
        def in_waiting(self):
            raise TypeError("unrelated reader bug")

    manager.serial_conn = BrokenReader()
    with pytest.raises(TypeError, match="unrelated reader bug"):
        manager.read_available()
    manager.serial_conn = conn


def test_close_waits_for_inflight_pty_read(pty_manager):
    manager, _master_fd = pty_manager
    conn = manager.serial_conn
    read_entered = threading.Event()
    allow_read = threading.Event()
    close_started = threading.Event()
    close_finished = threading.Event()
    errors = []

    class PausingSerial:
        @property
        def is_open(self):
            return conn.is_open

        @property
        def fd(self):
            return conn.fd

        @property
        def in_waiting(self):
            read_entered.set()
            if not allow_read.wait(2):
                raise TimeoutError("test did not release paused PTY read")
            return conn.in_waiting

        def read(self, size):
            return conn.read(size)

        def close(self):
            conn.close()

    manager.serial_conn = PausingSerial()

    def reader():
        try:
            manager.read_available()
        except Exception as exc:  # surfaced in the test thread
            errors.append(exc)

    def closer():
        close_started.set()
        manager.close()
        close_finished.set()

    read_thread = threading.Thread(target=reader)
    close_thread = threading.Thread(target=closer)
    read_thread.start()
    assert read_entered.wait(2)
    close_thread.start()
    assert close_started.wait(2)
    # close() cannot invalidate the PTY while read_available holds the lock.
    assert not close_finished.wait(0.05)
    allow_read.set()
    read_thread.join(2)
    close_thread.join(2)

    assert not read_thread.is_alive() and not close_thread.is_alive()
    assert close_finished.is_set()
    assert errors == []
    assert manager.serial_conn is None
