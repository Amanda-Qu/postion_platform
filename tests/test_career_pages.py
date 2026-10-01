"""Offline parser contracts using real public-page DOM captures.

Fixture provenance explicitly distinguishes successful HTTPS capture from the
robots-checked safe_fetch runtime, which could not resolve DNS in the capture
environment. No test makes live network calls.
"""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
import pytest

from app import career_pages, sources
from app.store import Store, canonical_url


FIXTURES = Path(__file__).parent / "fixtures" / "career_pages"
ADAPTERS = [
    ("tokenfab", career_pages.TOKENFAB_URL, career_pages.parse_tokenfab),
    ("extremevision", career_pages.EXTREMEVISION_URL, career_pages.parse_extremevision),
]


def response(kind, markup=None, url=None):
    body = markup if markup is not None else (FIXTURES / f"{kind}.html").read_text(encoding="utf-8")
    return sources.FetchResult(url or career_pages._URLS[kind], 200,
                               {"content-type": "text/html; charset=utf-8"}, body.encode("utf-8"))


def fixture_soup(kind):
    return BeautifulSoup(response(kind).text, "html.parser")


def test_tokenfab_genuine_cards_keep_multicity_and_complete_requirements():
    jobs = career_pages.parse_tokenfab(response("tokenfab"))
    assert len(jobs) == 3
    by_title = {job["title"]: job for job in jobs}
    assert by_title["AIDC交付经理"]["city"] == "贵阳"
    job = by_title["大模型推理加速专家"]
    assert job["company"] == "TokenFab"
    assert job["city"] == "深圳 · 上海 · 杭州"
    assert "模型蒸馏、训练后量化（PTQ/QAT）" in job["responsibilities"]
    assert "硕士及以上学历，3年以上" in job["education"]
    assert job["education"] == job["experience"]
    assert "Speculative Decoding" in job["raw_text"]
    gateway = by_title["LLM 推理网关工程师"]
    assert "3–8 年服务端、网关经验" in gateway["experience"]


def test_extremevision_genuine_list_omits_placeholders_and_separates_cities():
    jobs = career_pages.parse_extremevision(response("extremevision"))
    assert len(jobs) == 13
    assert all(job["title"] != "更多岗位即将开放" for job in jobs)
    llm = [job for job in jobs if job["title"] == "大模型算法工程师"]
    assert {job["city"] for job in llm} == {"青岛", "深圳"}
    assert len({job["source_identity"] for job in llm}) == 2
    assert all(job["company"] == "极视角" for job in jobs)
    assert "硕士以上学历" in llm[0]["education"]
    assert "3年以上工作经验" in llm[0]["experience"]
    assert "CV + NLP" in llm[0]["responsibilities"]
    researcher = next(job for job in jobs if job["title"] == "高级算法研究员")
    assert "工作4年以上" in researcher["experience"]
    product = next(job for job in jobs if job["title"] == "AI产品经理")
    assert "岗位描述" in product["responsibilities"]
    assert "任职资格" in product["requirements"]
    assert product["experience"] == "未知"


@pytest.mark.parametrize("kind,url,parser", ADAPTERS)
def test_unpublished_pay_dates_and_validity_are_not_invented(kind, url, parser):
    jobs = parser(response(kind))
    assert len({job["source_identity"] for job in jobs}) == len(jobs)
    for job in jobs:
        assert job["salary_raw"] == "未知"
        assert job["published_at"] is None
        assert job["status_validity"] == "未知"
        assert job["source_url"] == url  # A real page, no manufactured query/fragment.
        assert job["source_identity"].startswith(kind + ":")


@pytest.mark.parametrize("kind,url,parser", ADAPTERS)
def test_fetch_hook_uses_shared_safe_fetch_with_default_robots(kind, url, parser):
    with patch.object(sources, "safe_fetch", return_value=response(kind)) as fetch:
        jobs = sources.fetch_source({"kind": kind, "url": url, "name": "Configured name"})
    fetch.assert_called_once_with(url, max_bytes=5 * 1024 * 1024)
    assert jobs and all(job["source_name"] == "Configured name" for job in jobs)


@pytest.mark.parametrize("kind,url,parser", ADAPTERS)
def test_default_url_and_network_failures_use_same_safety_boundary(kind, url, parser):
    with patch.object(sources, "safe_fetch", side_effect=sources.SourceError("robots denied")) as fetch:
        with pytest.raises(sources.SourceError, match="robots denied"):
            career_pages.fetch_career_page({"kind": kind})
    fetch.assert_called_once_with(url, max_bytes=5 * 1024 * 1024)


@pytest.mark.parametrize("url", [
    "http://www.tokenfab.cn/join.html", "https://127.0.0.1/join.html",
    "https://evil.example/join.html", "https://www.tokenfab.cn.evil.example/join.html",
    "https://www.tokenfab.cn:8443/join.html", "https://u:p@www.tokenfab.cn/join.html",
    "https://www.tokenfab.cn/login", "https://www.tokenfab.cn/join.html?redirect=private",
    "https://www.tokenfab.cn/join.html#fake-job", "https://www.tokenfab.cn/\njoin.html",
    1, {"url": "https://www.tokenfab.cn/join.html"},
])
def test_focused_adapter_rejects_other_targets_before_fetch(url):
    with patch.object(sources, "safe_fetch", side_effect=AssertionError("must not fetch")):
        with pytest.raises(sources.SourceError):
            career_pages.fetch_career_page({"kind": "tokenfab", "url": url})


@pytest.mark.parametrize("kind,url,parser", ADAPTERS)
def test_unexpected_final_redirect_target_is_not_parsed(kind, url, parser):
    with patch.object(sources, "safe_fetch", return_value=response(kind, url="https://other.example/login")):
        with pytest.raises(sources.SourceError, match="重定向"):
            career_pages.fetch_career_page({"kind": kind})


@pytest.mark.parametrize("kind,url,parser", ADAPTERS)
@pytest.mark.parametrize("markup", ["", "<html>Please log in</html>", "<div>岗位职责：更多机会</div>"])
def test_empty_login_or_selector_drift_is_an_error_not_zero_jobs(kind, url, parser, markup):
    with pytest.raises(sources.SourceError):
        parser(response(kind, markup))


@pytest.mark.parametrize("kind,selector,parser", [
    ("tokenfab", ".job-title", career_pages.parse_tokenfab),
    ("tokenfab", ".job-detail", career_pages.parse_tokenfab),
    ("extremevision", ".li-title .name", career_pages.parse_extremevision),
    ("extremevision", ".recruitment .list .text", career_pages.parse_extremevision),
])
def test_one_malformed_card_fails_whole_response_instead_of_partial_success(kind, selector, parser):
    soup = fixture_soup(kind)
    soup.select_one(selector).decompose()
    with pytest.raises(sources.SourceError):
        parser(response(kind, str(soup)))


@pytest.mark.parametrize("kind,url,parser", ADAPTERS)
def test_missing_requirement_section_fails_loudly(kind, url, parser):
    markup = response(kind).text.replace("任职要求", "更改的章节").replace("任职资格", "更改的章节").replace("职位要求", "更改的章节")
    with pytest.raises(sources.SourceError, match="内容不完整"):
        parser(response(kind, markup))


def test_extremevision_only_empty_placeholders_does_not_prove_closed_jobs():
    soup = fixture_soup("extremevision")
    for card in soup.select(".recruitment .list > ul > li"):
        if card.select_one(".name").get_text(strip=True) != "更多岗位即将开放":
            card.decompose()
    with pytest.raises(sources.SourceError, match="不能据此判断岗位关闭"):
        career_pages.parse_extremevision(response("extremevision", str(soup)))


@pytest.mark.parametrize("kind,selector,parser", [
    ("tokenfab", ".job-title", career_pages.parse_tokenfab),
    ("extremevision", ".li-title .name", career_pages.parse_extremevision),
])
def test_missing_location_stays_unknown_without_using_other_roles_or_footer(kind, selector, parser):
    soup = fixture_soup(kind)
    title = soup.select_one(selector)
    if kind == "tokenfab":
        title.parent.parent.select_one("span").decompose()
    else:
        title.find_parent("li").select_one(".des").decompose()
    jobs = parser(response(kind, str(soup)))
    assert jobs[0]["city"] == "未知"
    assert jobs[1]["city"] != "未知"


def test_tokenfab_identity_survives_description_change_and_reorder():
    before = career_pages.parse_tokenfab(response("tokenfab"))
    soup = fixture_soup("tokenfab")
    soup.select_one(".job-detail li").append(" 已更新的职责")
    cards = soup.select(".job-card")
    cards[0].parent.append(cards[0].extract())
    after = career_pages.parse_tokenfab(response("tokenfab", str(soup)))
    assert {j["title"]: j["source_identity"] for j in before} == {j["title"]: j["source_identity"] for j in after}


def test_tokenfab_partial_card_selector_drift_does_not_omit_job_silently():
    soup = fixture_soup("tokenfab")
    soup.select_one(".job-card")["class"] = ["renamed-card"]
    with pytest.raises(sources.SourceError, match="数量不一致"):
        career_pages.parse_tokenfab(response("tokenfab", str(soup)))


def test_identical_duplicate_is_deduplicated_but_conflicting_duplicate_fails():
    soup = fixture_soup("tokenfab")
    original = soup.select_one(".job-card")
    soup.body.append(deepcopy(original))
    assert len(career_pages.parse_tokenfab(response("tokenfab", str(soup)))) == 3
    soup.select(".job-card")[-1].select_one(".job-detail li").append(" conflicting content")
    with pytest.raises(sources.SourceError, match="无法安全区分"):
        career_pages.parse_tokenfab(response("tokenfab", str(soup)))


def test_page_wide_urls_do_not_merge_multiple_jobs_and_refresh_stays_idempotent(tmp_path):
    store = Store(tmp_path)
    records = []
    for kind, _, parser in ADAPTERS:
        records.extend(parser(response(kind)))
    assert len(records) == 16
    assert len({canonical_url(j["source_url"]) for j in records}) == 2

    def ingest(record):
        job = deepcopy(record)
        job["sources"] = [{"url": job.pop("source_url"), "name": job.pop("source_name"),
                           "source_identity": job.pop("source_identity")}]
        job["ingest_mode"] = "source_refresh"
        return store.ingest(job)

    initial_ids = [ingest(record)[0]["id"] for record in records]
    assert len(set(initial_ids)) == 16
    assert len(store.all("jobs")) == 16
    for record, job_id in zip(records, initial_ids):
        record["responsibilities"] += "\nUpdated responsibility."
        saved, new = ingest(record)
        assert not new and saved["id"] == job_id
        assert saved["responsibilities"].endswith("Updated responsibility.")
        assert saved["status_validity"] == "未知"
    assert len(store.all("jobs")) == 16
