"""Authenticated source settings regressions with isolated stores and no network."""
import copy

import pytest

from app.source_catalog import company_catalog
from app.sources import source_catalog
from test_workflow import desk


RUNTIME = {
    "status": "最近获取失败",
    "message": "测试夹具：上次连接未成功，保留真实结果",
    "last_fetched": "2026-09-30T09:00:00+00:00",
    "last_attempt": "2026-10-01T09:00:00+00:00",
}


def seed_sources(desk, sources):
    settings = desk.store.get("settings")
    settings["sources"] = copy.deepcopy(sources)
    desk.store.set("settings", settings)
    return settings


def assert_runtime(source):
    assert {key: source.get(key) for key in RUNTIME} == RUNTIME


def test_unchanged_legacy_default_sources_keep_connection_evidence(desk):
    # Bootstrap defaults predate SourceConfig's added board/url empty defaults.
    sources = source_catalog()
    for source in sources:
        source.update(RUNTIME)
    settings = seed_sources(desk, sources)
    response = desk.client.put("/api/settings", json=settings)
    assert response.status_code == 200, response.text
    for source in response.json()["sources"]:
        assert_runtime(source)
    for source in desk.store.get("settings")["sources"]:
        assert_runtime(source)


@pytest.mark.parametrize("previous_empty", [None, "", "missing"])
def test_save_treats_missing_none_and_empty_optional_config_as_equal(desk, previous_empty):
    source = dict(id="legacy-lever", name="测试夹具 Lever", kind="lever",
                  site="fixture-site", enabled=False, **RUNTIME)
    fields = ("board", "url", "region", "company", "slug")
    if previous_empty != "missing":
        source.update({key: previous_empty for key in fields})
    settings = seed_sources(desk, [source])
    submitted = copy.deepcopy(settings)
    submitted["sources"][0].update({key: "" for key in fields})
    # A client snapshot may lag behind a background connection result.
    submitted["sources"][0].update(status="已连接", message="客户端旧状态",
                                   last_fetched="old", last_attempt="old")
    response = desk.client.put("/api/settings", json=submitted)
    assert response.status_code == 200, response.text
    assert_runtime(response.json()["sources"][0])


@pytest.mark.parametrize("append_route", ["settings", "catalog"])
def test_appending_catalog_source_preserves_existing_connection_evidence(desk, append_route):
    sources = source_catalog()
    for source in sources:
        source.update(RUNTIME)
    settings = seed_sources(desk, sources)
    if append_route == "catalog":
        response = desk.client.post("/api/sources/catalog/merge", json={"ids": ["tokenfab"]})
        assert response.status_code == 200, response.text
        saved = response.json()["settings"]
    else:
        settings["sources"].append(company_catalog()[0])
        response = desk.client.put("/api/settings", json=settings)
        assert response.status_code == 200, response.text
        saved = response.json()
    assert len(saved["sources"]) == len(sources) + 1
    for source in saved["sources"][:-1]:
        assert_runtime(source)
    # The next ordinary settings save must also keep evidence after validation
    # has introduced explicit empty defaults into the stored source rows.
    again = desk.client.put("/api/settings", json=saved)
    assert again.status_code == 200, again.text
    for source in again.json()["sources"][:-1]:
        assert_runtime(source)


def test_source_label_and_enable_changes_keep_connection_evidence(desk):
    source = dict(id="greenhouse-fixture", name="测试夹具 Greenhouse", kind="greenhouse",
                  board="fixture-board", url="", company="测试夹具公司", enabled=False, **RUNTIME)
    settings = seed_sources(desk, [source])
    settings["sources"][0].update(name="重新命名的测试夹具来源", enabled=True)
    response = desk.client.put("/api/settings", json=settings)
    assert response.status_code == 200, response.text
    saved = response.json()["sources"][0]
    assert saved["name"] == "重新命名的测试夹具来源" and saved["enabled"] is True
    assert_runtime(saved)


@pytest.mark.parametrize("kind,identifier", [("greenhouse", "board"), ("lever", "site")])
def test_clearing_legacy_slug_requires_connection_revalidation(desk, kind, identifier):
    source = dict(id="legacy-slug", name="测试夹具旧标识", kind=kind,
                  slug="old-fixture", enabled=False, **RUNTIME)
    settings = seed_sources(desk, [source])
    submitted = copy.deepcopy(settings)
    submitted["sources"][0].pop("slug")
    submitted["sources"][0][identifier] = ""
    response = desk.client.put("/api/settings", json=submitted)
    assert response.status_code == 200, response.text
    saved = response.json()["sources"][0]
    assert not saved.get("slug") and not saved.get(identifier)
    assert saved["status"] == "需要配置"


@pytest.mark.parametrize("kind,config,field,changed", [
    ("greenhouse", {"board": "fixture-board"}, "board", "another-board"),
    ("lever", {"site": "fixture-site"}, "site", "another-site"),
    ("public_page", {"url": "https://example.invalid/jobs"}, "url", "https://example.invalid/other"),
    ("tokenfab", {"url": "https://www.tokenfab.cn/join.html"}, "url", "https://example.invalid/jobs"),
    ("greenhouse", {"board": "fixture-board", "company": "测试夹具公司"}, "company", "另一个公司"),
    ("lever", {"site": "fixture-site", "region": "eu"}, "region", ""),
    ("greenhouse", {"board": "fixture-board"}, "kind", "lever"),
    ("greenhouse", {"slug": "fixture-board"}, "slug", "another-board"),
    ("lever", {"slug": "fixture-site"}, "slug", "another-site"),
])
def test_actual_source_config_changes_require_connection_revalidation(desk, kind, config, field, changed):
    source = dict(id="changed-source", name="测试夹具来源", kind=kind, enabled=False,
                  board="", site="", url="", company="", region="", slug="", **RUNTIME)
    source.update(config)
    settings = seed_sources(desk, [source])
    settings["sources"][0][field] = changed
    response = desk.client.put("/api/settings", json=settings)
    assert response.status_code == 200, response.text
    saved = response.json()["sources"][0]
    assert saved[field] == changed
    assert saved["status"] == "需要配置"
    assert saved["message"] == "配置已更改，请立即更新验证连接"


def test_inflight_fetch_result_survives_unrelated_save_and_empty_normalization(desk, monkeypatch):
    source = dict(id="legacy-lever", name="测试夹具 Lever", kind="lever",
                  site="fixture-site", enabled=True, **RUNTIME)
    seed_sources(desk, [source])

    def fetch_with_intervening_save(config):
        submitted = desk.client.get("/api/settings").json()
        submitted["schedule_time"] = "10:10"
        submitted["sources"][0].update(name="用户刚修改的来源名称", enabled=False,
                                       board="", url="", region="", company="", slug="")
        response = desk.client.put("/api/settings", json=submitted)
        assert response.status_code == 200, response.text
        assert_runtime(response.json()["sources"][0])
        return []

    monkeypatch.setattr(desk.worker_module, "fetch_source", fetch_with_intervening_save)
    run = desk.worker.discover(desk.worker.request_discovery())
    assert run["status"] == "completed"
    latest = desk.store.get("settings")
    assert latest["schedule_time"] == "10:10"
    saved = latest["sources"][0]
    assert saved["name"] == "用户刚修改的来源名称" and saved["enabled"] is False
    assert saved["status"] == "已连接"
    assert saved["message"] == "获取 0 条，按方向保留 0 条；新增 0 条"
    assert saved["last_fetched"] != RUNTIME["last_fetched"]
    assert saved["last_attempt"] == RUNTIME["last_attempt"]


@pytest.mark.parametrize("kind,config,field,changed", [
    ("greenhouse", {"board": "fixture-board"}, "board", "another-board"),
    ("lever", {"site": "fixture-site"}, "site", "another-site"),
    ("public_page", {"url": "https://example.invalid/jobs"}, "url", "https://example.invalid/other"),
    ("greenhouse", {"board": "fixture-board"}, "kind", "lever"),
    ("greenhouse", {"slug": "fixture-board"}, "slug", "another-board"),
    ("lever", {"slug": "fixture-site"}, "slug", "another-site"),
])
def test_inflight_old_fetch_cannot_validate_changed_configuration(desk, monkeypatch, kind, config, field, changed):
    source = dict(id="inflight-source", name="测试夹具来源", kind=kind, enabled=True,
                  board="", site="", url="", company="", region="", slug="", **RUNTIME)
    source.update(config)
    seed_sources(desk, [source])

    def fetch_with_intervening_config_change(config):
        submitted = desk.client.get("/api/settings").json()
        submitted["sources"][0][field] = changed
        response = desk.client.put("/api/settings", json=submitted)
        assert response.status_code == 200, response.text
        assert response.json()["sources"][0]["status"] == "需要配置"
        return []

    monkeypatch.setattr(desk.worker_module, "fetch_source", fetch_with_intervening_config_change)
    run = desk.worker.discover(desk.worker.request_discovery())
    assert run["status"] == "completed"
    saved = desk.store.get("settings")["sources"][0]
    assert saved[field] == changed
    assert saved["status"] == "需要配置"
    assert saved["message"] == "配置已更改，请立即更新验证连接"
    assert saved["last_fetched"] == RUNTIME["last_fetched"]
