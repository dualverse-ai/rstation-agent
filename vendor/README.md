# Station source

SOURCE.json records the Station commit and the exact copied files. Those modules are used directly by the agent launcher, with adapters in recovery.py and gpu.py. The small constants.py supplies only worker settings and does not load live Station state.

The Codex launch, retry scheduling, session handling and GPU file format come from these modules. Claude's persistent-session support and internal API retry detection are added by the launcher adapter because the copied Station Claude backend does not provide them.
