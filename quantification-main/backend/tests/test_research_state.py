from app.services.research_engine import research_state


def test_research_confidence_retains_partial_real_evidence():
    dimensions = {
        "quality": {"score": 80, "coverage": .8, "confidence": .8, "status": "ready"},
        "growth": {"score": 70, "coverage": .7, "confidence": .7, "status": "ready"},
        "cashflow": {"score": 0, "coverage": 0, "confidence": 0, "status": "insufficient_data"},
        "governance": {"score": 0, "coverage": 0, "confidence": 0, "status": "insufficient_data"},
        "industry": {"score": 0, "coverage": 0, "confidence": 0, "status": "insufficient_data"},
        "expectation": {"score": 0, "coverage": 0, "confidence": 0, "status": "insufficient_data"},
        "valuation": {"score": 0, "coverage": 0, "confidence": 0, "status": "insufficient_data"},
    }

    result = research_state(dimensions, {"eligible": True})

    assert result["confidence"] == 0.2143
    assert result["covered_dimensions"] == 2
    assert result["total_dimensions"] == 7
