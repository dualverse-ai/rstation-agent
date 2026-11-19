import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common
from main import parser, build_config
from recovery import routes


def test_recommended_roster_and_helpers():
    args = parser().parse_args(['start', 'no_three_in_line'])
    config = build_config(args, {'category': 'open_ended'})
    assert [(w['kind'], w['name']) for w in config['workers']] == [('codex', 'codex_1'), ('claude', 'claude_1'), ('gemini', 'gemini_1')]
    assert config['url'] == 'https://rstation.ai'
    assert config['coder'] and config['council'] and not args.supervisor


@pytest.mark.parametrize('kind', ['codex', 'claude', 'gemini'])
def test_public_primary_and_backup_settings(kind):
    prefix = kind.upper() + '_'
    env = {prefix+'API_KEY': 'primary-test', prefix+'BASE_URL': 'https://primary.example/v1',
           prefix+'BACKUP_API_KEY': 'backup-test', prefix+'BACKUP_BASE_URL': ''}
    result = routes(kind, env)
    assert [(r['api_key'], r['base_url']) for r in result] == [('primary-test', 'https://primary.example/v1'), ('backup-test', 'https://primary.example/v1')]
    env[prefix+'BACKUP_BASE_URL'] = 'https://backup.example/v1'
    assert routes(kind, env)[1]['base_url'] == 'https://backup.example/v1'


@pytest.mark.parametrize('exported', ['CODEX_API_KEY', 'RSTATION_WORKER_CODEX_API_KEY'])
def test_exported_settings_override_dotenv(tmp_path, monkeypatch, exported):
    monkeypatch.setattr(common, 'ROOT', tmp_path)
    for key in list(common.os.environ):
        if key.startswith(('CODEX_', 'RSTATION_WORKER_CODEX_', 'RSTATION_MANAGED_ENVIRONMENT')):
            monkeypatch.delenv(key)
    (tmp_path/'.env').write_text('CODEX_API_KEY=file-test\nCODEX_BASE_URL=https://primary.example/v1\n')
    monkeypatch.setenv(exported, 'shell-test')
    assert routes('codex', common.safe_environment())[0]['api_key'] == 'shell-test'
    monkeypatch.setenv(exported, '')
    with pytest.raises(ValueError):
        routes('codex', common.safe_environment())


def test_council_uses_only_configured_families(tmp_path):
    from test_council import setup_run
    from common import save
    from council import configure, Store
    run, config, env = setup_run(tmp_path)
    config['workers'] = [w for w in config['workers'] if w['kind'] != 'gemini']
    save(run/'run.json', config)
    for key in list(env):
        if 'GEMINI' in key or key == 'ANTIGRAVITY_BIN_PATH':
            env.pop(key)
    configure(run, 'all', True, env, statement='Research task', notify=False)
    assert set(Store(run).policy()['models']) == {'codex', 'claude'}
