"""Optional native CLI assistants and Station-compatible reasoning defaults."""
import json
from pathlib import Path
import re
import subprocess

from station.file_io_utils import save_text

# Versions exercised with the native delegation contract. Older CLIs can ignore
# unknown limits, so reject them before starting an enabled run.
MIN_CLI_VERSIONS = {"codex": (0, 162, 0), "claude": (2, 1, 295)}


def check_cli_version(kind, executable, env):
    result = subprocess.run([executable, "--version"], env=env, capture_output=True,
                            text=True, timeout=15, check=True)
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout)
    minimum = MIN_CLI_VERSIONS[kind]
    if not match or tuple(map(int, match.groups())) < minimum:
        required = ".".join(map(str, minimum))
        raise ValueError(f"Subagents require {kind} {required} or newer. Upgrade the CLI and rerun --check.")


def reasoning_effort(kind, model):
    # Keep research effort explicit without importing live Station configuration.
    # GPT-6 Astra supports max effort; assistants inherit the same setting.
    model = model.strip().lower()
    if kind == "codex":
        return "max" if model.startswith(("gpt-5.6-", "gpt-6-astra", "gpt-6.2-astra")) else "xhigh"
    return "max" if re.match(r"claude-opus-4-6(?:-|$)", model) else "high"


def researcher_guidance(count, kind=None):
    slots = "slot" if count == 1 else "slots"
    role = (" Use the research_assistant role with fork_context=false and a self-contained assignment." if kind == "codex" else
            " Use general-purpose assistants and inherit your model and thinking level.")
    return (f"You have {count} subagent {slots} available. You are encouraged to use "
            "subagents for writing and debugging code, running experiments, "
            "and literature surveys, while you focus on research strategy, developing hypotheses, "
            "interpreting results, and deciding what to pursue next. Give each assistant a focused "
            "assignment and review its findings before drawing scientific conclusions. Assistants "
            "share your workspace and resource budget; coordinate concurrent work accordingly. "
            "They should return their results to you, rather than register or publish independently. "
            "The same research access restrictions apply to them." + role)


def assistant_instructions(disable_web):
    scope = ("This is an isolated research run. Use only this rStation instance, the current "
             "workspace and preinstalled libraries. Do not use external websites, search, "
             "downloads, or previous runs and their artifacts." if disable_web else
             "Use the researcher's permitted sources when surveying literature.")
    return ("Complete the focused coding, experiment, or literature-survey assignment given by "
            "the researcher. Implement the requested method faithfully, debug it, and check outputs. "
            "If you cannot implement it or need to change it, explain that explicitly; an "
            "implementation failure is not evidence against the scientific idea. Return a concise "
            "report with findings, artifact paths and remaining limitations. Share the researcher's "
            "workspace and resource budget, coordinate file edits, and use ./gpu-run for any "
            "permitted GPU experiment. Do not register, publish, or spawn further assistants. " + scope)


def codex_delegation_config(work, model, count, disable_web):
    if not count:
        return ('agents.enabled=false',)
    # The main-researcher upgrade does not upgrade native coding assistants.
    if model == 'gpt-6.2-astra':model = 'gpt-6-astra'
    # A custom role supplies a bounded assignment instead of the researcher's
    # continuous-research instructions. It also fixes model/effort inheritance.
    effort = reasoning_effort("codex", model)
    path = Path(work)/".codex/agents/research-assistant.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (f'name = "research_assistant"\n'
               f'description = "Coding, experiments and literature surveys for the researcher."\n'
               f'model = {json.dumps(model)}\n'
               f'model_reasoning_effort = {json.dumps(effort)}\n'
               f'developer_instructions = {json.dumps(assistant_instructions(disable_web))}\n'
               '[agents]\nenabled = false\n')
    save_text(content, str(path))
    return ('agents.enabled=true',f'agents.max_concurrent_threads_per_session={count}',
            'agents.default_subagent_model='+json.dumps(model),
            'agents.default_subagent_reasoning_effort='+json.dumps(effort))
