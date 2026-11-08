from dataclasses import replace
from pathlib import Path

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
