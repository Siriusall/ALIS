from app.services import stock_screener


def test_pool_score_handles_normal_and_malformed_reasons():
    assert stock_screener._pool_score("auto_screener: score=72.5 conf=0.8") == 72.5
    assert stock_screener._pool_score("legacy reason without score") == 0.0
    assert stock_screener._pool_score("score=not-a-number") == 0.0
