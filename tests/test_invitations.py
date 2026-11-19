import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from main import parser,build_config
from common import initial_prompt


def test_invitation_per_worker_and_plain_launch_unchanged(tmp_path):
    args=parser().parse_args(['start','math_rlvr'])
    plain=build_config(args,{'category':'open_ended'})
    assert all('invitation_code' not in initial_prompt(plain,w,tmp_path) for w in plain['workers'])
    codes={w['name']:'private-code-'+w['name'] for w in plain['workers']}
    path=tmp_path/'invitations.json';path.write_text(json.dumps(codes));args.invitation_codes_file=path
    config=build_config(args,{'category':'open_ended'})
    for worker in config['workers']:
        prompt=initial_prompt(config,worker,tmp_path)
        assert worker['invitation_code'] in prompt
        assert 'POST /api/v1/agents/me/invitations' in prompt
        assert all(code not in prompt for name,code in codes.items() if name!=worker['name'])
    path.write_text(json.dumps({'codex_1':'only-one'}))
    with pytest.raises(ValueError,match='each worker'):build_config(args,{'category':'open_ended'})
    path.write_text(json.dumps(dict.fromkeys(codes,'duplicate')))
    with pytest.raises(ValueError,match='own'):build_config(args,{'category':'open_ended'})
