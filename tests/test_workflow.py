"""End-to-end backend acceptance using isolated, conspicuously labelled test data.

No real database or network service is touched. Background work is explicitly
claimed/executed so assertions inspect persisted results, not timing assumptions.
"""
import copy
import os
from datetime import datetime
from importlib import import_module
from io import BytesIO
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient


PASSWORD = "test-only-password-123456"
JD = """公司：测试夹具视觉公司
岗位：计算机视觉算法工程师
城市：深圳
薪资：30–60K·14薪
工作经验：5-10年
学历：本科及以上
岗位职责
负责图像分割、模型训练和模型部署。
任职要求
有图像分割、Python 与 PyTorch 的实践经验。
"""


@pytest.fixture
def desk(tmp_path, monkeypatch):
    # app.main creates a default app at import time. Set DATA_DIR before import
    # so even that bootstrap instance writes only inside pytest's temporary tree.
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "test_bootstrap"))
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("COOKIE_SECURE", "false")
    for key in ("AI_API_KEY", "OPENAI_API_KEY", "SMTP_HOST", "SMTP_FROM", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    main = import_module("app.main")
    worker_module = import_module("app.worker")

    def deny_network(*args, **kwargs):
        raise RuntimeError("测试夹具禁止外网：链接保留供人工核对")

    monkeypatch.setattr(main, "safe_fetch", deny_network)
    monkeypatch.setattr(worker_module, "fetch_source", deny_network)
    app = main.create_app(data_dir=tmp_path / "test_data", start_worker=False)
    with TestClient(app) as client:
        response = client.post("/api/login", json={"password": PASSWORD})
        assert response.status_code == 200, response.text
        client.headers["x-csrf-token"] = response.json()["csrf_token"]
        yield SimpleNamespace(app=app, client=client, store=app.state.store,
                              worker=app.state.worker, main=main,
                              worker_module=worker_module, path=tmp_path / "test_data")


def import_job(desk, text=JD, urls="", files=None):
    response = desk.client.post("/api/import", data={"text": text, "urls": urls}, files=files)
    assert response.status_code == 200, response.text
    return response.json()


def drain(desk):
    count = 0
    while task := desk.store.claim():
        desk.worker.execute(task)
        count += 1
        assert count < 30, "测试任务队列未能结束"
    return count


def confirmed_profile(desk):
    profile = desk.client.get("/api/profile").json()["profile"]
    profile.update(name="测试夹具人员", contact="fixture@example.invalid", confirmed=True,
                   summary="测试夹具：参与医学影像分割模型训练与验证。",
                   experience=["测试夹具单位｜算法工程师｜2020-2026（非真实履历）"],
                   education=["测试夹具教育记录（非真实学历）"],
                   projects=[{"id": "test-project-1", "title": "测试夹具分割项目",
                              "context": "测试夹具背景：医学影像",
                              "bullets": ["本人负责模型训练与模型部署。", "使用固定测试集评估图像分割，不添加未知效果指标。"],
                              "skills": ["分割", "模型训练", "模型部署"], "confirmed": True}],
                   skills=[{"name": "分割", "level": "done", "evidence": "测试夹具分割项目", "confirmed": True},
                           {"name": "多模态微调", "level": "learned", "evidence": "仅为测试夹具学习记录", "confirmed": True}])
    response = desk.client.put("/api/profile", json=profile)
    assert response.status_code == 200, response.text
    return response.json()["profile"]


def test_pasted_jd_can_be_corrected_salary_and_unknown_dates(desk):
    created = import_job(desk)["job"]
    assert created["salary_group"] == "可能符合"
    assert created["salary_detail"]["min_monthly"] == 30000
    assert created["salary_detail"]["extra_months"] == 2
    assert not created["published_at"] and created["first_seen"]
    assert created["status_validity"] == "未知"
    assert "title" in created["uncertain_fields"]
    response = desk.client.patch(f"/api/jobs/{created['id']}", json={
        "title": "用户已核对的测试视觉算法工程师", "note": "测试备注", "next_action": "核实薪资构成"})
    assert response.status_code == 200, response.text
    saved = desk.client.get(f"/api/jobs/{created['id']}").json()["job"]
    assert saved["title"] == "用户已核对的测试视觉算法工程师"
    assert "title" not in saved["uncertain_fields"]
    assert saved["note"] == "测试备注" and saved["history"]


@pytest.mark.skipif(os.name != "nt", reason="真实中文 OCR 集成验收需要 Windows 语言包")
def test_two_overlapping_screenshots_create_one_job_and_preserve_originals(desk):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 34)
    pages = [
        ["公司：测试截图公司", "岗位：视觉算法工程师", "城市：深圳", "薪资：30-60K", "岗位职责", "负责模型训练", "负责图像分割"],
        ["岗位职责", "负责模型训练", "负责图像分割", "任职要求", "熟悉 Python", "有模型部署经验"],
    ]
    uploads = []
    originals = []
    for number, lines in enumerate(pages):
        picture = Image.new("RGB", (1000, 620), "white")
        draw = ImageDraw.Draw(picture)
        for row, line in enumerate(lines):
            draw.text((40, 30 + row * 75), line, font=font, fill="black")
        stream = BytesIO()
        picture.save(stream, format="PNG")
        originals.append(stream.getvalue())
        uploads.append(("files", (f"test-screenshot-{number}.png", stream.getvalue(), "image/png")))
    payload = import_job(desk, text="", files=uploads)
    job = payload["job"]
    assert len(desk.store.all("jobs")) == 1
    assert job["title"] == "视觉算法工程师"
    assert job["salary_group"] == "可能符合"
    assert job["raw_text"].replace(" ", "").count("负责模型训练") == 1
    assert "模型部署" in job["requirements"]
    assert len(job["attachments"]) == 2
    for attachment, expected in zip(job["attachments"], originals):
        assert desk.client.get(f"/api/files/{attachment['id']}").content == expected


def test_ready_persists_independent_results_and_retries_only_failed_item(desk, monkeypatch):
    job = import_job(desk)["job"]
    with monkeypatch.context() as patch:
        patch.setattr(desk.worker_module, "report", lambda *args: (_ for _ in ()).throw(RuntimeError("测试夹具报告临时失败")))
        response = desk.client.patch(f"/api/jobs/{job['id']}", json={"status": "可投"})
        assert response.status_code == 200
        assert len(response.json()["tasks"]) == 5
        assert drain(desk) == 5
    tasks = {t["kind"]: t for t in desk.store.tasks_for(job["id"])}
    assert tasks["report"]["status"] == "failed"
    assert "测试夹具" in tasks["report"]["error"]
    assert tasks["resume"]["status"] == "blocked"
    assert "不会生成虚构简历" in tasks["resume"]["error"]
    assert tasks["questions"]["status"] == tasks["plan"]["status"] == "completed"
    assert not desk.store.all("resumes")
    completed = copy.deepcopy(tasks["questions"])
    response = desk.client.post(f"/api/jobs/{job['id']}/tasks/report/retry", json={"language": "zh"})
    assert response.status_code == 200, response.text
    assert drain(desk) == 1
    saved = {t["kind"]: t for t in desk.store.tasks_for(job["id"])}
    assert saved["report"]["status"] == "completed"
    assert saved["report"]["attempts"] == 2
    assert saved["questions"] == completed
    assert desk.store.one("jobs", job["id"])["status"] == "可投"
    assert not desk.store.one("jobs", job["id"])["applied_at"]


def test_confirmed_resume_versions_export_and_do_not_change_base(desk):
    from docx import Document
    from pypdf import PdfReader
    profile = confirmed_profile(desk)
    job = import_job(desk)["job"]
    response = desk.client.post(f"/api/jobs/{job['id']}/prepare", json={"language": "zh"})
    assert response.status_code == 200
    assert drain(desk) == 5
    assert all(t["status"] == "completed" for t in desk.store.tasks_for(job["id"]))
    detail = desk.client.get(f"/api/jobs/{job['id']}").json()
    version = detail["resumes"][0]
    assert version["changes"] and version["blocks"]
    assert "多模态微调" not in version["content"]["skills"]
    docx = desk.client.get(f"/api/resumes/{version['id']}/docx")
    pdf = desk.client.get(f"/api/resumes/{version['id']}/pdf")
    assert docx.status_code == pdf.status_code == 200
    docx_text = "\n".join(p.text for p in Document(BytesIO(docx.content)).paragraphs)
    pdf_text = "\n".join(p.extract_text() for p in PdfReader(BytesIO(pdf.content)).pages)
    for block in version["blocks"]:
        assert block["text"] in docx_text
        assert block["text"] in pdf_text
    assert desk.store.get("profile") == profile
    original = copy.deepcopy(desk.store.one("resumes", version["id"]))
    amended = copy.deepcopy(version["content"])
    amended["summary"] = "测试夹具：用户核对后修订的表达。"
    saved = desk.client.post(f"/api/resumes/{version['id']}/confirm", json={"content": amended})
    assert saved.status_code == 200, saved.text
    new = saved.json()["resume"]
    assert new["id"] != version["id"] and new["parent_id"] == version["id"]
    assert desk.store.one("resumes", version["id"]) == original
    assert desk.store.get("profile") == profile
    assert desk.store.one("jobs", job["id"])["status"] == "可投"


def test_resume_upload_is_draft_and_original_file_is_private(desk):
    before = copy.deepcopy(desk.store.get("profile"))
    content = "姓名：测试上传人员\n邮箱：upload@example.invalid\n项目：测试上传分割项目\n本人参与模型训练。"
    response = desk.client.post("/api/profile/upload", files={"file": ("测试简历.txt", content.encode(), "text/plain")})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["draft"]["name"] == "测试上传人员"
    assert not payload["draft"]["confirmed"]
    assert desk.store.get("profile") == before
    file_id = payload["file"]["id"]
    assert desk.client.get(f"/api/files/{file_id}").content == content.encode()
    with TestClient(desk.app) as anonymous:
        assert anonymous.get(f"/api/files/{file_id}").status_code == 401
        assert anonymous.get("/api/resumes/fake/pdf").status_code == 401
        assert anonymous.get("/api/profile").status_code == 401
        assert anonymous.get("/data/workbench.sqlite3").status_code == 404


def test_auth_csrf_and_cross_origin_write_protection(desk):
    assert desk.client.get("/api/session").json()["authenticated"]
    csrf = desk.client.headers.pop("x-csrf-token")
    response = desk.client.put("/api/profile", json=desk.store.get("profile"))
    assert response.status_code == 403
    desk.client.headers["x-csrf-token"] = csrf
    response = desk.client.put("/api/profile", json=desk.store.get("profile"), headers={"origin": "https://untrusted.invalid"})
    assert response.status_code == 403


def test_refresh_and_new_app_keep_status_files_and_task_results(desk):
    from app.store import Store
    job = import_job(desk, files=[("files", ("test-jd.txt", b"TEST FIXTURE ORIGINAL", "text/plain"))])["job"]
    response = desk.client.patch(f"/api/jobs/{job['id']}", json={"status": "可投", "favorite": True, "note": "持久化测试"})
    assert response.status_code == 200
    drain(desk)
    reopened = Store(desk.path)
    assert reopened.one("jobs", job["id"])["note"] == "持久化测试"
    assert reopened.one("jobs", job["id"])["favorite"]
    assert len(reopened.tasks_for(job["id"])) == 5
    restarted = desk.main.create_app(data_dir=desk.path, start_worker=False)
    with TestClient(restarted) as client:
        auth = client.post("/api/login", json={"password": PASSWORD})
        assert auth.status_code == 200
        refreshed = client.get(f"/api/jobs/{job['id']}").json()
        assert refreshed["job"]["status"] == "可投"
        assert len(refreshed["tasks"]) == 5
        file_id = refreshed["job"]["attachments"][0]["id"]
        assert client.get(f"/api/files/{file_id}").content == b"TEST FIXTURE ORIGINAL"


def test_cross_source_merge_preserves_links_and_failed_fetch_does_not_close(desk):
    first = import_job(desk, urls="https://source-a.invalid/jobs/test?utm_source=fixture")
    second = import_job(desk, urls="https://source-b.invalid/jobs/test")
    assert first["job"]["id"] == second["job"]["id"]
    assert second["merged"]
    assert len(desk.store.all("jobs")) == 1
    assert {s["url"] for s in second["job"]["sources"]} == {"https://source-a.invalid/jobs/test", "https://source-b.invalid/jobs/test"}
    assert second["warnings"] and second["job"]["status_validity"] == "未知"
    assert not second["job"]["last_verified"]


def test_merge_keeps_each_original_submission_for_audit(desk):
    first = import_job(desk, urls="https://source-a.invalid/jobs/test")
    second_text = JD + "\n测试来源二补充：部署环境待沟通核实。"
    second = import_job(desk, text=second_text, urls="https://source-b.invalid/jobs/test")
    assert second["job"]["id"] == first["job"]["id"]
    originals = second["job"].get("original_inputs", [])
    assert any(item.get("text") == JD for item in originals)
    assert any(item.get("text") == second_text for item in originals)


def test_sample_and_real_same_identity_do_not_merge(desk):
    from app.parsing import parse_job
    sample = parse_job(JD)
    sample.update(raw_text=JD, is_sample=True, sources=[{"url": "https://fixture.invalid/job/one", "name": "样例"}])
    sample, is_new = desk.store.ingest(sample)
    assert is_new
    real = parse_job(JD)
    real.update(raw_text=JD, is_sample=False, sources=[{"url": "https://fixture.invalid/job/one", "name": "测试真实来源通道"}])
    real, is_new = desk.store.ingest(real)
    assert is_new and real["id"] != sample["id"]
    assert desk.store.one("jobs", sample["id"])["is_sample"]
    assert not desk.store.one("jobs", real["id"])["is_sample"]


def test_digest_and_email_are_not_repeated_and_sample_is_excluded(desk, monkeypatch):
    job = import_job(desk)["job"]
    sample = copy.deepcopy(job)
    sample.update(company="样例隔离测试公司", title="样例视觉算法工程师", is_sample=True, raw_text="明确标记的样例数据，不能进入日报", sources=[])
    sample, _ = desk.store.ingest(sample)
    settings = desk.store.get("settings")
    settings.update(email_enabled=True, email_to="fixture@example.invalid")
    desk.store.set("settings", settings)
    sent = []
    monkeypatch.setattr(desk.worker_module.notifications, "configured", lambda: True)
    monkeypatch.setattr(desk.worker_module.notifications, "deliver", lambda digest, jobs, settings: sent.append([j["id"] for j in jobs]))
    first = desk.worker.build_digest({"id": "test-run-one"})
    second = desk.worker.build_digest({"id": "test-run-two"})
    assert first["job_ids"] == [job["id"]]
    assert first["email_status"] == "sent"
    assert second["job_ids"] == []
    assert sample["id"] not in first["job_ids"]
    assert sent == [[job["id"]]]
    from app.store import Store
    from app.worker import Worker
    restarted = Worker(Store(desk.path))
    assert restarted.build_digest({"id": "test-run-three"})["job_ids"] == []


def test_scheduler_day_survives_restart_and_same_day_tick(desk):
    from app.store import Store
    from app.worker import Worker
    before = datetime(2026, 9, 27, 8, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
    due = datetime(2026, 9, 27, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    desk.worker.scheduled_tick(before)
    assert not desk.store.all("runs")
    desk.worker.scheduled_tick(due)
    assert len(desk.store.all("runs")) == 1
    assert desk.store.get("schedule_day") == "2026-09-27"
    # Mark the existing run done; a fresh worker must still not re-enqueue it.
    run = desk.store.all("runs")[0]
    run["status"] = "completed"
    desk.store.put("runs", run)
    restarted = Worker(Store(desk.path))
    restarted.scheduled_tick(datetime(2026, 9, 27, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert len(desk.store.all("runs")) == 1
    restarted.scheduled_tick(datetime(2026, 9, 28, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert len(desk.store.all("runs")) == 2


def test_interview_uses_previous_answer_and_can_resume(desk):
    profile = confirmed_profile(desk)
    job = import_job(desk)["job"]
    started = desk.client.post("/api/interviews", json={"job_id": job["id"], "language": "zh", "type": "项目深挖"})
    assert started.status_code == 200, started.text
    interview = started.json()["interview"]
    assert len(interview["messages"]) == 1
    first_answer = "我负责模型训练，但当时尚未完成独立测试。"
    answered = desk.client.post(f"/api/interviews/{interview['id']}/answer", json={"answer": first_answer})
    assert answered.status_code == 200
    next_turn = answered.json()["interview"]["messages"][-1]
    assert first_answer in next_turn["content"] and "基线" in next_turn["content"]
    resumed = desk.client.get(f"/api/interviews/{interview['id']}").json()["interview"]
    assert len(resumed["messages"]) == 3
    second_answer = "团队评估了100例测试样本。"
    answered = desk.client.post(f"/api/interviews/{interview['id']}/answer", json={"answer": second_answer})
    next_turn = answered.json()["interview"]["messages"][-1]
    assert second_answer in next_turn["content"] and "本人负责" in next_turn["content"]
    finished = desk.client.post(f"/api/interviews/{interview['id']}/finish")
    assert finished.status_code == 200
    result = finished.json()["interview"]
    assert result["status"] == "completed" and len(result["feedback"]["rounds"]) == 2
    assert result["feedback"]["revision_list"]
    improvement = result["feedback"]["improved_answers"][0]["answer"]
    assert profile["projects"][0]["bullets"][0] in improvement
    assert "100" in result["feedback"]["unverified_numbers"]
