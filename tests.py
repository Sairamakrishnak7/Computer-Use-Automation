from pathlib import Path

from models import CapabilityArtifact, RunResult
from automation import assert_allowed_url, parse_params, render_parameter

ROOT = Path(__file__).resolve().parent


def test_artifacts_validate():
    for name in ["lookup_savings_balance.example.json", "open_subaccount_handoff.example.json"]:
        CapabilityArtifact.model_validate_json((ROOT / "artifacts" / name).read_text(encoding="utf-8"))


def test_business_outcome_contract():
    result = RunResult(status="business_outcome", capability="lookup_savings_balance", business_outcome="member_not_found", message="No member was found")
    assert result.status == "business_outcome"
    assert result.error_code is None


def test_parameter_rendering():
    assert render_parameter("{{member_id}}", {"member_id": "67890"}) == "67890"
    assert parse_params(["member_id=12345", "nickname=Trip"]) == {"member_id": "12345", "nickname": "Trip"}


def test_url_guardrail():
    assert_allowed_url("http://127.0.0.1:5000")
    try:
        assert_allowed_url("https://example.com")
    except RuntimeError:
        return
    raise AssertionError("guardrail did not block disallowed host")


if __name__ == "__main__":
    test_artifacts_validate()
    test_business_outcome_contract()
    test_parameter_rendering()
    test_url_guardrail()
    print("4 tests passed")
