"""Offline end-to-end milestone contracts; no live credentials or personal DB."""
import copy
import pytest
from app.source_catalog import company_catalog, merge_catalog
from app.constraints import classify_constraints
from app.profile_merge import merge_profile
from app.store import source_key
from test_workflow import desk, import_job, confirmed_profile, drain


def test_catalog_append_only_idempotent_and_bounded():
    catalog=company_catalog()
    assert len(catalog)==11 and len({s['id'] for s in catalog})==11
    assert all(not s['enabled'] and s['verified_at']=='2026-10-01' for s in catalog)
    assert sum(s['ingestion_status']=='portal_pending' for s in catalog)==9
    existing=[dict(catalog[0],enabled=True,url='https://custom.example/jobs',note='keep',last_attempt='old')]
    snapshot=copy.deepcopy(existing)
    merged,added=merge_catalog(existing,[s['id'] for s in catalog])
    assert existing==snapshot and merged[0]==snapshot[0] and len(added)==10
    assert merge_catalog(merged,[s['id'] for s in catalog])==(merged,[])
    with pytest.raises(ValueError): merge_catalog(existing,['not-a-source'])
    with pytest.raises(ValueError): merge_catalog(existing,['tencent','tencent'])
    with pytest.raises(ValueError): merge_catalog([{'id':str(i)} for i in range(30)],['tencent'])


def test_authenticated_catalog_merge_preserves_configuration_and_is_idempotent(desk):
    before=desk.client.get('/api/settings').json()
    before['sources'][0].update(enabled=False,name='我的来源',custom_note='保留')
    assert desk.client.put('/api/settings',json=before).status_code==200
    current=desk.store.get('settings')
    ids=[s['id'] for s in company_catalog()]
    first=desk.client.post('/api/sources/catalog/merge',json={'ids':ids})
    assert first.status_code==200 and len(first.json()['added_ids'])==11
    assert first.json()['settings']['sources'][:len(current['sources'])]==current['sources']
    assert desk.client.post('/api/sources/catalog/merge',json={'ids':ids}).json()['added_ids']==[]
    assert desk.client.post('/api/sources/catalog/merge',json={'ids':['unknown']}).status_code==422
    assert desk.client.get('/api/sources/catalog').json()['version']=='2026-10-01'


@pytest.mark.parametrize('city,salary,code',[
    ('深圳','固定月薪40K','meets'),('Shenzhen, China','40-60K·14薪','meets'),
    ('深圳','30-60K·14薪','possible'),('深圳','60-90万/年','unknown'),
    ('深圳','未知','unknown'),('未知','固定月薪40K','unknown'),
    ('Remote','40K','unknown'),('北京','50K','other_city'),('深圳','20-30K','below')])
def test_constraints_separate_confirmed_possible_unknown_and_other(city,salary,code):
    result=classify_constraints({'city':city,'salary_raw':salary},{'preferred_city':'深圳','salary_target':40000})
    assert result['code']==code
    assert result['evidence']['salary_raw']==salary


def test_listing_roles_do_not_collapse_and_refresh_preserves_user_state(desk):
    def role(title,identity):
        return {'company':'测试静态官网','title':title,'city':'深圳','raw_text':title+' 任职要求：视觉算法 PyTorch',
                'sources':[{'url':'https://example.com/careers','source_identity':identity}],
                'ingest_mode':'source_refresh','salary_raw':'未知'}
    a,new=desk.store.ingest(role('视觉算法工程师','vision')); assert new
    b,new=desk.store.ingest(role('训练算法工程师','training')); assert new and a['id']!=b['id']
    desk.store.update_job(a['id'],lambda j:j.update(status='感兴趣',note='保留人工备注',favorite=True))
    refreshed,new=desk.store.ingest(role('视觉算法工程师','vision'))
    assert not new and refreshed['id']==a['id'] and refreshed['favorite'] and refreshed['note']=='保留人工备注'
    assert len(desk.store.all('jobs'))==2
    assert source_key({'url':'https://example.com/job'})=='https://example.com/job'
    assert source_key(a['sources'][0])!=source_key(b['sources'][0])


def test_unknown_salary_stays_in_investigation_and_failure_does_not_close(desk,monkeypatch):
    job={'company':'测试官网','title':'视觉算法工程师','city':'深圳','salary_raw':'未知',
         'responsibilities':'图像分割、模型训练和模型部署','requirements':'PyTorch 视觉算法',
         'source_url':'https://example.com/careers','source_identity':'vision','published_at':None}
    monkeypatch.setattr(desk.worker_module,'fetch_source',lambda source:[copy.deepcopy(job)])
    run=desk.worker.discover(desk.worker.request_discovery())
    assert run['new_jobs']==1 and run['recommended']==1
    rows=desk.client.get('/api/dashboard').json()['recommendations']
    assert len(rows)==1 and rows[0]['constraint_fit']['code']=='unknown'
    assert not rows[0]['published_at']
    monkeypatch.setattr(desk.worker_module,'fetch_source',lambda source:(_ for _ in ()).throw(ValueError('结构变化')))
    failed=desk.worker.discover(desk.worker.request_discovery())
    assert failed['status']=='failed'
    assert desk.store.all('jobs')[0]['status_validity']!='已关闭'


def test_three_way_profile_merge_preserves_new_changes_and_reports_conflicts():
    base={'name':'旧','skills':[{'name':'Python','confirmed':True}], 'projects':[{'id':'p','title':'旧项目'}]}
    current=copy.deepcopy(base);current['projects'][0]['title']='刚核对的项目';current['skills'].append({'name':'C++','confirmed':True})
    reviewed=copy.deepcopy(base);reviewed['name']='新'
    merged,conflicts=merge_profile(base,current,reviewed)
    assert not conflicts and merged['projects']==current['projects'] and merged['skills']==current['skills']
    reviewed['projects'][0]['title']='旧草稿改动'
    merged,conflicts=merge_profile(base,current,reviewed)
    assert conflicts==['projects.p'] and merged['projects']==current['projects']


def test_profile_draft_accept_preserves_intervening_project_edit(desk):
    confirmed_profile(desk)
    upload=desk.client.post('/api/profile/upload',files={'file':('resume.txt','姓名：测试更新姓名\n'.encode(),'text/plain')}).json()
    current=desk.client.get('/api/profile').json()['profile']
    current['projects'][0]['title']='上传之后核实的新标题'
    assert desk.client.put('/api/profile',json=current).status_code==200
    draft=upload['draft'];draft['confirmed']=True
    response=desk.client.post('/api/profile/draft/accept',json={'draft_id':upload['id'],'profile':draft})
    assert response.status_code==200, response.text
    assert response.json()['profile']['projects'][0]['title']=='上传之后核实的新标题'
    assert desk.client.post('/api/profile/draft/accept',json={'draft_id':upload['id'],'profile':draft}).status_code==409


def test_profile_draft_conflict_does_not_save_partial_changes(desk):
    upload=desk.client.post('/api/profile/upload',files={'file':('resume.txt','姓名：草稿姓名\n'.encode(),'text/plain')}).json()
    current=desk.client.get('/api/profile').json()['profile'];current['name']='最新确认姓名'
    desk.client.put('/api/profile',json=current)
    response=desk.client.post('/api/profile/draft/accept',json={'draft_id':upload['id'],'profile':upload['draft']})
    assert response.status_code==409 and desk.store.get('profile')['name']=='最新确认姓名'


def test_each_resume_retains_own_stale_state(desk):
    confirmed_profile(desk)
    job=import_job(desk)['job']
    desk.worker.prepare(job['id']);drain(desk)
    old=desk.store.all('resumes')[0]
    p=desk.store.get('profile');p['summary']='更新后的真实概述';desk.client.put('/api/profile',json=p)
    desk.worker.prepare(job['id']);drain(desk)
    rows=desk.client.get('/api/jobs/'+job['id']).json()['resumes']
    assert len(rows)==2 and next(r for r in rows if r['id']==old['id'])['stale']
    assert not next(r for r in rows if r['id']!=old['id'])['stale']


def test_genuine_career_fixture_to_truthful_materials_workflow(desk,monkeypatch):
    from pathlib import Path
    from app import sources
    fixture=Path(__file__).parent/'fixtures/career_pages/extremevision.html'
    page=sources.FetchResult('https://www.extremevision.com.cn/join-us/',200,{},fixture.read_bytes())
    monkeypatch.setattr(sources,'safe_fetch',lambda *a,**k:page)
    monkeypatch.setattr(desk.worker_module,'fetch_source',sources.fetch_source)
    desk.client.post('/api/sources/catalog/merge',json={'ids':['extremevision']})
    settings=desk.store.get('settings')
    for row in settings['sources']: row['enabled']=row['id']=='extremevision'
    desk.client.put('/api/settings',json=settings)
    run=desk.worker.discover(desk.worker.request_discovery())
    assert run['status']=='completed' and run['fetched']==13
    jobs=desk.client.get('/api/jobs').json()['jobs']
    job=next(j for j in jobs if j['title']=='大模型算法工程师' and j['city']=='深圳')
    assert job['constraint_fit']['code']=='unknown' and not job['published_at']
    assert job['sources'][0]['source_identity'].startswith('extremevision:')
    desk.worker.prepare(job['id']);drain(desk)
    resume_task=next(t for t in desk.store.tasks_for(job['id']) if t['kind']=='resume')
    assert resume_task['status'] in ('blocked','failed') and not desk.store.all('resumes')
    # Only a clearly synthetic, explicitly confirmed test profile permits materials.
    confirmed_profile(desk)
    desk.worker.prepare(job['id']);drain(desk)
    resumes=desk.store.all('resumes')
    assert len(resumes)==1 and resumes[0]['content']['name']=='测试夹具人员'
    content=str(resumes[0]['content'])
    assert '测试夹具' in content and '测试夹具分割项目' in content
    assert desk.store.one('jobs',job['id'])['status']!='已投递'


def test_evidence_rank_uses_coverage_not_partial_perfect_mean():
    from app.constraints import evidence_rank
    sparse={'analysis':{'score':100,'evidence_coverage':{'confirmed':1,'total':5}}}
    broad={'analysis':{'score':80,'evidence_coverage':{'confirmed':4,'total':5}}}
    assert evidence_rank(broad)>evidence_rank(sparse)


def test_profile_merge_never_drops_duplicate_skill_entries_silently():
    base={'skills':[{'name':'Python','evidence':'a'},{'name':'Python','evidence':'b'}]}
    same,conflicts=merge_profile(base,base,base)
    assert same==base and not conflicts
    reviewed={'skills':[{'name':'Python','evidence':'new'}]}
    merged,conflicts=merge_profile(base,base,reviewed)
    assert merged==base and conflicts
