from src.novel import html_from_episode_text, user_subscription_status


def test_user_subscription_status_reports_free_for_zero_plus_type():
    assert user_subscription_status({"result": {"login": {"mem_plus_type": 0}}}) == "free"


def test_user_subscription_status_reports_paid_for_plus_type():
    assert user_subscription_status({"result": {"login": {"mem_plus_type": "1"}}}) == "paid"


def test_user_subscription_status_reports_paid_when_subscription_exists():
    response = {"result": {"login": {"mem_plus_type": 0}, "subscription": {"plan": "plus"}}}

    assert user_subscription_status(response) == "paid"


def test_user_subscription_status_reports_unknown_for_unexpected_shape():
    assert user_subscription_status({"result": {"login": {}}}) == "unknown"


def test_html_from_episode_text_normalizes_data_src():
    html = '<div><img data-src="//cdn.example.com/img.jpg"></div>'
    result = html_from_episode_text(html)
    assert 'src="https://cdn.example.com/img.jpg"' in result


def test_html_from_episode_text_wraps_bare_content():
    result = html_from_episode_text("<p>Hello</p>")
    assert "<html>" in result
    assert 'charset="utf-8"' in result


def test_html_from_episode_text_handles_empty_input():
    result = html_from_episode_text("")
    assert "<html>" in result
