"""Reproductions for independently reviewed provenance/concurrency defects.

All network and AI providers here are mocked. These are regression assertions
for required behavior, not live source/model validation.
"""
import asyncio
import copy
import time

import pytest

from app import ai, analysis
from app.store import Store
from app.worker import Worker


def job_input(**updates):
    job = {"company": "测试公司", "title": "视觉算法工程师", "city": "深圳", "salary_raw": "30–60K·14薪", "experience": "5年", "education": "本科", "responsibilities": "负责目标检测算法开发", "requirements": "计算机视觉", "published_at": None, "raw_text": "测试公司 深圳 视觉算法工程师 目标检测", "sources": [], "attachments": []}
    job.update(updates)
    return job


def test_merge_keeps_all_original_inputs_warnings_and_uncertainty(tmp_path):
    store = Store(tmp_path)
    first, _ = store.ingest(job_input(original_inputs=[{"text": "第一份完整输入"}], import_warnings=["警告一"], uncertain_fields=["title"]))
    merged, new = store.ingest(job_input(original_inputs=[{"text": "第二份完整输入"}], import_warnings=["警告二"], uncertain_fields=["salary_raw"]))
    assert not new and merged["id"] == first["id"]
    assert merged["original_inputs"] == [{"text": "第一份完整输入"}, {"text": "第二份完整输入"}]
    assert set(merged["import_warnings"]) == {"警告一", "警告二"}
    assert set(merged["uncertain_fields"]) == {"title", "salary_raw"}


def test_merge_can_fill_unknown_field_from_new_evidence(tmp_path):
    store = Store(tmp_path)
    store.ingest(job_input(salary_raw="未知"))
    merged, _ = store.ingest(job_input(salary_raw="30–60K·14薪"))
    assert merged["salary_raw"] == "30–60K·14薪"


def test_unknown_sentinels_do_not_count_as_complete_information(tmp_path):
    profile = Store(tmp_path).get("profile")
    job = job_input(id="incomplete")
    for field in ("company", "title", "city", "salary_raw", "experience", "education", "responsibilities", "requirements"):
        job[field] = "未知"
    result = analysis.match_job(job, profile)
    assert result["information_completeness"] == 0


def test_background_report_does_not_revert_newer_user_workflow_changes(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job, _ = store.ingest(job_input(status="可投"))
    task = store.enqueue(job["id"], "report")

    def delayed_report(stale_job, profile, settings):
        # Reproduce the interleaving during an external model request without
        # timing flakiness: the user updates the live record before it returns.
        latest = store.one("jobs", stale_job["id"])
        latest.update(status="已投递", note="已与 HR 沟通，周五跟进", applied_at="2026-09-27")
        store.save_job(latest)
        return {"engine": "test", "summary": "分析完成"}

    monkeypatch.setattr("app.worker.report", delayed_report)
    Worker(store).execute(task)
    latest = store.one("jobs", job["id"])
    assert latest["status"] == "已投递"
    assert latest["note"] == "已与 HR 沟通，周五跟进"
    assert latest["applied_at"] == "2026-09-27"


def test_ai_fact_rejects_unconfirmed_personal_evidence(tmp_path, monkeypatch):
    profile = Store(tmp_path).get("profile")
    assert profile["skills"][0]["confirmed"] is False
    result = {"summary": "测试输出", "claims": [{"conclusion": "候选人已确认具备生产级检测实践", "kind": "明确事实", "job_quote": "", "evidence_refs": ["skill:0"]}], "missing_questions": []}
    monkeypatch.setattr(ai, "call_json", lambda *args: result)
    with pytest.raises(ValueError):
        analysis.report(job_input(id="job"), profile, {"use_ai": True})


def test_summary_aspiration_does_not_promote_learned_skill_to_practice(tmp_path):
    profile = Store(tmp_path).get("profile")
    profile.update(confirmed=True, summary="希望向多模态方向发展，尚未做过相关项目。", skills=[{"name": "多模态微调", "level": "learned", "evidence": "仅学习教程，未做生产项目", "confirmed": True}], projects=[])
    job = job_input(id="job", title="VLM工程师", requirements="多模态模型", responsibilities="研发多模态模型", salary_raw="40–60K")
    result = analysis.match_job(job, profile)
    dimension = next(d for d in result["dimensions"] if d["requirement"] == "多模态微调")
    assert dimension["match"] != "有已确认实践证据"
    assert dimension["score"] != 100
    assert result["recommendation"] != "优先投递"


def test_lever_slug_change_clears_old_connected_status(tmp_path):
    from app.main import create_app

    app = create_app(tmp_path, start_worker=False)
    store = app.state.store
    settings = store.get("settings")
    settings["sources"] = [{"id": "lever-one", "kind": "lever", "name": "Lever", "enabled": True, "board": "", "url": "", "site": "palantir", "company": "Palantir Technologies", "status": "已连接", "message": "旧来源成功"}]
    store.set("settings", settings)
    changed = copy.deepcopy(settings)
    changed["sources"][0]["site"] = "does-not-exist"
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/settings" and "PUT" in route.methods)
    result = endpoint(changed)
    assert result["sources"][0]["status"] == "需要配置"


def test_link_import_does_not_block_async_event_loop(tmp_path, monkeypatch):
    import app.main as main
    from app.sources import FetchResult

    app = main.create_app(tmp_path, start_worker=False)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/import")

    def slow_fetch(url):
        time.sleep(0.35)
        return FetchResult(url, 200, {}, "测试公司\n职位：视觉算法工程师\n城市：深圳".encode())

    monkeypatch.setattr(main, "safe_fetch", slow_fetch)

    async def run():
        started = time.monotonic()

        async def heartbeat():
            await asyncio.sleep(0.02)
            return time.monotonic() - started

        elapsed, _ = await asyncio.gather(heartbeat(), endpoint(text="测试公司", urls="https://example.com/job", files=[]))
        return elapsed

    assert asyncio.run(run()) < 0.20
