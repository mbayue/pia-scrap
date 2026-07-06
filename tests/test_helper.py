from src.helper import j, load_config, mask_kv, save_config


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


def test_save_config_writes_json_atomically(monkeypatch, tmp_path):
    config_path = tmp_path / ".api.json"
    monkeypatch.setattr("src.helper.CONFIG_PATH", str(config_path))

    save_config({"login_at": "token", "userkey": "user", "tkey": "t"})

    assert load_config() == {"login_at": "token", "userkey": "user", "tkey": "t"}
    assert list(tmp_path.iterdir()) == [config_path]
