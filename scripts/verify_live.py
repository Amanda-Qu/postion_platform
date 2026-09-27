"""Read-only authentication smoke + an optional explicitly requested discovery run.

Does not display passwords or personal data. All output is counters and statuses.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import httpx
from dotenv import load_dotenv

root=Path(__file__).resolve().parent.parent
parser=argparse.ArgumentParser()
parser.add_argument('--refresh',action='store_true')
args=parser.parse_args()
load_dotenv(root/'.env')
password=os.getenv('APP_PASSWORD') or (root/'data'/'access-password.txt').read_text(encoding='utf-8').strip()
with httpx.Client(base_url='http://127.0.0.1:8765',timeout=20,trust_env=False) as client:
    assert client.get('/api/jobs').status_code==401
    auth=client.post('/api/login',json={'password':password}); auth.raise_for_status()
    client.headers['X-CSRF-Token']=auth.json()['csrf_token']
    before=client.get('/api/dashboard').json()
    if args.refresh:
        assert not before['settings']['email_enabled'], '验收不会触发真实邮件推送'
        result=client.post('/api/discovery/run',json={}); result.raise_for_status()
        run_id=result.json()['run']['id']
        for i in range(90):
            value=client.get('/api/dashboard').json()
            run=next(r for r in value['runs'] if r['id']==run_id)
            if run['status'] not in ('queued','running'): break
            time.sleep(1)
        else: raise RuntimeError('真实来源获取未在90秒内结束')
        assert run['status']=='completed',run
    after=client.get('/api/dashboard').json()
    ids=[j['id'] for j in after['jobs']]
    assert len(ids)==len(set(ids))
    assert not any(j.get('is_sample') for j in after['jobs'])
    output={'health':client.get('/api/health').json(),'job_count':len(ids),'recommended_count':len(after['recommendations']),'ready_count':after['stats']['ready'],'ai_configured':after['settings']['ai_configured'],'smtp_configured':after['settings']['smtp_configured'],'latest_run':{k:after['runs'][0].get(k) for k in ('status','fetched','new_jobs','merged','recommended','error')},'unauthenticated_api_guard':'passed','sample_jobs_in_real_database':0}
    print(json.dumps(output,ensure_ascii=False))
    (root/'test-results').mkdir(exist_ok=True)
    (root/'test-results'/'live-check.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
