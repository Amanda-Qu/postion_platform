"""Development-only maintenance: remove untouched bootstrap source records.

This never removes manual imports, notes, favourites, edited fields, workspaces, or
any job that has moved beyond 待看. A SQLite backup is taken before a mutation.
"""
import sys
from pathlib import Path
import sqlite3
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from app.store import Store, dump
from app.worker import Worker

store=Store(); worker=Worker(store); profile=store.get('profile')
used={x['job_id'] for t in ('tasks','resumes','interviews') for x in store.all(t)}
candidates=[j for j in store.all('jobs') if not worker.relevant(j,profile) and j['id'] not in used and not j.get('original_inputs') and not j.get('attachments') and not j.get('human_edited_fields') and not j.get('note') and not j.get('favorite') and not j.get('next_action') and j.get('status')=='待看' and j.get('sources') and all('greenhouse' in s.get('url','') for s in j['sources'])]
if candidates:
    with store.connect() as db, sqlite3.connect(store.directory/'bootstrap-before-filter.sqlite3') as backup:
        db.backup(backup)
    ids={j['id'] for j in candidates}
    with store.connect(immediate=True) as db:
        for id in ids:
            db.execute('DELETE FROM sources WHERE job_id=?',(id,))
            db.execute('DELETE FROM delivered WHERE job_id=?',(id,))
            db.execute('DELETE FROM jobs WHERE id=?',(id,))
        for digest in store.all('digests'):
            digest['job_ids']=[id for id in digest['job_ids'] if id not in ids]
            digest['summary']=f"首次推荐 {len(digest['job_ids'])} 个岗位。首次发现不等于发布，已按岗位方向核对。"
            db.execute('UPDATE digests SET data=? WHERE id=?',(dump(digest),digest['id']))
print({'removed_untouched_bootstrap_jobs':len(candidates),'remaining_jobs':len(store.all('jobs'))})
