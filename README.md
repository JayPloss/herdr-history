# herdr history

A [herdr](https://herdr.dev) plugin that remembers every space (workspace), open or
closed, and brings closed ones back.

- **Records** each space from herdr's own view: name, folder, tabs, panes, the agent
  kind herdr detected and the terminal title (the agent's topic), with times. It runs
  on herdr events (workspace/tab/pane lifecycle, agent detected, agent status) and at
  startup. Bursts of events collapse into one snapshot.
- **Picker** (`prefix+shift+h` in Jay's config, or `herdr plugin action invoke
  jayploss.history.open`): every space, open first, then by last activity, with a
  preview of its tabs and agent runs. Enter focuses an open space, or recreates a
  closed one (workspace, tabs, splits, folders) and restarts its agents.
- **Agent-neutral.** Nothing depends on one agent vendor. A native session id is used
  only when a herdr integration reported one, through the `RESUME` table in `hh.py`
  (claude, codex); other agents start fresh in the right folder.

Data: `~/.local/state/herdr/plugins/jayploss.history/history.sqlite` (herdr's plugin
state dir). Needs Python 3 and fzf.

```bash
herdr plugin link ~/Work/herdr-history
```
