from app.worker import Worker
from app.store import Store

def test_company_robotics_boilerplate_does_not_recommend_unrelated_roles(tmp_path):
    store=Store(tmp_path); worker=Worker(store); profile=store.get('profile')
    body={'responsibilities':'Build robots with multimodal models and computer vision at our robotics company','requirements':'Python'}
    for title in ('Wire Harness Engineer','Security Engineer','Mechanical Engineer','HR Manager','Helix AI Engineer, Android'):
        assert not worker.relevant({'title':title,**body},profile)
    assert worker.relevant({'title':'Helix AI Engineer, Perception',**body},profile)
    assert worker.relevant({'title':'AI Training Infrastructure Engineer',**body},profile)
