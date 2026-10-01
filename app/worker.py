"""Persistent queue and Beijing-time scheduler independent from browser lifetime."""
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from . import ai, notifications
from .analysis import report, questions, learning_plan, stories, resume_version, match_job, tags
from .parsing import salary_group
from .constraints import classify_constraints, evidence_rank
from .sources import fetch_source, same_source_config
from .store import now, uid, dump, job_signature

KINDS=('report','resume','questions','plan','stories')

class Worker:
    def __init__(self, store):
        self.store=store
        self.stop_event=threading.Event()
        self.wake=threading.Event()
        self.thread=None
        self.discovery_lock=threading.Lock()

    def start(self):
        self.store.recover()
        self.thread=threading.Thread(target=self.loop,name='career-worker',daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set(); self.wake.set()
        if self.thread: self.thread.join(timeout=3)

    def prepare(self, job_id, language='zh'):
        job=self.store.one('jobs',job_id)
        old={t['kind']:t for t in self.store.tasks_for(job_id) if t['language']==language}
        tasks=[]
        for kind in KINDS:
            previous=old.get(kind)
            stale=previous and (previous.get('profile_version')!=self.store.get('profile_version',0) or previous.get('job_signature')!=job_signature(job))
            tasks.append(self.store.enqueue(job_id,kind,language,retry=bool(stale)))
        self.wake.set()
        return tasks

    def request_discovery(self, trigger='manual'):
        with self.store.connect(immediate=True) as db:
            rows=db.execute('SELECT data FROM runs').fetchall()
            import json
            active=next((json.loads(r[0]) for r in rows if json.loads(r[0])['status'] in ('queued','running')),None)
            if active: return active
            run={'id':uid(),'status':'queued','trigger':trigger,'started_at':now(),'finished_at':'','error':'','results':[],'fetched':0,'new_jobs':0,'merged':0,'recommended':0}
            db.execute('INSERT INTO runs VALUES (?,?)',(run['id'],dump(run)))
        self.wake.set()
        return run

    def scheduled_tick(self, current=None):
        settings=self.store.get('settings')
        if not settings.get('auto_discovery'): return
        local=current or datetime.now(ZoneInfo('Asia/Shanghai'))
        key=local.date().isoformat()
        # A persistent day marker prevents reload/restart from triggering a second
        # scheduled run. At startup after 09:00, run once as a same-day catch-up.
        if local.strftime('%H:%M')>=settings['schedule_time'] and self.store.get('schedule_day')!=key:
            with self.store.connect(immediate=True) as db:
                row=db.execute("SELECT value FROM kv WHERE key='schedule_day'").fetchone()
                if row and row[0]==dump(key): return
                db.execute("INSERT INTO kv VALUES ('schedule_day',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(dump(key),))
            self.request_discovery('schedule')

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.scheduled_tick()
                task=self.store.claim()
                if task:
                    self.execute(task)
                    continue
                run=next((r for r in self.store.all('runs') if r['status']=='queued'),None)
                if run:
                    self.discover(run)
                    continue
            except Exception:
                logging.exception('Worker iteration failed')
            self.wake.wait(2)
            self.wake.clear()

    def execute(self, task):
        try:
            job=self.store.one('jobs',task['job_id'])
            if not job: raise ValueError('岗位不存在')
            profile=self.store.get('profile'); settings=self.store.get('settings')
            profile_version=self.store.get('profile_version',0)
            kind=task['kind']
            if kind=='report':
                result=report(job,profile,settings)
                self.store.attach_analysis(job['id'],result,job,profile_version)
            elif kind=='resume':
                result=resume_version(job,profile,settings,task['language'])
                self.store.save_resume(result)
                result={'resume_id':result['id'],'language':result['language'],'requires_review':result['requires_review'],'missing_questions':result['missing_questions'],'engine':result['engine']}
            elif kind=='questions': result={'engine':'按岗位证据整理','items':questions(job,profile,match_job(job,profile)['dimensions'])}
            elif kind=='plan': result=learning_plan(job,profile)
            elif kind=='stories': result=stories(job,profile)
            else: raise ValueError('未知任务类型')
            task.update(status='completed',result=result,error='',profile_version=profile_version,job_signature=job_signature(job))
        except ai.NeedsConfiguration as exc:
            task.update(status='blocked',error=str(exc))
        except Exception as exc:
            task.update(status='failed',error=str(exc)[:500])
        self.store.finish_task(task)

    def relevant(self, job, profile):
        text=' '.join(job.get(k,'') or '' for k in ('title','responsibilities','requirements'))
        recognized=set(tags(text))
        wanted=set(tags(' '.join(profile.get('directions',[]))+' '+ ' '.join(s['name'] for s in profile.get('skills',[]))))
        title=job.get('title','').lower()
        focused_role=any(x in title for x in ['machine learning','deep learning','computer vision','vision-language','multimodal','perception','robot learning','reinforcement learning','state estimation','localization','mapping','inference','pretraining','training performance','training infrastructure','data infrastructure','modeling','generative ai','algorithm','算法','研究','视觉','多模态','数据闭环','训练','推理','定位'])
        # Company boilerplate often says "robotics" in every listing, including
        # wiring and finance. Require an algorithm/training/data/deployment signal
        # in the actual title as well as an overlap with the editable profile.
        return focused_role and bool((recognized & wanted)-{'Python','C++'})

    def discover(self, run):
        run.update(status='running'); self.store.put('runs',run)
        settings=self.store.get('settings'); profile=self.store.get('profile')
        enabled=[s for s in settings.get('sources',[]) if s.get('enabled')]
        for source in enabled:
            try:
                fetched=fetch_source(source)
                relevant=[j for j in fetched if self.relevant(j,profile)]
                new_count=0; merged=0
                for job in relevant:
                    job['sources']=[{'url':job.pop('source_url',''),'name':job.pop('source_name',source['name']),'fetched_at':now(), 'source_identity':job.pop('source_identity','')}]
                    job['last_verified']=now()
                    job['ingest_mode']='source_refresh'
                    job['salary_detail']=salary_group(job.get('salary_raw',''),profile['salary_target'])
                    job['salary_group']=job['salary_detail']['group']
                    saved,new=self.store.ingest(job)
                    self.store.attach_analysis(saved['id'],match_job(saved,profile),saved,self.store.get('profile_version',0))
                    new_count+=int(new); merged+=int(not new)
                source.update(status='已连接',message=f'获取 {len(fetched)} 条，按方向保留 {len(relevant)} 条；新增 {new_count} 条',last_fetched=now())
                run['fetched']+=len(fetched); run['new_jobs']+=new_count; run['merged']+=merged
                run['results'].append({'source':source['name'],'status':'completed','fetched':len(fetched),'relevant':len(relevant),'new':new_count})
            except Exception as exc:
                source.update(status='最近获取失败',message=str(exc)[:500],last_attempt=now())
                run['results'].append({'source':source['name'],'status':'failed','error':str(exc)[:500]})
            # Read fresh settings before saving source runtime state so an in-flight
            # fetch cannot revert changes the user just made in the settings page.
            latest=self.store.get('settings')
            for current in latest.get('sources',[]):
                if current['id']==source['id'] and same_source_config(current,source):
                    for k in ('status','message','last_fetched','last_attempt'):
                        if k in source: current[k]=source[k]
            self.store.set('settings',latest)
            self.store.put('runs',run)
        digest=self.build_digest(run)
        run['recommended']=len(digest['job_ids'])
        failures=[r for r in run['results'] if r['status']=='failed']
        run.update(status='partial' if failures and len(failures)<len(enabled) else 'failed' if failures else 'completed', finished_at=now(), error='；'.join(r['source']+'：'+r['error'] for r in failures) if failures else '未启用自动来源，本次仅整理已有岗位' if not enabled else '')
        self.store.put('runs',run)
        return run

    def build_digest(self, run):
        settings=self.store.get('settings'); profile=self.store.get('profile')
        jobs=[j for j in self.store.all('jobs') if not j.get('is_sample') and j.get('status_validity')!='已关闭' and j.get('status') not in ('不合适','已关闭','已投递','面试中','Offer')]
        jobs=[j for j in jobs if self.relevant(j,profile) and salary_group(j.get('salary_raw',''),profile['salary_target'])['group']!='低于目标']
        jobs.sort(key=lambda j:({'meets':4,'possible':3,'unknown':2,'other_city':1,'below':0}[classify_constraints(j,profile)['code']], evidence_rank(j)),reverse=True)
        digest={'id':uid(),'date':datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(),'created_at':now(),'run_id':run['id'],'job_ids':[],'email_status':'disabled','email_error':''}
        # The notification ledger and in-app digest commit atomically. Repeated
        # fetches, multi-source merges, restarts and manual reruns cannot re-push a job.
        with self.store.connect(immediate=True) as db:
            for job in jobs:
                if len(digest['job_ids'])>=12: break
                existing=db.execute('SELECT 1 FROM delivered WHERE job_id=?',(job['id'],)).fetchone()
                if not existing:
                    db.execute('INSERT INTO delivered VALUES (?,?)',(job['id'],digest['id']))
                    digest['job_ids'].append(job['id'])
            digest['summary']=f"首次整理 {len(digest['job_ids'])} 个相关岗位，含待核实及其他城市线索，不代表均满足约束。发现日期不等于发布日期。" if digest['job_ids'] else '本次没有未推荐过且符合筛选条件的岗位，不凑数量。'
            db.execute('INSERT INTO digests VALUES (?,?)',(digest['id'],dump(digest)))
        if settings.get('email_enabled') and digest['job_ids']:
            if not notifications.configured():
                digest.update(email_status='blocked',email_error='邮件待配置 SMTP 环境变量，本次站内日报已保存')
            else:
                digest.update(email_status='sending'); self.store.put('digests',digest)
                try:
                    notifications.deliver(digest,[j for j in jobs if j['id'] in digest['job_ids']],settings)
                    digest.update(email_status='sent')
                except Exception as exc:
                    digest.update(email_status='failed',email_error='邮件发送未获确认（可能已送达，为避免重复不自动重发）：'+str(exc)[:300])
            self.store.put('digests',digest)
        return digest
