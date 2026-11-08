import pytest

from call_secretary.modem import ModemError, at_query, hardware_checklist


class Port:
    def __init__(self, lines):
        self.lines = iter(lines)
        self.writes = []

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def read(self, size):
        return next(self.lines, b"")


def test_query_ignores_echo_and_stops_at_final_response():
    port = Port([b"AT+CGMM\r\n", b"SIMCOM_SIM7600E-H\r\n", b"OK\r\n"])
    assert at_query(port, "AT+CGMM") == ["SIMCOM_SIM7600E-H"]


def test_diagnostic_rejects_call_or_reset_commands_without_writing():
    for command in ["ATA", "AT+CHUP", "AT+CRESET", "AT+CCFC=0,1"]:
        port = Port([])
        with pytest.raises(ModemError):
            at_query(port, command)
        assert not port.writes


def test_query_deadline_does_not_wait_forever():
    times = iter([0, 0.1, 4])
    with pytest.raises(ModemError, match="timed out"):
        at_query(Port([b""]), "AT", clock=lambda: next(times))


def test_hardware_checklist_never_claims_acceptance_without_measurement():
    assert all(
        value == "not_tested"
        for section in hardware_checklist().values()
        for value in section.values()
    )
