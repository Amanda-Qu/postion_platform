"""Small SQLite repository. WAL + short transactions support API and worker safely."""
import hashlib
import json
import os
import re
import sqlite3
import uuid
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

ROOT = Path(__file__).resolve().parent.parent

def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def uid():
    return uuid.uuid4().hex

def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

def canonical_url(url):
    try:
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            return ''
        query = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith('utm_') and k.lower() not in ('from', 'source', 'ref', 'referrer', 'trackingid')]
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/'), urlencode(sorted(query)), ''))
    except ValueError:
        return ''

def source_key(source):
    """A listing-page role needs an identity independent of its shared URL.

    Ordinary detail URLs retain their old keys. Page URLs stay real clickable
    links; the internal key is namespaced and never used as a request URL.
    """
    url = canonical_url(source.get('url', ''))
    role = source.get('source_identity')
    if url and isinstance(role,str) and role:
        return 'role:'+hashlib.sha256((url+'\n'+role).encode()).hexdigest()
    return url

def normalize(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC',str(value)).casefold())

def known(value):
    return bool(str(value or '').strip()) and str(value).strip().lower() not in ('未知','待核实','未披露','unknown','n/a','null')

def job_signature(job):
    fields=('company','title','city','salary_raw','experience','education','responsibilities','requirements','published_at')
    return hashlib.sha256(dump({k:job.get(k) for k in fields}).encode()).hexdigest()

def identity(job):
    # Conservative identity: all three facts must exist. Unknown titles/companies never
    # coalesce into one job. Exact duplicate imports still match their content hash.
    values = [normalize(job.get(k, '')) for k in ('company', 'title', 'city')]
    if all(x and x not in ('未知', '待核对') for x in values):
        return ('sample:' if job.get('is_sample') else '')+hashlib.sha256('|'.join(values).encode()).hexdigest()
    return ''

class Store:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.getenv('DATA_DIR', ROOT / 'data')).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / 'uploads').mkdir(exist_ok=True)
        (self.directory / 'exports').mkdir(exist_ok=True)
        self.path = self.directory / 'workbench.sqlite3'
        with self.connect() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, identity TEXT, content_hash TEXT, data TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS ix_identity ON jobs(identity);
              CREATE INDEX IF NOT EXISTS ix_content ON jobs(content_hash);
              CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY, job_id TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL, language TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(job_id,kind,language));
              CREATE TABLE IF NOT EXISTS resumes (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS interviews (id TEXT PRIMARY KEY, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS digests (id TEXT PRIMARY KEY, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS delivered (job_id TEXT PRIMARY KEY, digest_id TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, csrf TEXT, expires REAL);
              CREATE TABLE IF NOT EXISTS audit (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            ''')
        for key, file in [('profile', 'default_profile.json'), ('settings', 'default_settings.json')]:
            if self.get(key) is None:
                self.set(key, json.loads((ROOT / 'config' / file).read_text(encoding='utf-8')))

    @contextmanager
    def connect(self, immediate=False):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=20000')
        try:
            if immediate:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connect() as db:
            db.execute('INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, dump(value)))

    def one(self, table, id):
        assert table in ('jobs', 'files', 'tasks', 'resumes', 'interviews', 'runs', 'digests')
        with self.connect() as db:
            row = db.execute(f'SELECT data FROM {table} WHERE id=?', (id,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, table):
        assert table in ('jobs', 'files', 'tasks', 'resumes', 'interviews', 'runs', 'digests', 'audit')
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute(f'SELECT data FROM {table} ORDER BY rowid DESC')]

    def put(self, table, record):
        assert table in ('files', 'interviews', 'runs', 'digests', 'audit')
        with self.connect() as db:
            db.execute(f'INSERT INTO {table}(id,data) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', (record['id'], dump(record)))

    def save_job(self, job):
        job['updated_at'] = now()
        with self.connect() as db:
            db.execute('UPDATE jobs SET data=?, identity=? WHERE id=?', (dump(job), identity(job), job['id']))
        return job

    def update_job(self, id, mutate):
        """Atomic read/modify/write: long-running work cannot roll back user edits."""
        with self.connect(immediate=True) as db:
            row=db.execute('SELECT data FROM jobs WHERE id=?',(id,)).fetchone()
            if not row: raise ValueError('岗位不存在')
            job=json.loads(row[0])
            mutate(job)
            job['updated_at']=now()
            db.execute('UPDATE jobs SET data=?,identity=? WHERE id=?',(dump(job),identity(job),id))
        return job

    def attach_analysis(self, id, result, snapshot, profile_version):
        fields=('title','company','city','salary_raw','experience','education','responsibilities','requirements','published_at')
        def attach(current):
            if profile_version != self.get('profile_version',0) or any(current.get(k)!=snapshot.get(k) for k in fields):
                result['stale']=True
                result['stale_reason']='分析过程中岗位或画像已修改；此结果保留原资料依据，请重新分析。'
            else:
                current['analysis']=result
        self.update_job(id,attach)

    def ingest(self, job):
        """Merge exact identities/URLs while retaining every raw input and attachment.

        Example: the same 深圳/公司A/VLM岗位 from Lever and an imported screenshot
        becomes one record with two provenance entries. Similar-but-not-identical
        titles are intentionally not fuzzily collapsed.
        """
        from .parsing import merge_texts
        job.setdefault('sources', [])
        job.setdefault('attachments', [])
        job.setdefault('raw_text', '')
        for field in ('company','title','city','salary_raw','experience','education','responsibilities','requirements','published_at'):
            if not known(job.get(field)): job[field]=''
        sample_prefix='sample:' if job.get('is_sample') else ''
        raw_hash = sample_prefix+hashlib.sha256(normalize(job['raw_text']).encode()).hexdigest() if normalize(job['raw_text']) else ''
        key = identity(job)
        with self.connect(immediate=True) as db:
            found = None
            for source in job['sources']:
                url = canonical_url(source.get('url', ''))
                if url:
                    source['url'] = url
                    found = db.execute('SELECT j.data FROM jobs j JOIN sources s ON s.job_id=j.id WHERE s.url=?', (sample_prefix+source_key(source),)).fetchone()
                    if found:
                        break
            if not found and key:
                found = db.execute('SELECT data FROM jobs WHERE identity=?', (key,)).fetchone()
            if not found and raw_hash:
                found = db.execute('SELECT data FROM jobs WHERE content_hash=?', (raw_hash,)).fetchone()
            new = found is None
            if found:
                existing = json.loads(found[0])
                # Never overwrite a human correction or workflow status on refresh.
                conflicts = existing.setdefault('conflicts', [])
                old_urls={source_key(s) for s in existing.get('sources',[])}
                same_source_refresh=job.get('ingest_mode')=='source_refresh' and any(source_key(s) in old_urls for s in job.get('sources',[]))
                for field in ('company', 'title', 'city', 'salary_raw', 'experience', 'education', 'responsibilities', 'requirements', 'published_at'):
                    if not known(existing.get(field)) and known(job.get(field)):
                        existing[field] = job[field]
                    elif existing.get(field) and job.get(field) and existing[field] != job[field]:
                        conflict = {'field': field, 'existing': existing[field], 'incoming': job[field]}
                        if conflict not in conflicts:
                            conflicts.append(conflict)
                        if same_source_refresh and field not in existing.get('human_edited_fields',[]):
                            existing[field]=job[field]
                existing['raw_text'] = merge_texts([existing.get('raw_text', ''), job.get('raw_text', '')])
                for field in ('sources', 'attachments'):
                    for entry in job[field]:
                        unique = 'url' if field == 'sources' else 'id'
                        if not any((source_key(x)==source_key(entry)) if field=='sources' else x.get(unique)==entry.get(unique) for x in existing[field]):
                            existing[field].append(entry)
                for field in ('original_inputs', 'import_warnings'):
                    existing.setdefault(field, [])
                    for entry in job.get(field, []):
                        if entry not in existing[field]:
                            existing[field].append(entry)
                existing['uncertain_fields']=list(set(existing.get('uncertain_fields',[]))|set(job.get('uncertain_fields',[])))
                if job.get('last_verified'):
                    existing['last_verified'] = job['last_verified']
                # A fetch failure never closes a job. Only explicit page evidence can.
                if job.get('status_validity') in ('有效', '已关闭'):
                    existing['status_validity'] = job['status_validity']
                job = existing
            else:
                job.update(id=uid(), first_seen=now(), updated_at=now())
                for field, default in {'last_verified':'', 'status_validity':'未知', 'status':'待看', 'note':'', 'next_action':'', 'applied_at':'', 'favorite':False, 'is_sample':False, 'uncertain_fields':[], 'analysis':None, 'history':[]}.items():
                    job.setdefault(field, default)
            job['updated_at'] = now()
            db.execute('INSERT INTO jobs VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,identity=excluded.identity', (job['id'], identity(job), raw_hash, dump(job)))
            for source in job['sources']:
                url = canonical_url(source.get('url', ''))
                if url:
                    db.execute('INSERT OR IGNORE INTO sources VALUES (?,?)', (sample_prefix+source_key(source), job['id']))
        return job, new

    def tasks_for(self, job_id):
        return [x for x in self.all('tasks') if x['job_id'] == job_id]

    def enqueue(self, job_id, kind, language='zh', retry=False):
        with self.connect(immediate=True) as db:
            old = db.execute('SELECT data FROM tasks WHERE job_id=? AND kind=? AND language=?', (job_id, kind, language)).fetchone()
            old = json.loads(old[0]) if old else None
            if old and (not retry or old['status'] in ('queued', 'running')):
                return old
            task = {'id': old['id'] if old else uid(), 'job_id':job_id, 'kind':kind, 'language':language, 'status':'queued', 'error':'', 'result':old.get('result') if old else None, 'attempts': old.get('attempts', 0) if old else 0, 'updated_at':now()}
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data', (task['id'], job_id, kind, language, 'queued', dump(task)))
            return task

    def claim(self):
        with self.connect(immediate=True) as db:
            row = db.execute("SELECT data FROM tasks WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return None
            task = json.loads(row[0])
            task.update(status='running', updated_at=now(), attempts=task['attempts']+1)
            db.execute('UPDATE tasks SET status=?,data=? WHERE id=?', ('running', dump(task), task['id']))
            return task

    def finish_task(self, task):
        task['updated_at'] = now()
        with self.connect() as db:
            db.execute('UPDATE tasks SET status=?,data=? WHERE id=?', (task['status'], dump(task), task['id']))

    def save_resume(self, resume):
        with self.connect() as db:
            db.execute('INSERT INTO resumes VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', (resume['id'], resume['job_id'], dump(resume)))

    def recover(self):
        for task in self.all('tasks'):
            if task['status'] == 'running':
                task.update(status='queued', error='上次服务中断，已恢复任务')
                self.finish_task(task)
        for run in self.all('runs'):
            if run['status'] in ('queued', 'running'):
                run.update(status='failed', error='服务重启中断了本次获取，请立即更新重试', finished_at=now())
                self.put('runs', run)
        for digest in self.all('digests'):
            if digest.get('email_status') == 'sending':
                digest.update(email_status='uncertain', email_error='发送过程中服务中断，邮件可能已送达；为避免重复未自动重发。')
                self.put('digests', digest)
