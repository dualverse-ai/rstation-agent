import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import restart_pool as pool


def test_rollback_fences_cancels_waits_then_clears_old(monkeypatch):
    record = dict(id='operation',deadline=10,pool_activation_requested=True,
                  pool_plan=dict(old_project='old',new_project='new'))
    events=[]; busy=[True]
    monkeypatch.setattr(pool,'activate',lambda r: events.append('resolve_activation'))
    def api(plan,path,body):
        events.append((path,body))
        if path=='/api/project-cutoff':return dict(enforced=True)
        if body.get('project')=='old':return dict(operation='operation',not_after=9)
        return dict(active_jobs=[dict(id='job')] if busy[0] else [],permits_pending=['job'] if busy[0] else [])
    monkeypatch.setattr(pool,'api',api)
    with pytest.raises(ValueError,match='permits remain'):pool.rollback_pool(record)
    assert not any(isinstance(e,tuple) and e[1].get('clear') for e in events)
    busy[0]=False;pool.rollback_pool(record)
    assert events[-1][1] == dict(project='old',operation='operation',clear=True)


def test_rollback_before_activation_clears_only_old(monkeypatch):
    events=[]
    monkeypatch.setattr(pool,'clear_cutoff',lambda r: events.append('clear_old'))
    monkeypatch.setattr(pool,'activate',lambda r: pytest.fail('Never activate during preflight rollback'))
    pool.rollback_pool(dict(pool_plan={}))
    assert events==['clear_old']
