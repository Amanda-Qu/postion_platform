import pytest
from app import company_discovery as discovery
from app.sources import SourceError
from test_workflow import desk


def profile():
    return {'name':'秘密姓名','contact':'private@example.com','summary':'secret project',
            'preferred_city':'深圳、北京','skills':[{'name':'Python','confirmed':True,'level':'done'},
            {'name':'CUDA','confirmed':True,'level':'learned'},
            {'name':'秘密姓名','confirmed':True,'level':'done'},
            {'name':'Java','confirmed':False,'level':'done'}]}


def test_query_privacy_closed_vocabulary_and_practiced_only(monkeypatch):
    monkeypatch.delenv('BRAVE_SEARCH_API_KEY',raising=False)
    plan=discovery.preview(profile())
    assert plan['skills']==['Python'] and len(plan['queries'])==2
    assert not plan['configured']
    joined=str(plan['queries'])
    for private in ['秘密姓名','private@example.com','secret project','CUDA','Java']:
        assert private not in joined
    assert not discovery.preview({'skills':[]})['queries']


@pytest.mark.parametrize('url',['http://example.com','https://127.0.0.1','https://localhost/',
    'https://internal.local/a','https://a.internal/','https://foo.linkedin.com/x',
    'https://user:pass@example.com','https://example.com:123/','javascript:alert(1)',
    'https://example.com\\@evil.com','https://example.com/\n'])
def test_reject_unsafe_candidate_links(url):
    assert not discovery.candidate_url(url)


def test_discovery_not_catalog_deduplicates_bounds_and_marks_unknown():
    seen=[]
    def provider(query):
        seen.append(query)
        return [{'url':f'https://new-company-{i}.example/careers?utm_source=x','title':'Unverified'} for i in range(10)]
    result=discovery.discover(discovery.preview(profile()),[{'url':'https://new-company-0.example/careers'}],provider)
    assert len(result['candidates'])==9 and len(seen)==2
    assert all(c['verification']=='unknown' for c in result['candidates'])
    assert all(c['provider']=='Brave Search' and c['query'] and c['discovered_at'] for c in result['candidates'])


def test_provider_failure_is_honest():
    def failure(query): raise SourceError('额度不足')
    result=discovery.discover(discovery.preview(profile()),[],failure)
    assert result['status']=='failed' and result['errors'] and not result['candidates']


def test_api_setup_preview_review_and_repeat(desk,monkeypatch):
    monkeypatch.delenv('BRAVE_SEARCH_API_KEY',raising=False)
    p=desk.store.get('profile'); p.update(profile()); desk.store.set('profile',p)
    plan=desk.client.get('/api/sources/discovery').json()
    assert not plan['configured']
    assert desk.client.post('/api/sources/discovery',json={'query_token':plan['query_token']}).status_code==409
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY','test-placeholder-not-real')
    monkeypatch.setattr(discovery,'search_brave',lambda q:[{'url':'https://newco.example/careers','title':'New Co','description':'claims are unverified'}])
    assert desk.client.post('/api/sources/discovery',json={'query_token':'stale'}).status_code==409
    response=desk.client.post('/api/sources/discovery',json={'query_token':plan['query_token']})
    assert response.status_code==200
    result=response.json(); candidate=result['candidates'][0]
    assert not desk.store.all('jobs')
    assert desk.client.post('/api/sources/discovery',json={'query_token':plan['query_token']}).status_code==429
    body={'id':candidate['id'],'company':'New Co','official_url':'https://newco.example/about','confirmed':False}
    assert desk.client.post('/api/sources/discovery/review',json=body).status_code==422
    body['confirmed']=True
    review=desk.client.post('/api/sources/discovery/review',json=body)
    assert review.status_code==200
    source=review.json()['settings']['sources'][-1]
    assert source['verification']=='user_confirmed' and not source['enabled']
    assert source['kind']=='career_portal' and source['discovery_evidence']['query']
    assert desk.client.post('/api/sources/discovery/review',json=body).status_code==200
    assert sum(s['id']==candidate['id'] for s in desk.store.get('settings')['sources'])==1
    assert 'test-placeholder-not-real' not in desk.client.get('/api/sources/discovery').text


def test_target_directions_are_generic_interests_not_resume_claims():
    plan=discovery.preview({'preferred_city':'深圳','directions':['多模态/VLM','数据闭环','秘密企业的微调项目']})
    assert plan['directions']==['VLM','数据闭环','微调'] and plan['queries']
    assert not plan['skills'] and '秘密' not in str(plan['queries'])


@pytest.mark.parametrize('status',[301,401,403,429,500])
def test_provider_fixed_host_status_no_body_or_key_leak(monkeypatch,status):
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY','secret-test-key')
    seen=[]
    monkeypatch.setattr(discovery,'_validated_target',lambda url:(seen.append(url) or ('api.search.brave.com',['8.8.8.8'])))
    class Response:
        def __init__(self): self.status=status
    class Connection:
        def __init__(self,host,address): assert host=='api.search.brave.com' and address=='8.8.8.8'
        def request(self,method,path,headers):
            assert method=='GET' and path.startswith('/res/v1/web/search?')
            assert 'secret-test-key' not in path and headers['X-Subscription-Token']=='secret-test-key'
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(discovery,'_PinnedHTTPSConnection',Connection)
    with pytest.raises(SourceError) as e: discovery.search_brave('Python 深圳 careers')
    assert str(status) in str(e.value) and 'secret-test-key' not in str(e.value)
    assert seen==[discovery.PROVIDER_URL]


def test_provider_timeout_and_malformed_body(monkeypatch):
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY','test-key')
    monkeypatch.setattr(discovery,'_validated_target',lambda url:('api.search.brave.com',['8.8.8.8']))
    class Connection:
        def __init__(self,*args): pass
        def request(self,*args,**kwargs): raise TimeoutError('internal secret request detail')
        def close(self): pass
    monkeypatch.setattr(discovery,'_PinnedHTTPSConnection',Connection)
    with pytest.raises(SourceError,match='连接失败') as e: discovery.search_brave('Python 深圳')
    assert 'internal secret' not in str(e.value)


def test_new_endpoints_require_auth_and_csrf_no_schedule_change(desk):
    initial=desk.store.get('settings')
    assert desk.client.post('/api/sources/discovery',json={},headers={'x-csrf-token':'wrong'}).status_code==403
    desk.client.cookies.clear()
    assert desk.client.get('/api/sources/discovery').status_code==401
    assert desk.client.post('/api/sources/discovery/review',json={}).status_code==401
    assert desk.store.get('settings')==initial


@pytest.mark.parametrize('body,expected',[(b'{"web":{"results":[{"url":"https://new.example/careers"}]}}',True),
    (b'not-json',False),(b'{"web":{"results":{}}}',False),(b'[]',False),(b'x'*(1024*1024+1),False)])
def test_provider_bounded_json_response(monkeypatch,body,expected):
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY','test-key')
    monkeypatch.setattr(discovery,'_validated_target',lambda url:('api.search.brave.com',['8.8.8.8']))
    from io import BytesIO
    class Response:
        status=200
        def __init__(self): self.body=BytesIO(body)
        def getheader(self,*args): return 'identity'
        def read1(self,count): return self.body.read(count)
    class Connection:
        def __init__(self,*args): pass
        def request(self,*args,**kwargs): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(discovery,'_PinnedHTTPSConnection',Connection)
    if expected:
        assert discovery.search_brave('generic')[0]['url']=='https://new.example/careers'
    else:
        with pytest.raises(SourceError): discovery.search_brave('generic')


def test_simultaneous_requests_do_not_double_charge(desk,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    p=desk.store.get('profile'); p.update(profile()); p['preferred_city']='深圳';desk.store.set('profile',p)
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY','test-key')
    entered,release=threading.Event(),threading.Event()
    calls=[]
    def search(q):
        calls.append(q);entered.set(); assert release.wait(5);return []
    monkeypatch.setattr(discovery,'search_brave',search)
    token=desk.client.get('/api/sources/discovery').json()['query_token']
    with ThreadPoolExecutor() as pool:
        first=pool.submit(desk.client.post,'/api/sources/discovery',json={'query_token':token})
        try:
            assert entered.wait(5)
            assert desk.client.post('/api/sources/discovery',json={'query_token':token}).status_code==409
        finally: release.set()
        assert first.result().status_code==200
    assert len(calls)==1



def test_exact_urls_and_ats_identifiers_survive_discovery_and_review(desk,monkeypatch):
    original='https://ATS.example/careers?tenant=Company42&postId=123&utm_source=search#/jobs/detail'
    assert discovery.candidate_url(original)==original
    assert 'tenant=Company42' in discovery.dedupe_key(original)
    assert 'postId=123' in discovery.dedupe_key(original)
    assert '#/jobs/detail' in discovery.dedupe_key(original)
    result=discovery.discover(discovery.preview(profile()),[],lambda q:[{'url':original}])
    candidate=result['candidates'][0]
    assert candidate['url']==candidate['evidence_url']==original
    desk.store.set('company_discovery_run',result)
    official='https://Company.example/about?language=zh#/careers'
    r=desk.client.post('/api/sources/discovery/review',json={'id':candidate['id'],'company':'Company',
        'official_url':official,'confirmed':True})
    assert r.status_code==200
    source=r.json()['settings']['sources'][-1]
    assert source['url']==original
    assert source['discovery_evidence']['evidence_url']==original
    assert source['official_evidence_url']==official


def test_dedupe_tracking_only_keeps_distinct_tenants_and_jobs():
    urls=['https://ats.example/careers?tenant=A&postId=1&utm_source=one',
          'https://ats.example/careers?utm_source=two&postId=1&tenant=A',
          'https://ats.example/careers?tenant=A&postId=2',
          'https://ats.example/careers?tenant=B&postId=1']
    result=discovery.discover(discovery.preview(profile()),[],lambda q:[{'url':u} for u in urls])
    assert len(result['candidates'])==3
    assert result['candidates'][0]['evidence_url']==urls[0]
    assert discovery.dedupe_key(urls[0])==discovery.dedupe_key(urls[1])
    assert discovery.dedupe_key('https://ats.example/?source=tenant-A')!=discovery.dedupe_key('https://ats.example/?source=tenant-B')


@pytest.mark.parametrize('suffix',['?access_token=secret','?api_key=secret','#token=secret',
    '?%61ccess_token=secret','?%2561ccess_token=secret','/token/secret','?X-Amz-Signature=secret',
    '?password=secret','?code=secret'])
def test_credential_bearing_links_are_rejected_not_redacted(suffix):
    url='https://ats.example/careers'+suffix
    assert discovery.candidate_url(url)==''
    assert discovery.dedupe_key(url)==''
    assert not discovery.discover(discovery.preview(profile()),[],lambda q:[{'url':url}])['candidates']


def test_target_directions_take_priority_over_generic_skills():
    p=profile();p['skills']=[{'name':s,'confirmed':True,'level':'done'} for s in ['Python','C++','Java','JavaScript']]
    p['directions']=['多模态/VLM','数据闭环','微调']
    plan=discovery.preview(p)
    assert plan['queries'][0].startswith('深圳 VLM 数据闭环 微调 Python C++ ')
