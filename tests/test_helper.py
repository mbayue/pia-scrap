from src.helper import j, mask_kv


def test_mask_kv_masks_nested_tokens():
    data = {
        "result": {"LOGINAT": "secret-login", "nested": {"_t": "secret-token"}},
        "password": "secret-password",
        "safe": "value",
    }

    assert mask_kv(data) == {
        "result": {"LOGINAT": "***", "nested": {"_t": "***"}},
        "password": "***",
        "safe": "value",
    }


def test_j_serializes_unicode_json():
    assert j({"message": "안녕"}) == '{"message": "안녕"}'
