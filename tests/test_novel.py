from src.novel import user_subscription_status


def test_user_subscription_status_reports_free_for_zero_plus_type():
    assert user_subscription_status({"result": {"login": {"mem_plus_type": 0}}}) == "free"


def test_user_subscription_status_reports_paid_for_plus_type():
    assert user_subscription_status({"result": {"login": {"mem_plus_type": "1"}}}) == "paid"


def test_user_subscription_status_reports_paid_when_subscription_exists():
    response = {"result": {"login": {"mem_plus_type": 0}, "subscription": {"plan": "plus"}}}

    assert user_subscription_status(response) == "paid"


def test_user_subscription_status_reports_unknown_for_unexpected_shape():
    assert user_subscription_status({"result": {"login": {}}}) == "unknown"
