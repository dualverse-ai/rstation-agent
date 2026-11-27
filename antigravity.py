"""Antigravity's native stream and resume protocol, using the shared lifecycle."""
from dataclasses import replace
import json
from pathlib import Path

from common import read, save
from station.workers.cli import BaseCliWorkerBackend, PreparedCliWorkerLaunch


def provider_account_error(text):
    """Some gateways put an account-level error in a successful text response."""
    return isinstance(text,str) and text.strip().lower().startswith(
        "request is blocked due to the account restrictions for prohibited use policy")


def completed_turn(events):
    """Use current-turn evidence when agy repeats an earlier terminal error."""
    completed_replies = []
    completed_replies_valid = True
    response = ""
    response_step = None
    finished = False
    native_finished = False
    finish_incomplete = False
    failed = False
    saw_user = False
    result = {}
    for event in events:
        step = event.get("step_update", {})
        kind = step.get("step_type")
        if kind == "user_input":
            completed_replies.clear()
            completed_replies_valid = True
            response, response_step, finished, failed = "", None, False, False
            native_finished = False
            finish_incomplete = False
            result = {}
            saw_user = True
        elif kind == "error_message":
            failed = True
        elif kind == "agent_response":
            if step.get("step_index") != response_step:
                response, finished = "", False
                response_step = step.get("step_index")
            response += step.get("text_delta", "")
            finished = step.get("state") == "DONE"
            if finished and response.strip():
                # A terminal result can join multiple completed response steps.
                # Repeated/unknown step identities cannot establish that match.
                if response_step is None or any(index == response_step for index, _ in completed_replies):
                    completed_replies_valid = False
                completed_replies.append((response_step, response.strip()))
        elif kind == "tool":
            finished = native_finished = False
        elif kind == "finish":
            native_finished = step.get("state") == "DONE"
            finish_incomplete = not native_finished
        if event.get("event") == "result":
            result = event.get("result", {})
    if failed or finish_incomplete or provider_account_error(response) or provider_account_error(result.get("response")):
        return False
    if result.get("status") == "SUCCESS":
        return True
    # In agy 1.3.5, a successful resumed turn exits zero but its JSON result
    # can retain ERROR from history. Partial responses and fresh errors fail.
    terminal_response = result.get("response", "").strip()
    response_matches = terminal_response == response.strip() or (
        completed_replies_valid and len(completed_replies) > 1
        and terminal_response == "\n".join(text for _, text in completed_replies))
    return bool(result.get("status") == "ERROR" and saw_user and terminal_response
                and (native_finished or (finished and response.strip()
                     and response_matches)))


def prepare_profile(work, disable_web=False, model_name="gemini-4-carbon", context_tokens=500000, *, council=False, coder=False, adviser=False):
    home = Path(work) / ".home/.gemini"
    model = model_name if model_name.startswith("gemini-api://") else "gemini-api://" + model_name
    # This is the native model context budget, not an output or lifetime cap.
    # The bare custom-model selector otherwise leaves max_tokens unspecified.
    save(home / "antigravity-cli/settings.json", {
        "modelProvider": "gemini",
        "customModelsConfig": {"customModels": {model: {
            "modelName": model, "maxTokens": context_tokens,
        }}},
    })
    tools = ["run_command", "view_file",
             "write_to_file", "replace_file_content", "multi_replace_file_content",
             "list_dir", "find_by_name", "grep_search", "wait", "finish"]
    if not disable_web:
        tools += ["read_url_content", "search_web"]
    role = "rstation-council" if adviser else "rstation-researcher"
    if adviser:
        tools = ["view_file", "list_dir", "find_by_name", "grep_search", "finish"]
    if adviser:
        instructions="Provide research consultation. Read relevant local files using the read-only tools and listed workspaces. Do not execute code, edit files, run experiments or delegate.\n"
    else:
        instructions="Follow your research assignment. Read AGENTS.md for the available skills.\n"
        if coder:instructions+="Use ./coder for implementation and tests; focus on scientific ideas and interpretation.\n"
        if council:instructions+="Use ./council for outside scientific advice.\n"
        instructions+="Do not launch native subagents.\n"
    path = home / ("config/agents/" + role + ".md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nname: " + role + "\n"
                    "description: Independent rStation researcher\n"
                    "mainAgent: true\nsubagent: false\n"
                    "tools: " + json.dumps(tools) + "\n---\n"
                    + instructions)


def refresh_context_budget(work, model_name):
    """Apply an explicit run budget even when the parent controller has old code."""
    config = read(Path(work).parent / "run.json", {})
    if "gemini_context_tokens" not in config:
        return
    budget = int(config["gemini_context_tokens"])
    if budget <= 65536:
        raise ValueError("Context must exceed Gemini's 65,536-token output reserve.")
    path = Path(work) / ".home/.gemini/antigravity-cli/settings.json"
    settings = read(path)
    model = model_name if model_name.startswith("gemini-api://") else "gemini-api://" + model_name
    settings["customModelsConfig"]["customModels"][model]["maxTokens"] = budget
    save(path, settings)


class AntigravityResume(BaseCliWorkerBackend):
    backend_name = "gemini"
    supports_resume = True

    def prepare_launch(self, *, executable, run_dir, model_name, prompt, **kwargs):
        # API-key mode's built-in catalog can lag new Gemini releases. The native
        # custom-model selector passes the actual model ID to the configured API.
        model = model_name or "gemini-4-carbon"
        if not model.startswith("gemini-api://"):
            model = "gemini-api://" + model
        return PreparedCliWorkerLaunch(
            backend="gemini",
            command=[executable, "--model", model, "--agent", "rstation-researcher",
                     "--output-format", "stream-json", "--print-timeout", "0",
                     "--dangerously-skip-permissions", "-p", prompt],
            stdin_text=None, transcript_path=str(Path(run_dir) / "transcript.jsonl"),
            stderr_path=str(Path(run_dir) / "stderr.txt"), last_message_path=None,
            transcript_format="jsonl", env_overrides={})

    def prepare_resume_launch(self, *, resume_token, **kwargs):
        launch = self.prepare_launch(**kwargs)
        return replace(launch, command=launch.command + ["--conversation", resume_token])

    def extract_resume_token(self, path):
        try:
            with open(path) as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    token = (event.get("conversation_id") or
                             event.get("step_update", {}).get("conversation_id") or
                             event.get("result", {}).get("conversation_id"))
                    if token:
                        return token
        except FileNotFoundError:
            pass
        return None
