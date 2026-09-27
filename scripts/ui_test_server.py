"""Isolated browser acceptance environment; never touches real user data."""
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
os.environ['DATA_DIR']=str(ROOT/'tmp'/'ui-acceptance')
os.environ['APP_PASSWORD']='career-ui-test-2026'
from app.main import app
import uvicorn

settings=app.state.store.get('settings')
settings['auto_discovery']=False
for source in settings['sources']: source['enabled']=False
app.state.store.set('settings',settings)
if __name__=='__main__':
    uvicorn.run(app,host='127.0.0.1',port=8766,access_log=False)
