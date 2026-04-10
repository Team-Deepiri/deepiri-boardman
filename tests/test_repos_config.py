import pytest

import boardman.repos_config as rc


@pytest.mark.asyncio
async def test_list_workspace_repos_merges_org_and_yaml(monkeypatch, tmp_path):
    yml = tmp_path / "repos.yml"
    yml.write_text(
        "defaults:\n  category: misc\n  plaky_table: Inbox\n"
        "repos:\n  deepiri-org/one:\n    category: backend\n    plaky_table: API\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rc.settings, "repos_yml_path", str(yml))
    monkeypatch.setattr(rc.settings, "github_pat", "fake-token")
    monkeypatch.setattr(rc.settings, "github_org", "deepiri-org")

    async def fake_fetch(client, org, skip_archived=True):
        assert org == "deepiri-org"
        return ["deepiri-org/one", "deepiri-org/two"]

    monkeypatch.setattr(
        "boardman.github.org_repos.fetch_org_repository_full_names",
        fake_fetch,
    )

    rc.reload_repos_config()
    out = await rc.list_workspace_repos()
    assert out["deepiri-org/one"].category == "backend"
    assert out["deepiri-org/one"].plaky_table == "API"
    assert out["deepiri-org/two"].category == "misc"
    assert out["deepiri-org/two"].plaky_table == "Inbox"


@pytest.mark.asyncio
async def test_list_workspace_repos_yaml_only_without_pat(monkeypatch, tmp_path):
    yml = tmp_path / "repos.yml"
    yml.write_text(
        "repos:\n  other/extra:\n    category: x\n    plaky_table: T\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rc.settings, "repos_yml_path", str(yml))
    monkeypatch.setattr(rc.settings, "github_pat", None)

    rc.reload_repos_config()
    out = await rc.list_workspace_repos()
    assert list(out.keys()) == ["other/extra"]
