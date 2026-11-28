import json

from fleet_collect import gemini_tool_observation, rotation_observation
from fleet_dashboard import Store
from test_fleet_dashboard import row


def test_all_failed_keys_visible_across_moves_and_recovery(tmp_path):
    store=Store(tmp_path/'db')
    keys=['a'*64,'b'*64,'c'*64,'d'*64]
    inventory=[dict(id=k,name='Gemini '+str(i),endpoint=f'https://p{i}.example',
                    family='gemini',enabled=i<3) for i,k in enumerate(keys)]
    outcomes={k:dict(model='carbon',status='errors',http_status=503,at=19990+i) for i,k in enumerate(keys[:3])}
    rotation=dict(version=1,enabled=keys[:3],key=keys[2],model='carbon',exhausted=True,
                  exhausted_since=19992,observations=outcomes,model_observations={})
    held=row(id='retired',provider_key_id=keys[3],intentional_stop=True,state='stopped')
    def sample(at):
        store.record({'host':'h4','sampled_at':at,'provider_keys':inventory,
            'workers':[held,row(sampled_at=at,**rotation_observation(rotation))]})
        return store.view(at)
    snap=sample(20000)
    assert {k['id']:k['status'] for k in snap['provider_keys']}=={**dict.fromkeys(keys[:3],'errors'),keys[3]:'disabled'}
    active=next(w for w in snap['workers'] if w['id']=='one')
    assert not active['needs_attention']
    assert active['gemini_incidents']=={'routes_exhausted':20000}
    rotation.update(exhausted=False,model_observations={'neon':{keys[0]:dict(status='working',at=20009)}})
    snap=sample(20010)
    item=next(k for k in snap['provider_keys'] if k['id']==keys[0])
    assert item['status']=='mixed'
    assert item['model_status']['carbon']['http_status']==503
    assert item['model_status']['neon']['status']=='working'
    assert not next(w for w in snap['workers'] if w['id']=='one')['gemini_incidents']
    outcomes[keys[0]]=dict(model='carbon',at=20011,status='working')
    assert next(k for k in sample(20012)['provider_keys'] if k['id']==keys[0])['status']=='working'
    del outcomes[keys[1]]
    assert next(k for k in sample(20013)['provider_keys'] if k['id']==keys[1])['status']=='untried'


def test_repeated_reads_require_same_parameters_and_distinct_steps(tmp_path):
    folder=tmp_path/'launch_000001'
    folder.mkdir()
    transcript=folder/'transcript.jsonl'
    state=dict(token='session',launches=1)
    def step(index, params):
        return {'step_update':dict(conversation_id='session',step_type='tool',state='DONE',
            step_index=index,tool_name='read_file',tool_info={'parameters':json.dumps(params)})}
    def check(events):
        transcript.write_text(''.join(json.dumps(e)+'\n' for e in events))
        return gemini_tool_observation(tmp_path,state)
    read=step(0,{'Path':'private.txt'})
    assert not check([read]*7)
    assert not check([step(i,{'Path':'private.txt','StartLine':i}) for i in range(7)])
    events=[step(i,{'Path':'private.txt'}) for i in range(5)]
    result=check(events)
    assert len(result['identical_reads'])==24
    assert 'private.txt' not in str(result)
    assert not check(events+[{'event':'result'}])


def test_long_command_alert_persists_and_advancing_gpu_suppresses(tmp_path):
    store=Store(tmp_path/'db')
    def verify(t, step=None, tool='a'):
        sample=row(sampled_at=t,gemini_tool_observation={'pending_command':tool},foreground_job=step is not None,
                   job_progress_samples=[dict(id='job',step=step)] if step is not None else [])
        store.record(dict(host='h4',sampled_at=t,workers=[sample]))
        return store.view(t)['workers'][0]['gemini_incidents']
    assert not verify(20000)
    assert verify(23600)=={'long_command':20000}
    verify(23610,10)
    assert not verify(23620,11)
    assert not verify(23630,11,'b')


def test_first_live_attempt_is_distinguishable_from_an_unused_route():
    from fleet_dashboard import key_observation
    active=row(gemini_enabled=['a','b'],provider_key_id='a',gemini_attempt_started=19999,
               process_alive=True,last_response=19000)
    assert key_observation('a',[active],20000,True)['status']=='attempting'
    assert key_observation('b',[active],20000,True)['status']=='untried'


def test_retired_inventory_uses_current_host_policy(tmp_path,monkeypatch):
    import fleet_collect as fc
    enabled='a'*64
    active=tmp_path/'active'
    retired=tmp_path/'retired'
    (active/'agent_data').mkdir(parents=True)
    (active/'gemini-routing.json').write_text(json.dumps(dict(enabled=[enabled])))
    run=retired/'agent_data/old';run.mkdir(parents=True)
    (run/'STOP').touch()
    (run/'run.json').write_text(json.dumps(dict(workers=[dict(kind='gemini',name='gemini_1')])))
    monkeypatch.setattr(fc,'worker_key_inventory',lambda *a:([dict(id='b'*64,name='Gemini backup 1',family='gemini')],None))
    data=fc.collect([active,retired],'h4')
    assert data['provider_keys'][0]['enabled'] is False


def test_enabled_host_report_includes_backup_inventory_from_other_installation(tmp_path):
    store=Store(tmp_path/'db')
    key='a'*64
    store.record(dict(host='h4',sampled_at=20000,workers=[],provider_keys=[
        dict(id=key,name='Gemini backup 1',family='gemini',endpoint='https://example',enabled=False)]))
    store.record(dict(host='h6',sampled_at=20000,workers=[row(host='h6',gemini_enabled=[key])],provider_keys=[]))
    item=store.view(20000)['provider_keys'][0]
    assert item['status']=='untried' and item['hosts']==['h4','h6']
