import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import researcher_instructions, read, save
from main import parser, build_config
from recovery import ResearchManager
from subagents import check_cli_version, reasoning_effort
from station.workers.job_manager import CliJobLaunchDecision


def config(tmp_path, kind='codex', count=0, disabled=False):
    flags = ['start', 'test_problem', '--gemini', '0', '--codex', str(int(kind == 'codex')),
             '--claude', str(int(kind == 'claude')), '--subagents', str(count)]
    if disabled:
        flags.append('--disable-web')
    return build_config(parser().parse_args(flags), {'category': 'open_ended'})


def test_default_and_saved_slot_count(tmp_path):
    args = parser().parse_args(['start', 'p'])
    c = build_config(args, {'category': 'open_ended'})
    assert c['subagents'] == 0
    baseline = researcher_instructions(c, c['workers'][0], tmp_path)
    assert 'subagent' not in baseline
    legacy = dict(c)
    legacy.pop('subagents')
    assert researcher_instructions(legacy, legacy['workers'][0], tmp_path) == baseline
    c['subagents'] = 2
    save(tmp_path/'run.json', c)
    restored = read(tmp_path/'run.json')
    prompt = researcher_instructions(restored, restored['workers'][0], tmp_path)
    assert '2 subagent slots' in prompt
    for text in ['literature surveys', 'research strategy', 'interpreting results',
                 'share your workspace and resource budget', 'rather than register']:
        assert text in prompt


def test_invalid_slot_counts():
    with pytest.raises(ValueError, match='nonnegative'):
        build_config(parser().parse_args(['start', 'p', '--gemini', '0', '--subagents', '-1']), {'category': 'open_ended'})
    with pytest.raises(SystemExit):
        parser().parse_args(['start', 'p', '--subagents', '1.5'])


@pytest.mark.parametrize('kind,model,expected', [
    ('codex', 'gpt-5.5', 'xhigh'), ('codex', 'gpt-6-astra', 'max'),
    ('codex', 'gpt-6.2-astra', 'max'),
    ('codex', 'gpt-5.6-sol', 'max'), ('claude', 'claude-opus-4-8', 'high'),
    ('claude', 'claude-opus-5-5', 'high'), ('claude', 'claude-opus-4-6', 'max'),
    ('claude', 'claude-opus-4-6-20260101', 'max'), ('claude', 'claude-sonnet-4-6', 'high'),
])
def test_station_reasoning_defaults(kind, model, expected):
    assert reasoning_effort(kind, model) == expected


@pytest.mark.parametrize('kind', ['codex', 'claude'])
@pytest.mark.parametrize('count', [0, 1, 2])
@pytest.mark.parametrize('disabled', [False, True])
@pytest.mark.parametrize('resuming', [False, True])
def test_real_launch_configuration(tmp_path, kind, count, disabled, resuming):
    c = config(tmp_path, kind, count, disabled)
    worker = c['workers'][0]
    name = worker['name']
    # No model call: use a harmless executable only to exercise launch assembly.
    env = dict(os.environ, CODEX_BIN_PATH='/bin/true', CLAUDE_BIN_PATH='/bin/true',
               RSTATION_WORKER_CODEX_API_KEY='test', RSTATION_WORKER_CODEX_BASE_URL='http://localhost:1',
               RSTATION_WORKER_CLAUDE_API_KEY='test', RSTATION_WORKER_CLAUDE_BASE_URL='http://localhost:1')
    manager = ResearchManager(tmp_path, c, env)
    decision = CliJobLaunchDecision('resume' if resuming else 'fresh', 1, 0,
                                   'saved-session' if resuming else None, 0)
    try:
        claimed = manager._claim_cli_job_launch(name, 'launch_test', decision)
        spec = manager._build_cli_job_launch_spec(name, 'launch_test', decision, claimed)
        prepared = manager._prepare_cli_job_launch(spec, decision, manager._get_cli_job_backend)
        cmd = prepared.command
        effort = reasoning_effort(kind, worker['model'])
        if resuming:
            assert 'saved-session' in cmd
        if kind == 'codex':
            assert f'model_reasoning_effort="{effort}"' in cmd
            assert f'agents.enabled={str(bool(count)).lower()}' in cmd
            role_path = tmp_path/name/'.codex/agents/research-assistant.toml'
            if count:
                assert f'agents.max_concurrent_threads_per_session={count}' in cmd
                assert 'agents.default_subagent_model="gpt-6-astra"' in cmd
                role = tomllib.loads(role_path.read_text())
                assert role['model'] == 'gpt-6-astra'
                assert role['model_reasoning_effort'] == 'max'
                assert role['agents']['enabled'] is False
                assert ('Do not use external websites' in role['developer_instructions']) is disabled
            else:
                assert not role_path.exists()
        else:
            assert cmd[cmd.index('--effort')+1] == effort
            assert ('--bare' in cmd) is not bool(count)
            if count:
                assert '--safe-mode' in cmd
                assert '--forward-subagent-text' in cmd
                tools = cmd[cmd.index('--tools')+1].split(',')
                assert 'Agent' in tools
                assert ('WebSearch' in tools) is not disabled
                assert ('WebFetch' in tools) is not disabled
                instructions = cmd[cmd.index('--append-subagent-system-prompt')+1]
                assert ('Do not use external websites' in instructions) is disabled
                assert 'Do not register, publish, or spawn further assistants' in instructions
                assert spec.env['CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS'] == str(count)
                assert spec.env['CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH'] == '1'
                assert spec.env['CLAUDE_CODE_SUBAGENT_MODEL'] == worker['model']
            else:
                assert '--agents' not in cmd
                assert 'Agent' in cmd and 'Task' in cmd
        assert ('WebSearch' in cmd) is (disabled and kind == 'claude')
    finally:
        manager.close()


@pytest.mark.parametrize('kind,version,valid', [
    ('codex', 'codex-cli 0.156.1', False), ('codex', 'codex-cli 0.162.0', True),
    ('claude', '2.1.81 (Claude Code)', False), ('claude', '2.1.295 (Claude Code)', True),
    ('claude', '2.1.300 (Claude Code)', True), ('codex', 'unknown', False),
])
def test_version_guard(tmp_path, kind, version, valid):
    cli = tmp_path/'cli'
    cli.write_text('#!'+sys.executable+'\nprint('+repr(version)+')\n')
    cli.chmod(0o755)
    if valid:
        check_cli_version(kind, str(cli), dict(os.environ))
    else:
        with pytest.raises(ValueError, match='Upgrade the CLI'):
            check_cli_version(kind, str(cli), dict(os.environ))
