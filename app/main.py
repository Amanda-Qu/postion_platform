"""Authenticated FastAPI API + local responsive app; no secret enters static files."""
import copy
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import ValidationError
from bs4 import BeautifulSoup

from . import ai, interview, notifications, company_discovery
from .analysis import match_job, report, tags
from .exports import ResumeContent, export_resume, resume_blocks
from .models import Profile, Settings, JobPatch, Language, Login, InterviewStart, Answer
from .parsing import extract_file, merge_texts, parse_job, salary_group
from .sources import source_catalog, safe_fetch
from .profile_merge import merge_profile
from .constraints import classify_constraints, evidence_rank
from .source_catalog import company_catalog, merge_catalog, CATALOG_VERSION
from .store import ROOT, Store, now, uid, canonical_url, dump, job_signature
from .worker import Worker, KINDS

load_dotenv(ROOT / '.env')

def create_app(data_dir=None, start_worker=True):
    store=Store(data_dir)
    settings=store.get('settings')
    if not settings['sources']:
        settings['sources']=source_catalog(); store.set('settings',settings)
    worker=Worker(store)
    password=os.getenv('APP_PASSWORD','')
    password_path=store.directory/'access-password.txt'
    if not password:
        if not password_path.exists():
            password_path.write_text(secrets.token_urlsafe(21),encoding='utf-8')
        password=password_path.read_text(encoding='utf-8').strip()
    if len(password)<12:
        raise RuntimeError('APP_PASSWORD 至少需要12个字符')
    salt=secrets.token_bytes(16)
    password_hash=hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1)
    attempts={}; auth_lock=threading.Lock(); interview_lock=threading.Lock(); export_lock=threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            (store.directory/'server-runtime.pid').write_text(str(os.getpid()),encoding='ascii')
            worker.start()
        yield
        if start_worker:
            worker.stop()
            (store.directory/'server-runtime.pid').unlink(missing_ok=True)

    app=FastAPI(title='个人求职工作台',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.store=store; app.state.worker=worker

    def get_session(request):
        token=request.cookies.get('career_session','')
        if not token: return None
        with store.connect() as db:
            row=db.execute('SELECT csrf,expires FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        return dict(row) if row and row['expires']>time.time() else None

    @app.middleware('http')
    async def protection(request, call_next):
        origin=request.headers.get('origin')
        # Only same-origin writes. CSRF still applies to clients without Origin.
        if request.method not in ('GET','HEAD','OPTIONS') and origin:
            origin_parts=urlsplit(origin)
            if origin_parts.netloc != request.headers.get('host'):
                return JSONResponse({'detail':'禁止跨站写入'},status_code=403)
        length=request.headers.get('content-length')
        if length and (not length.isdigit() or int(length)>38*1024*1024):
            return JSONResponse({'detail':'本次上传总量不得超过36MB'},status_code=413)
        if request.url.path.startswith('/api/') and request.url.path not in ('/api/login','/api/session','/api/health'):
            session=get_session(request)
            if not session: return JSONResponse({'detail':'请先登录个人工作台'},status_code=401)
            if request.method not in ('GET','HEAD','OPTIONS') and not secrets.compare_digest(request.headers.get('x-csrf-token',''),session['csrf']):
                return JSONResponse({'detail':'会话校验失败，请刷新页面重试'},status_code=403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Cache-Control']='no-store'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail':str(exc)[:600]},status_code=400)

    @app.exception_handler(ai.NeedsConfiguration)
    async def unconfigured(request,exc):
        return JSONResponse({'detail':str(exc)},status_code=409)

    def public_settings():
        value=copy.deepcopy(store.get('settings'))
        cfg=ai.configuration(value)
        value.update(ai_configured=ai.configured(value), smtp_configured=notifications.configured(), ai_model=cfg['model'], ai_base_url=cfg['url'], ai_env_override=bool(os.getenv('AI_MODEL') or os.getenv('AI_BASE_URL')))
        return value

    def get_job(id):
        job=store.one('jobs',id)
        if not job: raise HTTPException(404,'岗位不存在')
        profile=store.get('profile')
        job['salary_detail']=salary_group(job.get('salary_raw',''),profile['salary_target'])
        job['salary_group']=job['salary_detail']['group']
        job['constraint_fit']=classify_constraints(job,profile)
        return job

    def decorate(job):
        job=copy.deepcopy(job)
        profile=store.get('profile')
        job['salary_detail']=salary_group(job.get('salary_raw',''),profile['salary_target'])
        job['salary_group']=job['salary_detail']['group']
        job['constraint_fit']=classify_constraints(job,profile)
        if not job.get('analysis'): job['analysis']=match_job(job,profile)
        if 'evidence_coverage' not in job['analysis']:
            job['analysis']['evidence_coverage']=match_job(job,profile)['evidence_coverage']
        job['recommendation']=job['analysis']['recommendation']
        job['match_score']=job['analysis'].get('score')
        themes=', '.join(job['analysis'].get('direction',{}).get('job_tags',[])[:4]) or '职责尚待补充'
        job['reason']=f"岗位主题：{themes}。{job['analysis']['constraints']['location']['result']}；{job['salary_group']}。{job['analysis']['recommendation']}，具体深度按经历证据核对。"
        return job

    @app.get('/api/health')
    def health():
        return {'status':'ok','service':'career-workbench'}

    @app.get('/api/session')
    def session(request:Request):
        session=get_session(request)
        return {'authenticated':bool(session),'csrf_token':session['csrf'] if session else '', 'password_hint':'首次启动的登录密码保存在本机 data/access-password.txt；也可通过 APP_PASSWORD 设置。'}

    @app.post('/api/login')
    def login(body:Login,request:Request):
        ip=request.client.host if request.client else 'local'
        with auth_lock:
            history=[t for t in attempts.get(ip,[]) if time.time()-t<300]
            if len(history)>=12: raise HTTPException(429,'尝试次数过多，请5分钟后重试')
            history.append(time.time()); attempts[ip]=history
        candidate=hashlib.scrypt(body.password.encode(),salt=salt,n=16384,r=8,p=1)
        if not secrets.compare_digest(candidate,password_hash): raise HTTPException(401,'密码不正确')
        with auth_lock: attempts.pop(ip,None)
        token=secrets.token_urlsafe(36); csrf=secrets.token_urlsafe(28)
        with store.connect() as db:
            db.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
            db.execute('INSERT INTO sessions VALUES (?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),csrf,time.time()+12*3600))
        response=JSONResponse({'authenticated':True,'csrf_token':csrf})
        response.set_cookie('career_session',token,max_age=12*3600,httponly=True,samesite='strict',secure=os.getenv('COOKIE_SECURE','false').lower()=='true')
        return response

    @app.post('/api/logout')
    def logout(request:Request):
        with store.connect() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(request.cookies.get('career_session','').encode()).hexdigest(),))
        response=JSONResponse({'ok':True}); response.delete_cookie('career_session'); return response

    @app.get('/api/dashboard')
    def dashboard():
        jobs=[decorate(j) for j in store.all('jobs')]
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        first_today=lambda j: datetime.fromisoformat(j['first_seen']).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()==today
        counts={s:sum(j['status']==s for j in jobs) for s in ('待看','感兴趣','可投','已投递','面试中','Offer')}
        stats={'total':len(jobs),'new_today':sum(first_today(j) for j in jobs),'ready':counts['可投'],'applied':counts['已投递'],'interviewing':counts['面试中'],'confirmed_salary':sum(j['salary_group']=='明确符合' for j in jobs),'statuses':counts}
        profile=store.get('profile')
        recommendations=[j for j in jobs if not j.get('is_sample') and worker.relevant(j,profile) and j['status_validity']!='已关闭' and j['status'] not in ('不合适','已关闭','已投递','面试中','Offer') and j['salary_group']!='低于目标']
        recommendations.sort(key=lambda j:({'meets':4,'possible':3,'unknown':2,'other_city':1,'below':0}[j['constraint_fit']['code']], evidence_rank(j), j.get('published_at') or ''),reverse=True)
        return {'jobs':jobs,'recommendations':recommendations,'stats':stats,'sources':public_settings()['sources'],'runs':store.all('runs')[:15],'digests':store.all('digests')[:30],'settings':public_settings(),'profile':profile}

    @app.get('/api/jobs')
    def jobs(q:str='',status:str='',salary_group:str='',city:str='',sort:str='newest',favorite:str=''):
        rows=[decorate(j) for j in store.all('jobs')]
        if q: rows=[j for j in rows if q.lower() in dump(j).lower()]
        if status: rows=[j for j in rows if j['status']==status]
        if salary_group: rows=[j for j in rows if j['salary_group']==salary_group]
        if city: rows=[j for j in rows if city.lower() in j.get('city','').lower()]
        if favorite in ('true','1'): rows=[j for j in rows if j.get('favorite')]
        if sort in ('score','match'): rows.sort(key=evidence_rank,reverse=True)
        elif sort=='salary': rows.sort(key=lambda j:j['salary_detail']['min_monthly'] or -1,reverse=True)
        elif sort=='updated': rows.sort(key=lambda j:j['updated_at'],reverse=True)
        return {'jobs':rows}

    @app.get('/api/jobs/{id}')
    def detail(id:str):
        job=decorate(get_job(id))
        resumes=[r for r in store.all('resumes') if r['job_id']==id]
        for r in resumes:
            r['blocks']=resume_blocks(r['content'])
            r['stale']=r.get('profile_snapshot')!=store.get('profile') or job_signature(r.get('job_snapshot') or {})!=job_signature(job)
            if r['stale']: r['stale_reason']='此版本基于较早的画像或岗位资料；新版本不会改写历史版本。'
            r.pop('profile_snapshot',None); r.pop('job_snapshot',None)
        tasks=store.tasks_for(id)
        for task in tasks:
            task['stale']=bool(task.get('result')) and (task.get('profile_version')!=store.get('profile_version',0) or task.get('job_signature')!=job_signature(job))
            if task['stale']: task['stale_reason']='画像或岗位已修改，当前保存结果基于旧资料；可单项更新。'
        return {'job':job,'tasks':tasks,'resumes':resumes,'interviews':[{k:v for k,v in x.items() if k not in ('profile_snapshot','job_snapshot','resume_snapshot')} for x in store.all('interviews') if x['job_id']==id], 'analysis':job.get('analysis')}

    @app.patch('/api/jobs/{id}')
    def edit_job(id:str,body:JobPatch):
        get_job(id); updates=body.model_dump(exclude_none=True)
        def mutate(job):
            changed={k:{'before':job.get(k),'after':v} for k,v in updates.items() if job.get(k)!=v}
            job.update(updates)
            job['human_edited_fields']=list(set(job.get('human_edited_fields',[]))|set(k for k in updates if k in ('company','title','city','salary_raw','experience','education','responsibilities','requirements','published_at')))
            if changed:
                job.setdefault('history',[]).append({'at':now(),'changes':changed})
                job['uncertain_fields']=[x for x in job.get('uncertain_fields',[]) if x not in updates]
                if any(k in changed for k in ('title','company','responsibilities','requirements','salary_raw','city','experience','education')):
                    job['analysis']=None
                    job['materials_note']='岗位资料已修改，已保存的材料仍保留原版本；可逐项重新生成。'
            if body.status=='已投递' and not job.get('applied_at'): job['applied_at']=now()[:10]
        job=store.update_job(id,mutate)
        if body.status=='可投': worker.prepare(id)
        return {'job':decorate(job),'tasks':store.tasks_for(id)}

    async def save_upload(file:UploadFile, purpose:str):
        original=Path(file.filename or 'upload').name
        ext=Path(original).suffix.lower()
        if ext not in ('.pdf','.docx','.txt','.md','.png','.jpg','.jpeg','.webp'):
            raise HTTPException(400,'支持 PDF、DOCX、TXT、Markdown、PNG、JPEG、WebP 文件')
        id=uid(); target=store.directory/'uploads'/(id+ext)
        size=0; hasher=hashlib.sha256()
        try:
            with target.open('wb') as stream:
                while chunk:=await file.read(1024*1024):
                    size+=len(chunk)
                    if size>12*1024*1024: raise HTTPException(413,'每个文件不得超过12MB')
                    stream.write(chunk); hasher.update(chunk)
            if not size: raise HTTPException(400,'文件为空')
        except Exception:
            target.unlink(missing_ok=True)
            raise
        item={'id':id,'name':original,'filename':target.name,'size':size,'sha256':hasher.hexdigest(),'purpose':purpose,'created_at':now()}
        store.put('files',item)
        return item,target

    @app.post('/api/import')
    async def import_job(text:str=Form(''),urls:str=Form(''),files:list[UploadFile]=File(default=[])):
        if len(text)>100000 or len(urls)>12000: raise HTTPException(400,'文本过长，请精简后导入')
        if len(files)>6: raise HTTPException(400,'一次最多6个文件，其中截图最多2张')
        if sum(Path(f.filename or '').suffix.lower() in ('.png','.jpg','.jpeg','.webp') for f in files)>2:
            raise HTTPException(400,'一次最多2张截图')
        links=[x.strip() for x in re.split(r'[\n\s]+',urls) if x.strip()]
        if len(links)>5: raise HTTPException(400,'一次最多5个岗位来源链接')
        parts=[text]; attachments=[]; warnings=[]; sources=[]; originals=[]; total=0
        for f in files:
            item,path=await save_upload(f,'job')
            total+=item['size']
            if total>36*1024*1024: raise HTTPException(413,'本次上传总量不得超过36MB')
            attachments.append({'id':item['id'],'name':item['name']})
            try:
                parsed=await run_in_threadpool(extract_file,path)
                parts.append(parsed['text']); warnings.extend(parsed['warnings'])
                originals.append({'file_id':item['id'],'text':parsed['text'],'method':parsed['method']})
            except Exception as exc:
                warnings.append(item['name']+'：解析失败，原件已保存；'+str(exc)[:200])
        for link in links:
            url=canonical_url(link)
            if not url:
                warnings.append('链接格式无效，已保留原始输入：'+link); continue
            sources.append({'url':url,'name':urlsplit(url).hostname or '手动链接','fetched_at':''})
            try:
                fetched=await run_in_threadpool(safe_fetch,url)
                soup=BeautifulSoup(fetched.text,'html.parser')
                for node in soup(['script','style','nav','footer','header']): node.decompose()
                page_text=soup.get_text('\n',strip=True)[:100000]
                parts.append(page_text); sources[-1]['fetched_at']=now()
                originals.append({'url':link,'text':page_text,'method':'公开页面文本'})
            except Exception as exc:
                warnings.append('链接获取失败，已保留你输入的内容和链接。请补充截图或文字：'+str(exc)[:250])
        if not text.strip() and not attachments and not links:
            raise HTTPException(400,'请提供岗位文字、截图、文件或链接')
        merged=merge_texts(parts)
        if len(merged)>150000:
            warnings.append('解析文本超过15万字，结构化仅使用前15万字；原件完整保留')
        job=parse_job(merged[:150000])
        job.update(raw_text=merged[:300000],sources=sources,attachments=attachments,original_inputs=[{'text':text,'links':links}]+originals,last_verified=now() if any(s['fetched_at'] for s in sources) else '',import_warnings=warnings)
        # OCR and rule extraction are draft interpretations: highlight every
        # extracted field until the user confirms it, even if its syntax is certain.
        job['uncertain_fields']=list(set(job.get('uncertain_fields',[])) | set(k for k in ('company','title','city','salary_raw','experience','education','responsibilities','requirements','published_at') if job.get(k)))
        saved,new=store.ingest(job)
        return {'job':decorate(saved),'warnings':warnings,'merged':not new}

    @app.post('/api/jobs/{id}/analyze')
    def analyze(id:str):
        job=get_job(id)
        profile_version=store.get('profile_version',0)
        result=report(job,store.get('profile'),store.get('settings'))
        store.attach_analysis(id,result,job,profile_version)
        return {'analysis':result}

    @app.post('/api/jobs/{id}/prepare')
    def prepare(id:str,body:Language=Language()):
        get_job(id)
        def mutate(job):
            if job['status'] not in ('已投递','面试中','Offer'): job['status']='可投'
        store.update_job(id,mutate)
        return {'tasks':worker.prepare(id,body.language)}

    @app.post('/api/jobs/{id}/tasks/{kind}/retry')
    def retry(id:str,kind:str,body:Language=Language()):
        get_job(id)
        if kind not in KINDS: raise HTTPException(400,'未知任务')
        result=store.enqueue(id,kind,body.language,retry=True); worker.wake.set()
        return {'task':result,'tasks':store.tasks_for(id)}

    @app.get('/api/files/{id}')
    def original_file(id:str,inline:bool=False):
        item=store.one('files',id)
        if not item: raise HTTPException(404,'文件不存在')
        target=(store.directory/'uploads'/item['filename']).resolve()
        if target.parent!=store.directory/'uploads' or not target.is_file(): raise HTTPException(404,'文件不存在')
        allow_inline=inline and target.suffix.lower() in ('.png','.jpg','.jpeg','.webp')
        return FileResponse(target,filename=item['name'],content_disposition_type='inline' if allow_inline else 'attachment')

    @app.get('/api/resumes/{id}/{fmt}')
    def download_resume(id:str,fmt:str):
        resume=store.one('resumes',id)
        if not resume or fmt not in ('docx','pdf'): raise HTTPException(404,'简历版本不存在')
        if resume.get('requires_review'): raise HTTPException(409,'请先核对并确认本版本的事实与翻译')
        target=store.directory/'exports'/f'{id}.{fmt}'
        # Resume IDs are immutable versions. Cache each export and publish it
        # atomically, so double-clicking Download cannot read a half-written file.
        with export_lock:
            if not target.exists():
                temporary=store.directory/'exports'/f'{id}-{uid()}.{fmt}'
                try:
                    export_resume(resume['content'],fmt,temporary)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
        mime='application/pdf' if fmt=='pdf' else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        return FileResponse(target,filename=f"resume-{resume['language']}-{id[:8]}.{fmt}",media_type=mime)

    @app.post('/api/resumes/{id}/confirm')
    def confirm_resume(id:str,body:dict):
        old=store.one('resumes',id)
        if not old: raise HTTPException(404,'简历不存在')
        content=ResumeContent.model_validate(body.get('content',old['content'])).model_dump()
        resume_blocks(content)
        # A manual correction creates another version. The foundation and prior
        # exported versions remain immutable and retain their profile snapshot.
        updated=copy.deepcopy(old)
        updated.update(id=uid(),parent_id=id,content=content,created_at=now(),requires_review=False,confirmed_by_user_at=now())
        updated['changes'].append({'before':dump(old['content']),'after':dump(content),'reason':'用户逐项核对并保存为独立版本'})
        store.save_resume(updated)
        return {'resume':updated}

    @app.get('/api/profile')
    def profile():
        return {'profile':store.get('profile'),'version':store.get('profile_version',0),'uploads':[x for x in store.all('files') if x['purpose']=='profile'],'draft':store.get('profile_draft')}

    def profile_changed(value):
        store.put('audit',{'id':uid(),'at':now(),'kind':'profile_update','snapshot':value})
        for job in store.all('jobs'):
            store.update_job(job['id'],lambda current:current.update(analysis=None,materials_note='个人画像已更新，历史材料保留原版本。请核对准备进度中的待更新项。'))

    def write_profile(db,value):
        row=db.execute("SELECT value FROM kv WHERE key='profile_version'").fetchone()
        version=json.loads(row[0]) if row else 0
        for key, val in [('profile',value),('profile_version',version+1)]:
            db.execute('INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,dump(val)))

    @app.put('/api/profile')
    def update_profile(body:Profile):
        value=body.model_dump()
        with store.connect(immediate=True) as db:
            write_profile(db,value)
        profile_changed(value)
        return {'profile':value}

    @app.post('/api/profile/draft/accept')
    def accept_profile_draft(body:dict):
        reviewed=Profile.model_validate(body.get('profile')).model_dump()
        with store.connect(immediate=True) as db:
            row=db.execute("SELECT value FROM kv WHERE key='profile_draft'").fetchone()
            draft=json.loads(row[0]) if row else None
            if not draft or not draft.get('id') or body.get('draft_id')!=draft['id']:
                raise HTTPException(409,'草稿已过期或被替换，请重新打开最新提取草稿')
            current=json.loads(db.execute("SELECT value FROM kv WHERE key='profile'").fetchone()[0])
            value, conflicts=merge_profile(draft['base_profile'],current,reviewed)
            if conflicts:
                raise HTTPException(409,'这些字段在提取后已有更新，未覆盖任何内容。请重新上传并核对：'+ '、'.join(conflicts))
            value=Profile.model_validate(value).model_dump()
            write_profile(db,value)
            db.execute("DELETE FROM kv WHERE key='profile_draft'")
        profile_changed(value)
        return {'profile':value}

    @app.post('/api/profile/upload')
    async def upload_profile(file:UploadFile=File(...)):
        item,path=await save_upload(file,'profile')
        parsed=await run_in_threadpool(extract_file,path)
        text=parsed['text']; base_profile=copy.deepcopy(store.get('profile')); draft=copy.deepcopy(base_profile)
        draft['confirmed']=False
        email=re.search(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',text)
        phone=re.search(r'(?<!\d)1[3-9]\d{9}(?!\d)',text)
        if email or phone: draft['contact']=' | '.join(x.group(0) for x in [email,phone] if x)
        name=re.search(r'(?:姓名|Name)\s*[:：]\s*([^\n]+)',text,re.I)
        if name: draft['name']=name[1].strip()
        years=re.search(r'(\d+(?:\.\d+)?)\s*年(?:以上)?(?:工作|算法|相关)?经验',text)
        if years: draft['years']=min(float(years[1]),70)
        for name in tags(text):
            existing=next((s for s in draft['skills'] if s['name']==name),None)
            quote=next((line for line in text.splitlines() if name.lower() in line.lower()),'简历含相关术语，需用户核对实践程度')
            value={'name':name,'level':'unknown','evidence':quote[:1000],'confirmed':False}
            if existing: existing.update(value)
            else: draft['skills'].append(value)
        # Conservative project candidates require an explicit project label. Resume
        # mentions do not automatically become confirmed responsibilities or skills.
        chunks=re.split(r'(?m)^\s*(?:项目名称|项目经历|项目)\s*[:：]\s*',text)
        for chunk in chunks[1:10]:
            lines=[x.strip() for x in chunk.splitlines() if x.strip()]
            if lines:
                draft['projects'].append({'id':uid()[:12],'title':lines[0][:150],'context':'','bullets':lines[1:16],'skills':tags(chunk),'confirmed':False})
        result={'id':uid(),'base_profile':base_profile,'base_version':store.get('profile_version',0),'draft':Profile.model_validate(draft).model_dump(),'text':text,'warnings':parsed['warnings']+['提取结果为待核对草稿，未覆盖基础履历。请核对姓名、项目边界、技能深度与学习/实践分类后保存。'],'file':item,'method':parsed['method']}
        store.set('profile_draft',result)
        return result

    @app.get('/api/settings')
    def settings_get(): return public_settings()

    @app.get('/api/sources/catalog')
    def source_catalog_get():
        return {'version':CATALOG_VERSION, 'sources':company_catalog(),
                'scope':'人工维护的入口目录；另可通过「发现新公司」按技能搜索待核实线索'}

    @app.post('/api/sources/catalog/merge')
    def source_catalog_merge(body:dict):
        ids=body.get('ids')
        if set(body) != {'ids'} or not isinstance(ids,list) or not all(isinstance(x,str) for x in ids):
            raise HTTPException(422,'请提供来源 id 列表')
        # One write transaction avoids overwriting a simultaneous settings change.
        with store.connect(immediate=True) as db:
            current=json.loads(db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()[0])
            try:
                current['sources'],added=merge_catalog(current.get('sources',[]),ids)
            except ValueError as exc:
                raise HTTPException(422,str(exc)) from exc
            value=Settings.model_validate(current).model_dump()
            db.execute("UPDATE kv SET value=? WHERE key='settings'",(dump(value),))
        return {'added_ids':added,'settings':public_settings()}

    discovery_lock = threading.Lock()

    @app.get('/api/sources/discovery')
    def company_discovery_preview():
        return {**company_discovery.preview(store.get('profile')),
                'last_run':store.get('company_discovery_run')}

    @app.post('/api/sources/discovery')
    def company_discovery_run(body:dict):
        plan = company_discovery.preview(store.get('profile'))
        if set(body) != {'query_token'} or body.get('query_token') != plan['query_token']:
            raise HTTPException(409,'搜索词已改变，请重新打开预览并确认')
        if not plan['configured']:
            raise HTTPException(409,plan['setup'])
        if not plan['queries']:
            raise HTTPException(422,'请先保存通用目标方向或已确认实践技能，以及支持的目标城市')
        if not discovery_lock.acquire(blocking=False):
            raise HTTPException(409,'公司搜索正在运行，请稍后查看结果')
        try:
            last=store.get('company_discovery_run') or {}
            if time.time() - last.get('started_epoch',0) < 60:
                raise HTTPException(429,'请等待一分钟再搜索；上次结果已保留')
            started=time.time()
            result=company_discovery.discover(plan,store.get('settings')['sources'])
            result['started_epoch']=started
            store.set('company_discovery_run',result)
            return result
        finally:
            discovery_lock.release()

    @app.post('/api/sources/discovery/review')
    def company_discovery_review(body:dict):
        if set(body) != {'id','company','official_url','confirmed'} or body.get('confirmed') is not True:
            raise HTTPException(422,'请确认公司名称及官网证据后再添加')
        company=body.get('company')
        official=company_discovery.candidate_url(body.get('official_url'))
        if not isinstance(company,str) or not 1 <= len(company.strip()) <= 100 or not official:
            raise HTTPException(422,'请提供公司名称和公开 HTTPS 官网证据链接')
        # User attestation is labelled separately from automated ownership proof.
        # No arbitrary-site fetch and no automatic parser/enabling from a search hit.
        with store.connect(immediate=True) as db:
            row=db.execute("SELECT value FROM kv WHERE key='company_discovery_run'").fetchone()
            run=json.loads(row[0]) if row else {}
            candidate=next((c for c in run.get('candidates',[]) if c['id']==body['id']),None)
            if not candidate: raise HTTPException(404,'线索不存在，请刷新结果')
            settings=json.loads(db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()[0])
            if not any(company_discovery.dedupe_key(s.get('url',''))==company_discovery.dedupe_key(candidate['url']) for s in settings['sources']):
                if len(settings['sources']) >= 30: raise HTTPException(422,'来源最多 30 个')
                settings['sources'].append(dict(id=candidate['id'],name=company.strip()+' · 人工核对入口',
                    company=company.strip(),url=candidate['url'],kind='career_portal',enabled=False,
                    status='人工查看',ingestion_status='portal_pending',verification='user_confirmed',
                    official_evidence_url=official,reviewed_at=now(),discovery_evidence=candidate.copy(),
                    message='官网归属由用户核对，系统未独立验证；无自动读取适配器，岗位和薪资未知。'))
                settings=Settings.model_validate(settings).model_dump()
                db.execute("UPDATE kv SET value=? WHERE key='settings'",(dump(settings),))
            candidate.update(review_status='added',verification='user_confirmed',official_evidence_url=official)
            db.execute("UPDATE kv SET value=? WHERE key='company_discovery_run'",(dump(run),))
        return {'settings':public_settings(),'last_run':run}

    @app.put('/api/settings')
    def settings_put(body:dict):
        for k in ('ai_configured','smtp_configured','ai_env_override'): body.pop(k,None)
        value=Settings.model_validate(body).model_dump()
        current=store.get('settings')
        old={x['id']:x for x in current['sources']}
        for source in value['sources']:
            prior=old.get(source['id'])
            if prior and all(source.get(k)==prior.get(k) for k in ('kind','board','url','site','region','company')):
                for k in ('status','message','last_fetched','last_attempt'):
                    if k in prior: source[k]=prior[k]
            else:
                source.update(status='需要配置',message='配置已更改，请立即更新验证连接')
        store.set('settings',value)
        worker.wake.set()
        return public_settings()

    @app.post('/api/discovery/run')
    def discovery(): return {'run':worker.request_discovery()}

    @app.post('/api/interviews')
    def start_interview(body:InterviewStart):
        job=get_job(body.job_id)
        value=interview.start(job,store.get('profile'),[r for r in store.all('resumes') if r['job_id']==body.job_id],body.language,body.type,store.get('settings'))
        if store.get('settings').get('use_ai') and not ai.configured(store.get('settings')):
            raise ai.NeedsConfiguration('AI面试待配置；可在设置中关闭AI增强，使用明确标注的本地分支练习。')
        store.put('interviews',value)
        return {'interview':value}

    @app.get('/api/interviews/{id}')
    def get_interview(id:str):
        value=store.one('interviews',id)
        if not value: raise HTTPException(404,'面试记录不存在')
        return {'interview':value}

    @app.post('/api/interviews/{id}/answer')
    def answer_interview(id:str,body:Answer):
        # Serialize one-answer/one-follow-up transitions; retries after a provider
        # failure reuse the already-saved answer rather than duplicating a round.
        with interview_lock:
            value=store.one('interviews',id)
            if not value: raise HTTPException(404,'面试记录不存在')
            if value['status']=='completed': raise HTTPException(409,'本轮面试已结束，请开启新一轮')
            if len(value['messages'])>=41: raise HTTPException(400,'本轮已达到20轮，请结束面试查看反馈')
            if not (value['messages'][-1]['role']=='user' and value['messages'][-1]['content']==body.answer):
                if value['messages'][-1]['role']=='user': raise HTTPException(409,'上一回答已保存，请用同一回答重试生成追问')
                value['messages'].append({'role':'user','content':body.answer,'at':now()})
            value.update(updated_at=now(),status='waiting_generation'); store.put('interviews',value)
            try:
                interview_settings=copy.deepcopy(store.get('settings'))
                interview_settings['use_ai']=value['engine']=='AI追问'
                question,focus=interview.next_question(value,body.answer,interview_settings)
                value['messages'].append({'role':'assistant','content':question,'focus':focus,'at':now()})
                value.update(status='active',error='',updated_at=now())
            except Exception as exc:
                value.update(error=str(exc)[:500]); store.put('interviews',value)
                raise
            store.put('interviews',value)
            return {'interview':value}

    @app.post('/api/interviews/{id}/finish')
    def finish_interview(id:str):
        with interview_lock:
            value=store.one('interviews',id)
            if not value: raise HTTPException(404,'面试记录不存在')
            value.update(status='completed',feedback=interview.finish(value),updated_at=now())
            store.put('interviews',value)
        return {'interview':value}

    app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
    @app.get('/')
    def index(): return FileResponse(ROOT/'static'/'index.html')
    return app

app=create_app()
