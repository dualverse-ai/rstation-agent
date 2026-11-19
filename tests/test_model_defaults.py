"""Carbon defaults must not remove legacy model/context selections."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from main import parser,build_config
from antigravity import prepare_profile
from council import MODELS

def test_new_worker_defaults_to_carbon():
 args=parser().parse_args(['start','test','--codex','0','--claude','0','--gemini','1'])
 c=build_config(args,{'category':'open_ended'})
 assert c['workers'][0]['model']=='gemini-4-carbon'
 assert c['gemini_context_tokens']==500000

def test_explicit_legacy_model_and_budget_remain_valid():
 args=parser().parse_args(['start','test','--codex','0','--claude','0','--gemini','1','--gemini-model','gemini-4-argon','--gemini-context-tokens','300000'])
 c=build_config(args,{'category':'open_ended'})
 assert c['workers'][0]['model']=='gemini-4-argon'
 assert c['gemini_context_tokens']==300000

def test_new_custom_selector_is_carbon_500k(tmp_path):
 prepare_profile(tmp_path)
 p=tmp_path/'.home/.gemini/antigravity-cli/settings.json'
 models=json.loads(p.read_text())['customModelsConfig']['customModels']
 assert models['gemini-api://gemini-4-carbon']=={'modelName':'gemini-api://gemini-4-carbon','maxTokens':500000}

def test_explicit_run_budget_is_preserved_for_carbon(tmp_path):
 prepare_profile(tmp_path,model_name='gemini-4-carbon',context_tokens=300000)
 models=json.loads((tmp_path/'.home/.gemini/antigravity-cli/settings.json').read_text())['customModelsConfig']['customModels']
 assert models['gemini-api://gemini-4-carbon']['maxTokens']==300000

def test_council_uses_carbon_first_but_keeps_saved_argon_eligible():
 assert MODELS['gemini'][0]=='gemini-4-carbon'
 assert 'gemini-4-argon' in MODELS['gemini']


def test_empty_native_model_uses_carbon_fallback(tmp_path):
 from antigravity import AntigravityResume
 launch=AntigravityResume().prepare_launch(executable='agy',run_dir=str(tmp_path),model_name='',prompt='test')
 assert launch.command[launch.command.index('--model')+1]=='gemini-api://gemini-4-carbon'
