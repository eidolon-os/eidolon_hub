from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_runner():
    script = Path(__file__).resolve().parents[1] / "scripts" / "guard_fake_e2e.py"
    spec = importlib.util.spec_from_file_location("guard_fake_e2e", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario, expected_status",
    [
        ("success", "acknowledged"),
        ("offline", "published"),
        ("mismatch", "failed"),
    ],
)
async def test_guard_fake_e2e_runner_scenarios(scenario: str, expected_status: str) -> None:
    runner = _load_runner()

    result = await runner.run_scenario(scenario)

    assert result["passed"] is True
    assert result["guard_action_status"] == expected_status


@pytest.mark.asyncio
async def test_guard_fake_e2e_runner_rejects_guard_body_payload() -> None:
    runner = _load_runner()

    result = await runner.run_scenario("reject-guard-body")

    assert result["passed"] is True
    assert result["http_status"] == 422
    assert "guard.policy.action" in result["detail"]
