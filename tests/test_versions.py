"""Snapshot and refresh regressions: saved preparation must not silently go stale."""
from app.store import Store
from app.worker import Worker
from app.analysis import learning_plan
from app.parsing import parse_job

def test_source_refresh_preserves_human_corrections_and_updates_other_fields(tmp_path):
    store=Store(tmp_path)
    fixture={'company':'验收样例公司','title':'视觉算法工程师','city':'深圳','salary_raw':'30-60K','responsibilities':'旧职责','requirements':'检测','raw_text':'fixture only','sources':[{'url':'https://example.com/careers/1','name':'公开样例'}]}
    original,_=store.ingest(fixture.copy())
    store.update_job(original['id'],lambda j:j.update(salary_raw='固定月薪40-60K',human_edited_fields=['salary_raw']))
    refreshed,new=store.ingest({**fixture,'salary_raw':'35-60K','responsibilities':'新职责','ingest_mode':'source_refresh'})
    assert not new
    assert refreshed['salary_raw']=='固定月薪40-60K'
    assert refreshed['responsibilities']=='新职责'
    assert any(x['field']=='salary_raw' for x in refreshed['conflicts'])

def test_changed_profile_requeues_materials_but_retains_last_result(tmp_path):
    store=Store(tmp_path); worker=Worker(store)
    job,_=store.ingest({'company':'验收样例','title':'视觉算法','city':'深圳','raw_text':'检测算法','requirements':'检测'})
    worker.prepare(job['id'])
    while task:=store.claim(): worker.execute(task)
    old=next(t for t in store.tasks_for(job['id']) if t['kind']=='report')
    assert old['status']=='completed'
    same=worker.prepare(job['id'])
    assert next(t for t in same if t['kind']=='report')['status']=='completed'
    store.set('profile_version',1)
    changed=worker.prepare(job['id'])
    report=next(t for t in changed if t['kind']=='report')
    assert report['status']=='queued'
    assert report['result']==old['result']

def test_bonus_requirement_does_not_become_mandatory_course(tmp_path):
    store=Store(tmp_path)
    job={'id':'sample','title':'算法工程师','requirements':'Requirements:\nPython\nBonus Qualifications:\nMIL','responsibilities':'模型训练','city':'深圳'}
    plan=learning_plan(job,store.get('profile'))
    mil=next(x for x in plan['items'] if x['topic']=='MIL')
    assert mil['priority']=='有帮助但不必优先'

def test_explicit_publication_date_normalizes_without_guessing():
    assert parse_job('发布日期：2026年9月27日')['published_at']=='2026-09-27'
    assert parse_job('发布时间：2026/9/27')['published_at']=='2026-09-27'
    invalid=parse_job('发布时间：2026-99-27')
    assert invalid['published_at']==''
    assert 'published_at' in invalid['uncertain_fields']
