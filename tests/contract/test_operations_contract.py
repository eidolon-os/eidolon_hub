"""Hub's operations contract against Hub's own code.

``ops/component.toml`` is what Ops believes about how Hub is deployed. It is
worth only as much as it is true, and it stops being true the ordinary way:
someone moves the database, renames a settings key, drops a dependency, and
the contract keeps asserting the old shape until a Host disagrees with it.

Every test here pins one claim in that file to the thing in this repository
that would have to change with it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

from hub.config import HubConfig

_REPOSITORY = Path(__file__).resolve().parents[2]
_CONTRACT = _REPOSITORY / "ops/component.toml"


@pytest.fixture(scope="module")
def contract() -> dict:
    return tomllib.loads(_CONTRACT.read_text(encoding="utf-8"))


def _input(contract: dict, name: str) -> dict:
    return next(entry for entry in contract["inputs"] if entry["name"] == name)


def test_the_declared_database_is_the_one_hub_would_open(
    contract: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EIDOLON_STATE_ROOT", "/var/lib/eidolon")
    # The default is computed from the environment at construction, so build a
    # config the way a Host would rather than reading the literal back.
    resolved = Path(HubConfig().persistence.path)

    declared = Path(contract["state"]["authority"][0]["path"])

    # Moving the database without moving this line would produce a backup that
    # runs, reports success, and copies a file nothing writes to any more.
    assert resolved == declared


def test_the_settings_a_host_runs_on_are_this_repositorys_own(contract: dict) -> None:
    entry = _input(contract, "hub_settings")

    # The template a Host is started from is in this repository, and it is the
    # same file Hub loads locally — not a deployment copy beside it. It used to
    # be eidolon_kernel/config/hub.systemd.example.yaml, which cost Hub the
    # ability to change its own deployed defaults and meant this suite was not
    # exercising the file a Host runs on.
    assert entry["template"] == "config/settings.yaml"
    assert (_REPOSITORY / entry["template"]).is_file()

    # No way back to a template someone else owns: Ops derives the repository
    # from the component that declared the input, so a second key here would be
    # read by nobody while looking authoritative.
    assert "template_component" not in entry


def test_the_declared_template_is_the_file_hub_itself_loads(
    contract: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = _REPOSITORY / _input(contract, "hub_settings")["template"]

    monkeypatch.delenv("EIDOLON_HUB_SETTINGS_YAML", raising=False)
    resolved_by_hub = HubConfig.load()
    monkeypatch.setenv("EIDOLON_HUB_SETTINGS_YAML", str(template))

    # Hub resolves its own settings path; the contract states a template path.
    # A rename that moved only one of them would leave Ops rendering a file Hub
    # does not read, which is the shape of the bug this migration removed.
    assert HubConfig.load() == resolved_by_hub


def test_the_template_still_carries_what_ops_rewrites(contract: dict) -> None:
    template = (_REPOSITORY / _input(contract, "hub_settings")["template"]).read_text(
        encoding="utf-8"
    )

    # Ops matches these two lines literally and exactly once each, then replaces
    # them with the Host's own identity. Renaming, nesting, quoting or folding
    # either one would not fail Hub's own validation — it would fail a release,
    # or worse, ship a Hub answering to a name no device asked for.
    for line in ("hub_id: eidolon-hub-local", "public_base_url: https://eidolon-hub.local"):
        assert template.count(line) == 1


def test_a_rendered_template_is_still_settings_hub_accepts(contract: dict) -> None:
    template = (_REPOSITORY / _input(contract, "hub_settings")["template"]).read_text(
        encoding="utf-8"
    )
    rendered = template.replace(
        "hub_id: eidolon-hub-local", "hub_id: eidolon-hub-b3c897513cdce9"
    ).replace(
        "public_base_url: https://eidolon-hub.local",
        "public_base_url: https://eidolon-hub-b3c897513cdce9.local:8443",
    )

    # What Ops writes onto a Host has to be a document Hub's strict, extra-forbid
    # config still validates — including the port in the origin, which the local
    # default does not carry.
    config = HubConfig.model_validate(yaml.safe_load(rendered))

    assert config.onboarding.hub_id == "eidolon-hub-b3c897513cdce9"
    assert config.onboarding.public_base_url.endswith(":8443")


def test_the_declared_database_is_the_one_hubs_settings_point_at(
    contract: dict,
) -> None:
    document = yaml.safe_load(
        (_REPOSITORY / _input(contract, "hub_settings")["template"]).read_text(encoding="utf-8")
    )

    declared = contract["state"]["authority"][0]["path"]
    on_a_host = document["persistence"]["path"].replace(
        "$EIDOLON_STATE_ROOT", "/var/lib/eidolon"
    )

    assert on_a_host == declared


def test_the_declared_entrypoint_is_a_dependency_hub_actually_has(
    contract: dict,
) -> None:
    unit = contract["units"][0]
    executable = Path(unit["exec"]).name

    project = tomllib.loads((_REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))
    declared_dependencies = " ".join(project["project"]["dependencies"])

    # Hub's unit does not run a console script of its own; it runs uvicorn out
    # of Hub's venv. Dropping uvicorn from this repository would leave the unit
    # pointing at a file that no longer gets installed.
    assert executable in declared_dependencies


def test_every_declared_unit_is_one_the_release_ships(contract: dict) -> None:
    # The unit files still live in eidolon_kernel/deploy/systemd. This checks
    # the naming Ops will look them up by, which is the half Hub controls.
    for unit in contract["units"]:
        assert unit["id"].startswith("eidolon-")
        assert not unit["exec"].startswith("/")


def test_nothing_in_the_contract_is_a_secret(contract: dict) -> None:
    body = _CONTRACT.read_text(encoding="utf-8")

    # This file is committed. Anything in it is readable by everyone who can
    # read the repository, so it names inputs and never carries them.
    for entry in contract["inputs"]:
        assert set(entry) <= {
            "name",
            "install_path",
            "kind",
            "source",
            "owner",
            "group",
            "mode",
            "template",
            "required_by",
        }
    for marker in ("PRIVATE KEY", "SECRET", "password", "token ="):
        assert marker not in body
