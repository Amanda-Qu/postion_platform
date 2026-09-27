"""Offline contract/security tests. Live observations are separately documented."""
import json
import socket
from unittest.mock import patch

import pytest

from app import sources


def response(data, url="https://careers.example.com/job", status=200, headers=None):
    content = data if isinstance(data, str) else json.dumps(data)
    return sources.FetchResult(url, status, headers or {}, content.encode("utf-8"))


def public_dns(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


@pytest.mark.parametrize("url", [
    "http://example.com", "https://127.0.0.1", "https://localhost/", "https://2130706433/",
    "https://0x7f000001/", "https://[::1]/", "https://169.254.169.254/latest/meta-data/",
    "https://user:pass@example.com/", "https://example.com:8443/", "https://example.com\\@127.0.0.1/",
    "https://test.local/", "https://%31%32%37.0.0.1/", "https://www.zhipin.com/job_detail/a.html",
    "https://www.linkedin.com/jobs/", "https://api-c.liepin.com/",
])
def test_rejects_private_obfuscated_or_restricted_urls_without_network(url):
    with patch.object(sources.socket, "getaddrinfo", side_effect=AssertionError("must not resolve")):
        with pytest.raises(sources.SourceError):
            sources._validated_target(url)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "fc00::1", "100.64.0.1", "::ffff:127.0.0.1"])
def test_dns_to_internal_address_is_rejected(address):
    answers = public_dns() + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
    with patch.object(sources.socket, "getaddrinfo", return_value=answers):
        with pytest.raises(sources.SourceError, match="禁止访问"):
            sources._validated_target("https://public-looking.example.com/")


def test_redirect_is_revalidated_before_access():
    first = response("", status=302, headers={"location": "https://127.0.0.1/private"})
    with patch.object(sources.socket, "getaddrinfo", side_effect=public_dns), patch.object(sources, "_request_once", return_value=first) as request:
        with pytest.raises(sources.SourceError):
            sources.safe_fetch("https://example.com", respect_robots=False)
        assert request.call_count == 1


def test_robots_wildcards_and_specific_allow():
    rules, delay = sources._robots_rules("User-agent: *\nDisallow: /*?*\nDisallow: /private/*\nAllow: /private/open$\nCrawl-delay: 2")
    assert not sources._robots_allows("https://example.com/jobs?city=sz", rules)
    assert not sources._robots_allows("https://example.com/private/person", rules)
    assert sources._robots_allows("https://example.com/private/open", rules)
    assert delay == 2


def test_robots_crawler_specific_group_wins():
    rules, _ = sources._robots_rules("User-agent: *\nDisallow: /\nUser-agent: PersonalCareerDesk\nAllow: /jobs\nDisallow: /private")
    assert sources._robots_allows("https://example.com/jobs", rules)
    assert not sources._robots_allows("https://example.com/private", rules)


def test_failed_robots_blocks_page_fetch():
    sources._robots_cache.clear()
    with patch.object(sources.socket, "getaddrinfo", side_effect=public_dns), patch.object(sources, "_request_once", return_value=response("blocked", status=403)) as fetch:
        with pytest.raises(sources.SourceError, match="robots"):
            sources.safe_fetch("https://careers.example.com/job")
        assert fetch.call_count == 1


def test_http_failure_is_not_job_closed():
    with patch.object(sources.socket, "getaddrinfo", side_effect=public_dns), patch.object(sources, "_request_once", return_value=response("", status=404)):
        with pytest.raises(sources.SourceError, match="有效状态仍未知"):
            sources.safe_fetch("https://example.com/old-job", respect_robots=False)


def test_greenhouse_uses_publication_not_update_date():
    payload = {"jobs": [{"title": "Vision engineer", "location": {"name": "深圳"}, "content": "<p>检测与部署</p>", "updated_at": "2026-09-27", "absolute_url": "https://example.com/job"}]}
    with patch.object(sources, "safe_fetch", return_value=response(payload)):
        job = sources.fetch_source({"kind": "greenhouse", "board": "example", "company": "Example"})[0]
    assert job["published_at"] is None
    assert job["city"] == "深圳"
    assert job["responsibilities"] == "未知"
    assert job["raw_text"] == "检测与部署"
    assert job["status_validity"] == "有效"


def test_lever_annual_pay_is_not_monthly_or_company_guessed():
    payload = [{"text": "Perception engineer", "descriptionPlain": "Robot learning", "categories": {"location": "Remote"}, "salaryRange": {"currency": "USD", "interval": "per-year-salary", "min": 180000, "max": 240000}, "hostedUrl": "https://example.com/job"}]
    with patch.object(sources, "safe_fetch", return_value=response(payload)):
        job = sources.fetch_source({"kind": "lever", "site": "some-slug"})[0]
    assert "180000" in job["salary_raw"] and "per-year-salary" in job["salary_raw"]
    assert "15000" not in job["salary_raw"]
    assert job["company"] == "未知"
    assert job["published_at"] is None


def test_jsonld_graph_and_duplicate_nodes_merge():
    node = {"@type": "JobPosting", "title": "视觉算法", "hiringOrganization": {"name": "测试公司"}, "datePosted": "2026-09-20", "jobLocation": [{"address": {"addressLocality": "深圳", "addressCountry": "中国"}}], "description": "<p>训练</p>", "baseSalary": {"currency": "CNY", "value": {"minValue": 30000, "maxValue": 60000, "unitText": "MONTH"}}}
    page = response('<script type="application/ld+json">' + json.dumps({"@graph": [node, node]}) + "</script>")
    jobs = sources.parse_public_page(page)
    assert len(jobs) == 1
    assert jobs[0]["city"] == "深圳，中国"
    assert jobs[0]["published_at"] == "2026-09-20"
    assert jobs[0]["salary_raw"] == "CNY 30000–60000 / MONTH"
    assert jobs[0]["status_validity"] == "未知"


def test_missing_jsonld_does_not_manufacture_jobs():
    with pytest.raises(sources.SourceError, match="补充文字或截图"):
        sources.parse_public_page(response("<h1>Sign in</h1>"))


def test_default_source_is_verified_robotics_only():
    enabled = [s for s in sources.source_catalog() if s["enabled"]]
    assert len(enabled) == 1 and enabled[0]["board"] == "figureai"
    assert all(not s["enabled"] for s in sources.source_catalog() if s["kind"] in ("boss", "liepin", "linkedin"))
    assert all(s["message"] for s in sources.source_catalog())


def test_board_slug_cannot_inject_paths_or_queries():
    with pytest.raises(sources.SourceError):
        sources.fetch_source({"kind": "greenhouse", "board": "../private?test=1"})


def test_pay_excerpt_preserves_currency_and_no_inferred_interval():
    excerpt = sources._salary_excerpt("Responsibilities\nBuild models\nThe US\nbase\nsalary range is between $200,000 - $400,000\nOther benefits vary.")
    assert "$200,000 - $400,000" in excerpt
    assert "monthly" not in excerpt and "annual" not in excerpt


def test_external_data_cannot_turn_script_into_job_link():
    assert sources._base_job(source_url="javascript:alert(1)")["source_url"] == ""


def test_greenhouse_heading_sections_keep_required_and_preferred_separate():
    markup = """<p>Company founded 15 years ago.</p><h2>Responsibilities</h2>
    <ul><li>Develop VLM perception models.</li></ul><h2>Requirements</h2>
    <ul><li>5+ years of production ML experience.</li><li>Bachelor's degree or equivalent experience.</li></ul>
    <h2>Preferred Qualifications</h2><ul><li>Master's degree or PhD preferred.</li></ul>
    <h2>Benefits</h2><p>Annual bonus and employee learning stipend.</p>"""
    fields = sources._greenhouse_sections(markup)
    assert "Develop VLM" in fields["responsibilities"]
    assert "Company founded" not in fields["responsibilities"]
    assert "Requirements" not in fields["responsibilities"]
    assert "Preferred Qualifications" in fields["requirements"]
    assert "Annual bonus" not in fields["requirements"]
    assert fields["experience"] == "5+ years of production ML experience."
    assert fields["education"] == "Bachelor's degree or equivalent experience.\nMaster's degree or PhD preferred."


def test_greenhouse_inline_bold_qualifiers_remain_in_original_sentences():
    markup = "<p><strong>Responsibilities:</strong> Build and deploy models.</p><p><strong>Qualifications</strong></p><p><strong>Master's</strong> degree preferred, or equivalent experience.</p><p>Minimum 5+ years of relevant experience.</p><h3>About our company</h3><p>Company history.</p>"
    fields = sources._greenhouse_sections(markup)
    assert "Build and deploy models." in fields["responsibilities"]
    assert fields["education"] == "Master's degree preferred, or equivalent experience."
    assert fields["experience"] == "Minimum 5+ years of relevant experience."
    assert "Company history" not in fields["requirements"]


def test_greenhouse_alternative_degree_and_year_conditions_are_not_simplified():
    text = "Requirements:\nBachelor's degree with 8+ years of experience, or Master's degree with 5+ years of experience.\nBonus Qualifications:\nPhD preferred.\nCompensation:\nSalary negotiable."
    fields = sources._greenhouse_sections(text)
    assert fields["experience"] == "Bachelor's degree with 8+ years of experience, or Master's degree with 5+ years of experience."
    assert "PhD preferred." in fields["education"]
    assert "Salary negotiable" not in fields["requirements"]


def test_greenhouse_unsupported_background_does_not_invent_requirements():
    fields = sources._greenhouse_sections("<p>Our founder completed a PhD. We were founded 10 years ago.</p><p>We develop perception models.</p>")
    assert fields == {"responsibilities": "", "requirements": "", "experience": "", "education": ""}
    assert sources._greenhouse_sections("Requirements:\nMaster C++ profiling and real-time debugging.")["education"] == ""


def test_greenhouse_chinese_requirement_evidence_and_full_jd_retained():
    markup = "<h2>公司介绍</h2><p>机器人研发团队。</p><h2>岗位职责</h2><p>开发视觉模型。</p><h2>任职要求</h2><p>5年以上算法工作经验。</p><p>硕士及以上学历，或同等研发经历。</p><h2>加分项</h2><p>具身智能项目经验。</p>"
    payload = {"jobs": [{"title": "视觉研发", "location": {"name": "深圳"}, "content": markup, "absolute_url": "https://example.com/job"}]}
    with patch.object(sources, "safe_fetch", return_value=response(payload)):
        job = sources.fetch_source({"kind": "greenhouse", "board": "fixture", "company": "测试公司"})[0]
    assert job["experience"] == "5年以上算法工作经验。"
    assert job["education"] == "硕士及以上学历，或同等研发经历。"
    assert "加分项" in job["requirements"]
    assert "机器人研发团队。" in job["raw_text"]
    assert "机器人研发团队。" not in job["responsibilities"]
