from dataclasses import replace
from pathlib import Path

import pytest

from call_secretary.config import IngressRoute, load_family
from call_secretary.routing import Incoming, Router


def family_with_routes(*routes):
    return replace(load_family(Path("config/family.example.yaml")), routes=routes)


def test_same_external_caller_and_hidden_id_route_by_original_recipient():
    router = Router(
        family_with_routes(
            IngressRoute("shared", "member_a", "+39000000101", True),
            IngressRoute("shared", "member_b", "+39000000102", True),
        )
    )
    for caller in ["+39000000999", None]:
        assert router.incoming(Incoming("shared", caller, "+39000000101")).profile.id == "member_a"
        assert router.incoming(Incoming("shared", caller, "+39000000102")).profile.id == "member_b"


def test_calling_owner_number_is_never_used_as_recipient():
    router = Router(family_with_routes(IngressRoute("shared", "member_a", "+39000000101", True)))
    assert router.incoming(Incoming("shared", "+39000000101")).profile is None


def test_unvalidated_and_ambiguous_routes_fail_to_general_greeting():
    unvalidated = Router(family_with_routes(IngressRoute("shared", "member_a", None, False)))
    assert unvalidated.incoming(Incoming("shared")).profile is None
    ambiguous = Router(
        family_with_routes(
            IngressRoute("shared", "member_a", None, True),
            IngressRoute("shared", "member_b", None, True),
        )
    )
    assert ambiguous.incoming(Incoming("shared")).source == "ambiguous_route"


def test_ambiguous_or_unknown_spoken_name_does_not_guess():
    family = family_with_routes()
    family = replace(family, profiles=tuple(replace(p, aliases=("Alex",)) for p in family.profiles))
    assert Router(family).spoken("Alex").profile is None
    assert Router(family).spoken("not-a-person").profile is None
    assert Router(family).spoken("member_c").profile.id == "member_c"


def test_profile_prompt_excludes_other_profiles_and_private_destinations():
    family = family_with_routes()
    selected = replace(family.profiles[0], telegram_chat_id="123456789")
    prompt = Router(family).instructions(selected)
    assert "123456789" not in prompt
    assert "Persona B" not in prompt
    assert "Persona C" not in prompt
    assert "Pronto" in prompt


def test_disabled_profile_cannot_be_selected():
    family = family_with_routes()
    family = replace(family, profiles=(replace(family.profiles[0], enabled=False),))
    assert Router(family).spoken("member_a").profile is None


@pytest.mark.parametrize("original", ["+39000000101", "+39000000199", None, "invalid"])
@pytest.mark.parametrize("alternative_validated", [True, False])
def test_disabled_overlapping_route_keeps_shared_line_ambiguous(
    family, original, alternative_validated
):
    disabled = replace(family.profiles[1], enabled=False)
    routes = (
        IngressRoute("shared", "member_a", None, True),
        IngressRoute("shared", disabled.id, "+39000000101", alternative_validated),
    )
    configured = replace(family, routes=routes, profiles=(family.profiles[0], disabled))
    router = Router(configured)
    resolution = router.incoming(Incoming("shared", original_called_number=original))
    assert resolution.profile is None
    assert resolution.source == "ambiguous_route"
    assert configured.unresolved_greeting in router.instructions(resolution.profile)


def test_duplicate_routes_to_same_person_are_safe_but_a_disabled_target_is_not(family):
    original = "+39000000101"
    routes = (
        IngressRoute("shared", "member_a", None, True),
        IngressRoute("shared", "member_a", original, True),
    )
    call = Incoming("shared", original_called_number=original)
    active = replace(family, routes=routes)
    resolved = Router(active).incoming(call)
    assert resolved.profile == family.profiles[0]
    assert resolved.source == "verified_original_number"

    disabled = replace(family.profiles[0], enabled=False)
    router = Router(replace(active, profiles=(disabled, *family.profiles[1:])))
    resolution = router.incoming(call)
    assert resolution.profile is None
    assert resolution.source == "disabled_route"


def test_disabled_profile_does_not_make_a_shared_spoken_alias_unambiguous(family):
    active = replace(family.profiles[0], aliases=("Same Name", "..."))
    disabled = replace(family.profiles[1], aliases=("Same Name",), enabled=False)
    router = Router(replace(family, profiles=(active, disabled)))
    resolution = router.spoken("Same Name")
    assert resolution.profile is None
    assert resolution.source == "ambiguous_name"
    assert router.spoken("...").profile is None
    assert router.spoken(active.owner_name).profile == active
    assert router.spoken(disabled.owner_name).profile is None
