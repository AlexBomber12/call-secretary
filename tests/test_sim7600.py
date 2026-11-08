from collections import deque

import pytest

from call_secretary.modem import ModemError
from call_secretary.sim7600 import ATChannel, CallControl, CommandRejected, parse_call


class Port:
    """Scripted serial endpoint; never opens a device."""

    def __init__(self, responses):
        self.responses = deque(responses)
        self.reads = deque()
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)
        self.reads.extend(self.responses.popleft())
        return len(data)

    def read(self, size):
        assert size == 4096
        return self.reads.popleft() if self.reads else b""

    def close(self):
        self.closed = True


def snapshot(state=4, number="+39000000123", identity=1):
    return f'+CLCC: {identity},1,{state},0,0,"{number}",145\r\n'.encode()


def test_transactions_preserve_fragmented_events_between_response_lines():
    port = Port([[b"AT+CL", b"CC\r\nRI", b"NG\r", b"\n", snapshot(), b"OK\r\n"]])
    link = ATChannel(port)
    assert len(link.execute("AT+CLCC")) == 1
    assert link.events() == ["RING"]
    assert link.events() == []


def test_rejected_commands_do_not_make_future_commands_indeterminate():
    port = Port([[b"+CME ERROR: 10\r\n"], [b"OK\r\n"]])
    link = ATChannel(port)
    with pytest.raises(CommandRejected):
        link.execute("ATA")
    assert link.execute("AT") == ()
    assert not link.closed


def test_timeout_closes_channel_and_cannot_be_followed_by_another_command():
    ticks = iter([0, 0, 4])
    port = Port([[]])
    link = ATChannel(port, clock=lambda: next(ticks))
    with pytest.raises(ModemError, match="indeterminate"):
        link.execute("ATA", timeout=3)
    assert port.closed
    with pytest.raises(ModemError, match="closed"):
        link.execute("AT")
    assert port.writes == [b"ATA\r"]


def test_call_is_answered_and_ended_only_with_its_current_token():
    port = Port(
        [
            [b"OK\r\n"],
            [snapshot(), b"OK\r\n"],
            [b"RING\r\n", snapshot(), b"OK\r\n"],
            [snapshot(), b"OK\r\n"],
            [b"VOICE CALL: BEGIN\r\nOK\r\n"],
            [snapshot(0), b"OK\r\n"],
            [snapshot(0), b"OK\r\n"],
            [b"VOICE CALL:END: 000017\r\nOK\r\n"],
            [b"OK\r\n"],
        ]
    )
    calls = CallControl(ATChannel(port))
    incoming = calls.poll()
    assert calls.poll().token == incoming.token
    # Repeated RING did not create a new logical call.
    calls.answer(incoming.token)
    assert calls.poll().state == 0
    assert calls.hangup(incoming.token) is None
    assert port.writes.count(b"ATA\r") == 1
    assert port.writes.count(b"AT+CHUP\r") == 1
    assert incoming.incoming("dedicated").original_called_number is None


def test_parser_does_not_expose_caller_number_in_repr_or_route_from_it():
    call = parse_call('+CLCC: 1,1,4,0,0,"39000000123",145,"Synthetic, caller"')
    assert call.number == "+39000000123"
    assert "39000000123" not in repr(call)


@pytest.mark.parametrize(
    "line",
    [
        "RING",
        "+CLCC: 1,1,4,0,0,123",
        "+CLCC: 0,1,4,0,0",
        "+CLCC: 1,2,4,0,0",
        "+CLCC: 1,1,7,0,0",
        "+CLCC: 1,1,4,8,0",
        "+CLCC: 1,1,4,0,2",
        '+CLCC: 1,1,4,0,0,"unterminated',
        '+CLCC: 1,1,4,0,0,"123",256',
        '+CLCC: 1,1,4,0,0,"123",bad',
    ],
)
def test_parser_rejects_ambiguous_or_malformed_metadata_without_echoing_it(line):
    with pytest.raises(ModemError, match="Malformed") as err:
        parse_call(line)
    assert line not in str(err.value)


@pytest.mark.parametrize("suffix", ["", ',"anonymous",128', ',"１２３",145'])
def test_missing_or_unreliable_caller_is_not_invented(suffix):
    assert parse_call("+CLCC: 1,1,4,0,0" + suffix).number is None


@pytest.mark.parametrize(
    "command,timeout", [("ATD123;", 3), ("AT", 0), ("AT", 121), ("AT", float("nan"))]
)
def test_outgoing_commands_and_invalid_deadlines_never_write(command, timeout):
    port = Port([])
    link = ATChannel(port)
    with pytest.raises(ModemError, match="Unsupported"):
        link.execute(command, timeout=timeout)
    assert not port.writes and not link.closed


def test_close_is_idempotent_and_sanitizes_backend_errors():
    class BrokenClose(Port):
        def close(self):
            raise OSError("synthetic private device path")

    link = ATChannel(BrokenClose([]))
    with pytest.raises(ModemError, match="Cannot close") as err:
        link.close()
    assert "private" not in str(err.value)
    link.close()


@pytest.mark.parametrize(
    "chunks",
    [
        [b"x" * 4097],
        [b"x" * 1025 + b"\r"],
        [b"x" * 4096, b"\r\n"],
        [b"RING\r\n" * 33],
        [b" " * 1000 + b"\r\n"] * 17,
        [b"\xff\r\n"],
    ],
    ids=["chunk", "line", "partial-line", "events", "total", "encoding"],
)
def test_malformed_or_flooding_input_closes_the_channel(chunks):
    port = Port([chunks])
    link = ATChannel(port)
    with pytest.raises(ModemError, match="indeterminate"):
        link.execute("AT")
    assert link.closed and port.closed


def test_short_write_and_disconnect_close_even_if_cleanup_fails():
    class Broken(Port):
        def write(self, data):
            return 0

        def close(self):
            raise OSError("private close failure")

    link = ATChannel(Broken([]))
    with pytest.raises(ModemError, match="indeterminate"):
        link.execute("AT")
    assert link.closed

    class Disconnected(Port):
        def read(self, size):
            raise OSError("private serial failure")

    link = ATChannel(Disconnected([[]]))
    with pytest.raises(ModemError, match="indeterminate") as err:
        link.execute("AT")
    assert "private" not in str(err.value) and link.closed


def test_operator_interrupt_closes_the_channel_and_propagates():
    class Interrupted(Port):
        def read(self, size):
            raise KeyboardInterrupt

    port = Interrupted([[]])
    with pytest.raises(KeyboardInterrupt):
        ATChannel(port).execute("AT")
    assert port.closed


def test_no_carrier_rejects_answer_but_is_an_event_during_hangup():
    port = Port([[b"NO CARRIER\r\n"], [b"NO CARRIER\r\nOK\r\n"]])
    link = ATChannel(port)
    with pytest.raises(CommandRejected):
        link.execute("ATA")
    assert link.execute("AT+CHUP") == ()
    assert link.events() == ["NO CARRIER", "NO CARRIER"]


def test_channel_has_one_call_controller_and_failed_initialization_closes_it():
    channel = ATChannel(Port([[b"OK\r\n"]]))
    CallControl(channel)
    with pytest.raises(ModemError, match="already owns"):
        CallControl(channel)
    port = Port([[b"ERROR\r\n"]])
    with pytest.raises(CommandRejected):
        CallControl(ATChannel(port))
    assert port.closed


@pytest.mark.parametrize(
    "rows",
    [
        [snapshot(), snapshot(identity=2)],
        [b"+CLCC: 1,0,2,0,0\r\n"],
        [b"+CLCC: 1,1,4,1,0\r\n"],
        [b"+CLCC: 1,1,4,0,1\r\n"],
        [snapshot(state=5)],
        [snapshot(state=0)],
    ],
    ids=["second-call", "outgoing", "data", "conference", "waiting", "unowned-active"],
)
def test_unsupported_or_existing_calls_are_never_adopted(rows):
    port = Port([[b"OK\r\n"], [*rows, b"OK\r\n"]])
    calls = CallControl(ATChannel(port))
    with pytest.raises(ModemError):
        calls.poll()
    assert b"ATA\r" not in port.writes and b"AT+CHUP\r" not in port.writes


@pytest.mark.parametrize("ended", [True, False])
def test_stale_tokens_cannot_answer_a_replacement_or_ended_call(ended):
    next_call = [] if ended else [snapshot()]
    port = Port([[b"OK\r\n"], [snapshot(), b"OK\r\n"], [*next_call, b"OK\r\n"]])
    calls = CallControl(ATChannel(port))
    call = calls.poll()
    with pytest.raises(ModemError, match="Stale"):
        calls.answer(call.token if ended else "not-the-current-token")
    assert b"ATA\r" not in port.writes


@pytest.mark.parametrize("state", [0, 4])
def test_answer_is_not_sent_twice_even_if_state_transition_is_delayed(state):
    port = Port(
        [
            [b"OK\r\n"],
            [snapshot(), b"OK\r\n"],
            [snapshot(), b"OK\r\n"],
            [b"OK\r\n"],
            [snapshot(state), b"OK\r\n"],
        ]
    )
    calls = CallControl(ATChannel(port))
    token = calls.poll().token
    calls.answer(token)
    with pytest.raises(ModemError, match="already answered"):
        calls.answer(token)
    assert port.writes.count(b"ATA\r") == 1


def test_late_end_hint_does_not_end_an_active_call_and_reused_id_gets_new_token():
    port = Port(
        [
            [b"OK\r\n"],
            [snapshot(), b"OK\r\n"],
            [snapshot(), b"OK\r\n"],
            [b"OK\r\n"],
            [b"NO CARRIER\r\n", snapshot(0), b"OK\r\n"],
            [snapshot(), b"OK\r\n"],
            [b"VOICE CALL:END: 000017\r\n", snapshot(), b"OK\r\n"],
        ]
    )
    calls = CallControl(ATChannel(port))
    first = calls.poll()
    calls.answer(first.token)
    assert calls.poll().token == first.token
    second = calls.poll()
    assert second.token != first.token
    third = calls.poll()
    assert third.token != second.token


def test_a_changed_active_modem_id_is_not_adopted(tmp_path):
    port = Port(
        [
            [b"OK\r\n"],
            [snapshot(), b"OK\r\n"],
            [snapshot(), b"OK\r\n"],
            [b"OK\r\n"],
            [snapshot(0, identity=2), b"OK\r\n"],
        ]
    )
    calls = CallControl(ATChannel(port))
    token = calls.poll().token
    calls.answer(token)
    with pytest.raises(ModemError, match="Will not adopt"):
        calls.poll()


def test_changed_incoming_caller_invalidates_the_old_token():
    port = Port(
        [
            [b"OK\r\n"],
            [snapshot(), b"OK\r\n"],
            [snapshot(number="+39000000456"), b"OK\r\n"],
        ]
    )
    calls = CallControl(ATChannel(port))
    first = calls.poll()
    with pytest.raises(ModemError, match="Stale"):
        calls.hangup(first.token)
    assert b"AT+CHUP\r" not in port.writes


def test_changed_active_caller_is_not_treated_as_the_owned_call():
    port = Port(
        [
            [b"OK\r\n"],
            [snapshot(), b"OK\r\n"],
            [snapshot(), b"OK\r\n"],
            [b"OK\r\n"],
            [snapshot(0, number="+39000000456"), b"OK\r\n"],
        ]
    )
    calls = CallControl(ATChannel(port))
    calls.answer(calls.poll().token)
    with pytest.raises(ModemError, match="Will not adopt"):
        calls.poll()
