---
name: coder
description: Delegate implementation, debugging and tests to the fixed Codex coder so you can focus on scientific ideas and interpretation.
---

Delegate coding to coder; give it the scientific intent, relevant files and acceptance tests. Focus your own attention on science and review the code behind reported results.

```sh
./coder run --session ablation --prompt-file coding-brief.md
./coder read ablation
./coder resume ablation --prompt "Add the missing edge-case test."
```

Your shell starts a local Codex process for each task in your workspace and research environment; it edits the files there. Independent tasks run concurrently. No copy or apply step is needed. Avoid editing the same files while it works. Optional `--path my_project` selects its starting directory. The operator fixes its model, normally `gpt-6.2-sol`; web access follows your run's setting.

Commands return promptly; a private message announces completion. `./coder status` lists tasks and `./coder cancel ablation` stops one. Coder runs CPU checks; you run scientific experiments using your configured resources.
