from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from main import parser,build_config
from common import researcher_instructions

def test_resource_notes_survive_configuration(tmp_path):
    notes=tmp_path/'notes.md';notes.write_text('Prepared baseline: /data/baseline')
    args=parser().parse_args(['start','math_rlvr','--instructions-file',str(notes)])
    config=build_config(args,{'category':'open_ended'})
    assert config['instructions']==notes.read_text()
    assert 'Prepared baseline: /data/baseline' in researcher_instructions(config,config['workers'][0],tmp_path)


def test_gpu_pool_is_shared_unless_operator_restricts_it(tmp_path):
    args = parser().parse_args(['start', 'p', '--gpus', '0,1,2,3,4,5,6,7',
                               '--worker-gpus', 'claude_1=2,3'])
    config = build_config(args, {'category': 'open_ended'})
    for worker in config['workers']:
        expected = [2, 3] if worker['name'] == 'claude_1' else list(range(8))
        assert worker['gpus'] == expected
        prompt = researcher_instructions(config, worker, tmp_path)
        assert 'none is assigned to you unless the operator explicitly specifies otherwise' in prompt
        assert 'do not pin GPUs or create a wrapper that narrows it' in prompt
        assert './gpu-run -- python your_experiment.py' in prompt
