---
name: council
description: Consult another model family for scientific ideas, criticism and interpretation, especially before a major pivot or after repeated inconclusive experiments.
---

Use council to challenge your scientific reasoning. Describe the question and point to relevant workspace files; advisers can read them directly. Check `./council models` and choose a family different from yours.

```sh
./council run claude --session mechanism --prompt "Read results/summary.csv. What mechanism explains this pattern?"
./council read mechanism
./council resume mechanism --prompt-file followup.md
```

Commands return promptly; a private message announces the reply. Continue useful research while waiting. `./council status` shows progress, `read --all` shows the discussion, and `cancel SESSION` stops a consultation. Resume without a prompt retries an interrupted turn in the same conversation.

Consult before a major change of direction or after repeated uninformative results. Record what you adopt or reject and why in your research notes. Council advises; use coder for implementation. You retain scientific judgment.
