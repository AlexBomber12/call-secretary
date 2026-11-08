import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from call_secretary import calls
from call_secretary.budget import BudgetError
from call_secretary.calls import CallController, simulate
from call_secretary.config import ConfigError, IngressRoute
from call_secretary.routing import Incoming, Router, canonical_number
from call_secretary.storage import Store


def test_paid_constructor_routes_and_reserves_before_api(tmp_path, family, rates):
    with Store(tmp_path / "state") as store:
        with pytest.raises(ConfigError, match="pricing"):
            CallController(store, family, Incoming("x"), paid=True)
        disabled = replace(family.profiles[0], enabled=False)
        with pytest.raises(ConfigError, match="disabled"):
            CallController(
                store,
                replace(family, profiles=(disabled,)),
                Incoming("x"),
                operator_profile=disabled.id,
            )
        routed = replace(family, routes=(IngressRoute("x", "member_a", None, True),))
        call = CallController(store, routed, Incoming("x"), paid=True, rates=rates)
        assert call.profile.id == "member_a"
        call.finish()
        call = CallController(store, family, Incoming("x"), operator_profile="member_b")
        assert call.profile.id == "member_b"
        call.finish()
        with pytest.raises(BudgetError):
            CallController(
                store,
                replace(family, monthly_budget_eur=Decimal("0.001")),
                Incoming("x"),
                paid=True,
                rates=rates,
            )
        assert (
            store.db.execute("SELECT COUNT(*) FROM calls WHERE status='budget_refused'").fetchone()[
                0
            ]
            == 1
        )


def test_usage_overrun_is_persisted_and_late_events_are_ignored(tmp_path, family, rates):
    async def scenario():
        with Store(tmp_path / "state") as store:
            call = CallController(store, family, Incoming("x"), paid=True, rates=rates)
            await call.activity(True)
            assert call.caller_speaking
            await call.text("a", "caller", "synthetic", False, 1)
            await call.interrupted("a")
            assert store.transcript(call.cid)[0]["interrupted"] == 1
            normal = {
                "input_tokens": 1,
                "output_tokens": 1,
                "input_token_details": {},
                "output_token_details": {},
            }
            await call.usage("small", normal)
            with pytest.raises(BudgetError, match="exceeded"):
                await call.usage("over", normal | {"output_tokens": 1_000_000_000})
            assert (await call.tool("finish_call", {})).finish
            assert not (await call.tool("finish_call", {"bad": True})).finish
            call.finish()
            await call.interrupted("a")
            await call.usage("late", {})

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["recipient_timeout", "silence_timeout", "speaking", "active"])
def test_monitor_uses_code_owned_silence_and_recipient_guards(
    tmp_path, family, monkeypatch, reason
):
    async def scenario():
        with Store(tmp_path / "state") as store:
            call = CallController(store, family, Incoming("x"))
            call.started -= 61
            call.last_activity -= 61
            if reason != "recipient_timeout":
                call.profile = family.profiles[0]
            if reason == "speaking":
                call.caller_speaking = True
            if reason == "active":
                await call.activity()
            voice = SimpleNamespace(finish_event=asyncio.Event(), closed=False)

            async def close():
                voice.closed = True

            async def sleep(seconds):
                if reason in {"speaking", "active"}:
                    call.finish()

            voice.close_soon = close
            monkeypatch.setattr(calls.asyncio, "sleep", sleep)
            await call.monitor(voice)
            if reason in {"recipient_timeout", "silence_timeout"}:
                assert call.stop_reason == reason and voice.finish_event.is_set()
            else:
                assert not voice.closed

    asyncio.run(scenario())


def test_empty_simulation_does_not_invent_text(tmp_path, family):
    async def scenario():
        with Store(tmp_path / "state") as store:
            cid = await simulate(store, family, None, "")
            assert store.transcript(cid) == []

    asyncio.run(scenario())


def test_router_excludes_disabled_profiles_and_normalizes_international_prefix(family):
    assert canonical_number("00 39 (000) 001-234") == "+39000001234"
    disabled = replace(family.profiles[0], enabled=False)
    router = Router(replace(family, profiles=(disabled, *family.profiles[1:])))
    assert router.spoken(disabled.owner_name).profile is None
    prompt = router.instructions(None)
    assert disabled.owner_name not in prompt
    assert len(router.tools(family.profiles[1])) == 1
    assert router.max_prompt_bytes() > 0
