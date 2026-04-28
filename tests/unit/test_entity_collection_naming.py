"""Test for Bug #15 — naive `name.lower() + "s"` pluralization.

Pre-fix the entity-create CLI auto-derived `collection_name` as
`name.lower() + "s"`, which produced typo'd plurals: `Company` ->
`companys`, `Opportunity` -> `opportunitys`. English plurals are harder
than that.

Per Craig 2026-04-28 ("accept and fix forward"): existing collections
in dev/prod retain their typo'd names — no rename migration. New entity
defs that omit `--collection-name` get a proper plural via the
`inflect` library going forward. Operators who need to land in an
existing typo'd collection (cross-org re-clone) pass `--collection-name`
explicitly.

These tests pin the new auto-derive behavior at both CLI surfaces
(kernel + user-facing) so the fix doesn't regress to the typo and
existing clean entity names (Email -> emails) stay clean.
"""

from indemn_os.entity_commands import _default_collection_name as _user_default
from kernel.cli.entity_commands import _default_collection_name as _kernel_default


def test_company_pluralizes_correctly():
    """The exact case the bug filed: Company should become companies, not companys."""
    assert _user_default("Company") == "companies"
    assert _kernel_default("Company") == "companies"


def test_opportunity_pluralizes_correctly():
    """The other case the bug filed: Opportunity -> opportunities."""
    assert _user_default("Opportunity") == "opportunities"
    assert _kernel_default("Opportunity") == "opportunities"


def test_already_clean_plurals_stay_clean():
    """Email -> emails was correct under the naive +s rule too. The new
    rule must not regress that."""
    assert _user_default("Email") == "emails"
    assert _user_default("Meeting") == "meetings"
    assert _user_default("Touchpoint") == "touchpoints"
    assert _user_default("Deal") == "deals"


def test_irregular_plurals():
    """Real English plurals: Person -> people, not persons."""
    assert _user_default("Person") == "people"


def test_both_cli_surfaces_agree():
    """Kernel CLI and user-facing CLI must produce the same default name —
    the dual-codebase issue from Bug #5 means we have to pin both."""
    for name in ["Company", "Opportunity", "Email", "Person", "Document"]:
        assert _user_default(name) == _kernel_default(name), (
            f"CLI surfaces disagree on default collection_name for {name}"
        )
