# herdr-history

herdr plugin: `herdr-plugin.toml` (hooks, action, popup pane) + `hh.py` (stdlib Python, fzf).

- Linked locally (`herdr plugin link`), so edits to `hh.py` take effect on the next event;
  manifest changes need `herdr plugin unlink jayploss.history && herdr plugin link .`.
- Test against the live server without touching the user's screen: create workspaces
  with `--no-focus`, use `hh.py open ID --no-focus`, render the picker by sending
  `python3 hh.py pick` into a hidden workspace's pane and reading it with `herdr pane read`.
  Delete test spaces from the DB afterwards.
- Hook logs: `herdr plugin log list --plugin jayploss.history`.
- Must stay model/provider-neutral: per-agent behaviour lives only in the `RESUME` table.
