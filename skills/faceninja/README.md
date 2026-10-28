# FaceNinja skill for AI agents

This folder teaches an AI coding agent (Claude Code, Codex, Cursor, Gemini CLI, OpenClaw, Aider, or anything that can
run a shell command) to make MetaHuman heads with [FaceNinja](https://faceninja.net): photos in, a 3D head with
52 ARKit blendshapes, 4K skin maps and a MetaHuman ZIP for Unreal out.

| File | What it is |
| --- | --- |
| `SKILL.md` | The instructions the agent reads (open Agent Skills format). |
| `scripts/faceninja.py` | The command-line client. Python 3.9+, standard library only; works on its own too. |
| `reference/api.md` | Short API notes the agent reads when it needs more than the script. |
| `LICENSE` | MIT. The skill and script are free to use, change and share; making heads needs a FaceNinja account and credits. |

## Install

```sh
curl -fsSL https://faceninja.net/skill/install.sh | sh              # every agent folder it finds
curl -fsSL https://faceninja.net/skill/install.sh | sh -s -- --claude   # or --codex, --cursor, --agents
```

```powershell
irm https://faceninja.net/skill/install.ps1 | iex
```

```sh
npx skills add faceninjahq/skills                     # any agent; -a <agent> (claude-code, codex, cursor), -g for all projects
claude plugin marketplace add faceninjahq/skills      # Claude Code plugin, then:
claude plugin install faceninja@faceninja
```

Or unpack https://faceninja.net/skill/faceninja-skill.zip into your agent's skills folder
(`~/.claude/skills/`, `~/.codex/skills/`, `.cursor/skills/`, `.agents/skills/`). The installers change nothing else.

## Use

Tell your agent, for example:

> Read https://faceninja.net/skill/SKILL.md and install the FaceNinja skill it describes. Then sign me in with it
> (it will show me a code to approve) and make a MetaHuman head from ./photo.jpg. Save the GLB and the MetaHuman ZIP in
> ./faceninja-out.

Or use the script yourself:

```sh
python3 scripts/faceninja.py login                 # opens faceninja.net/device; approve the code shown
python3 scripts/faceninja.py balance
python3 scripts/faceninja.py make me.jpg me-side.jpg --name Me --formats glb,fbx,zip
python3 scripts/faceninja.py batch ./cast --per-person-subfolders --dry-run
```

## Updates

The skill keeps itself current: at most once an hour a command checks https://faceninja.net/skill/version.json. A newer
version is downloaded, every file is checked against its SHA-256, the files are swapped in (the old ones stay in
`.previous/`) and the command runs on with the new code. It only writes inside this folder and runs nothing else.
`faceninja.py update --check` looks, `faceninja.py update --rollback` goes back one version, and
`FACENINJA_NO_AUTOUPDATE=1` turns automatic updates off. `faceninja.py guidance` shows FaceNinja's current notes for
agents (limits, recommended formats, maintenance windows).

## Money and safety

- A head costs 1,500 credits (USD 15). Failed heads are refunded; downloads are free. The skill tells you the cost and
  your balance before it spends, and stops to ask before any batch of more than 5 heads (`--yes`).
- Signing in creates an API key named after the agent and computer ("Claude Code on my-laptop"). It is stored in
  `~/.config/faceninja/credentials.json` (`%APPDATA%\FaceNinja\credentials.json` on Windows) readable by you only, and
  is never printed. Revoke it in Factory → API keys or with `faceninja.py logout`.
- If you paste a key into an agent instead (`FACENINJA_API_KEY`), the agent's transcript keeps it: revoke it when done.
- Only photos of yourself or of adults who gave you their informed consent; you are responsible for that and for how you
  use the heads. Before the first head the script asks you once, on each computer, to confirm it (`faceninja.py attest`).
  See https://faceninja.net/legal/content-policy.
