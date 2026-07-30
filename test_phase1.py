from pydantic import ValidationError

from app.worker.schemas import Claim


def run_tests():
    # Test 1: Valid Fact Claim
    c = Claim(
        statement="GPT-4 is created by OpenAI",
        claim_type="fact",
        status="подтверждено",
        checked_at="2026-07-30T12:00:00Z",
        source_name="OpenAI",
        source_url="https://openai.com",
        source_type="official",
        exact_quote="We introduced GPT-4...",
        confidence_level="high"
    )
    assert c.claim_type == "fact"
    
    # Test 2: Valid Opinion Claim
    c2 = Claim(
        statement="This is a great tool",
        claim_type="opinion",
        status="пропущено"
    )
    assert c2.status == "пропущено"

    # Test 3: Invalid status
    try:
        Claim(statement="Test", claim_type="fact", status="unknown_status")
        assert False, "Should raise ValidationError"
    except ValidationError:
        pass
    
    print("All Pydantic schema tests passed successfully!")

