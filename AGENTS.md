# rStation Agent

This repository owns the agent launcher, provider recovery, GPU helpers, coder and council. The rStation website and API are maintained separately in dualverse-ai/rstation.

Keep README.md concise and user-facing. Default to one Codex, one Claude and one Gemini researcher; coder and council on, supervisor off. Keep native conversations, workspaces and experiments intact during recovery. Coder runs directly in the researcher workspace without a central queue.

Credentials, CLI homes, agent_data and generated research are local state and must remain ignored. Do not copy production state into source or tests. Preserve bundled runtime attribution. Run relevant tests with .venv/bin/python -m pytest tests. Native CLI tests use mock providers unless real calls are explicitly authorized.

Make launcher changes here. Do not maintain a second source copy in the rStation service repository. Commit, push and deploy only within the owner's authorization.
