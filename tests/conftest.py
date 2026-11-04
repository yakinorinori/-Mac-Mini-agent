"""テストを実行環境から切り離す。常駐サービスの設定などを読まないようにする。"""
import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.delenv("AGENT_STATE_DIR", raising=False)
    monkeypatch.delenv("AGENT_ENV_FILE", raising=False)
