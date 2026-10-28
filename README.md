# FaceNinja skill for AI agents

Turn a photo into a finished MetaHuman head from your AI agent. This repository holds the open-source (MIT) FaceNinja
agent skill: a `SKILL.md` your agent reads and a small Python command-line client (standard library only) that calls
the [FaceNinja](https://faceninja.net/?utm_source=github&utm_medium=referral&utm_campaign=skill_repo) API.

What you get for each head:

- MetaHuman topology with **52 ARKit blendshapes**
- **4K skin** textures
- **GLB, FBX or a MetaHuman ZIP** for Unreal Engine (OBJ, STL and PLY on request)

The skill is free and MIT-licensed. Making heads is a paid service: each head costs FaceNinja credits, and there are no
free credits. The skill always tells you the cost and your balance before it spends anything, and it asks before any
batch of more than five heads.

## Install

**Any agent ([skills.sh](https://skills.sh))**: detects Claude Code, Codex, Cursor, Gemini CLI and many more.

```sh
npx skills add faceninjahq/skills                  # this project
npx skills add faceninjahq/skills -g               # every project
npx skills add faceninjahq/skills -a codex         # one agent: claude-code, codex, cursor, gemini-cli, antigravity, ...
```

**Claude Code** (plugin marketplace):

```sh
claude plugin marketplace add faceninjahq/skills
claude plugin install faceninja@faceninja
```

**Cursor**: `npx skills add faceninjahq/skills -a cursor`, or install the FaceNinja plugin from the Cursor
Marketplace once it is listed.

**Codex**: `npx skills add faceninjahq/skills -a codex` (installs to `.agents/skills/`, or `~/.agents/skills/` with `-g`).

**Gemini CLI** (extension):

```sh
gemini extensions install https://github.com/faceninjahq/skills
```

**Without Node** (installer from faceninja.net; copies the skill into every agent folder it finds):

```sh
curl -fsSL https://faceninja.net/skill/install.sh | sh                  # or: sh -s -- --claude | --codex | --cursor | --agents
```

```powershell
irm https://faceninja.net/skill/install.ps1 | iex
```

**By hand**: copy `skills/faceninja/` into your agent's skills folder (`~/.claude/skills/`, `~/.agents/skills/`,
`~/.cursor/skills/`, `~/.gemini/skills/`). Installing only copies these files; it runs nothing.

## Use

Ask your agent, for example:

> Use the FaceNinja skill to sign me in (show me the code to approve) and make a MetaHuman head from ./photo.jpg.
> Save the GLB and the MetaHuman ZIP in ./faceninja-out.

Sign-in uses a device code: the agent shows you a short code and you approve it in your browser on faceninja.net. The
agent never asks for your password. The API key is stored only on your computer (`~/.config/faceninja/credentials.json`,
mode 0600) and is never printed.

You can also run the client yourself:

```sh
python3 skills/faceninja/scripts/faceninja.py login
python3 skills/faceninja/scripts/faceninja.py balance
python3 skills/faceninja/scripts/faceninja.py make me.jpg --name Me --formats glb,zip --dry-run
```

## Updates

The client keeps the skill current. At most once an hour it checks `https://faceninja.net/skill/version.json`. When a
newer version exists, it downloads the skill's own files over HTTPS, checks each one against its SHA-256, keeps the old
files in `.previous/`, and swaps the new ones in. It only writes inside the skill's folder and runs nothing else.
Set `FACENINJA_NO_AUTOUPDATE=1` to turn this off; `faceninja.py update --rollback` goes back one version.

## Consent and safety

Only use photos of yourself, or of adults who gave you their informed consent. Before the first head, the client asks
you once on each computer to confirm this. No minors, and no public figures without their written permission. See the
[Content & Likeness Policy](https://faceninja.net/legal/content-policy).

## VTubers: the Creator Kit

The free [FaceNinja Creator Kit](https://faceninja.net/creators?utm_source=github&utm_medium=referral&utm_campaign=skill_repo)
is an Unreal project that turns your own head into a VTuber-style avatar with voice, lip sync and OBS.

## Links

- Website: <https://faceninja.net/?utm_source=github&utm_medium=referral&utm_campaign=skill_repo>
- API docs: <https://faceninja.net/docs/>
- Support: support@faceninja.net
- X: [@FaceNinjaHQ](https://x.com/FaceNinjaHQ)

## License

[MIT](LICENSE). The license covers this skill and its client. FaceNinja heads are subject to the
[Terms of Service](https://faceninja.net/legal/terms-of-service).
