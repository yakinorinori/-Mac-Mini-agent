"""テストを実行環境から切り離す。常駐サービスの設定などを読まないようにする。"""
import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.delenv("AGENT_STATE_DIR", raising=False)
    monkeypatch.delenv("AGENT_ENV_FILE", raising=False)


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch):
    """テストごとに空の保存先を使う。"""
    import main
    from store import Store, StoredApprovals

    fresh = Store()
    monkeypatch.setattr(main, "store", fresh)
    monkeypatch.setattr(main, "approvals", StoredApprovals(fresh))
    return fresh
