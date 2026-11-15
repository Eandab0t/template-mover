# TemplateMover

Copy a Discord server structure from one guild to another: categories, text
and voice channels, roles, and permission overwrites.

Dry-run by default. Nothing is ever deleted, and existing target content is
never modified — roles, categories and channels that already exist under the
same name are reused and left untouched.

## Setup

- Python 3.8+ with `pip install discord.py`
- A bot token — no privileged intents are needed, only the default guilds intent
- The bot must be a member of both guilds and have **Manage Channels** +
  **Manage Roles** in the target guild

## Usage

```bash
# dry run - shows everything that would be created, changes nothing
python templatemover.py --template-id 111111111111111111 --target-id 222222222222222222

# apply - prints the same preview, asks for confirmation, then creates
python templatemover.py --template-id 111111111111111111 --target-id 222222222222222222 --apply
```

The token comes from the `DISCORD_TOKEN` env var, or pass `--token`.

## Mapping rules

| Template object | Behavior |
|---|---|
| Role | Created with same name/color/hoist/mentionable/permissions; if a target role with that name exists it is **reused**; managed (bot) roles are skipped |
| @everyone overwrites | Mapped to the target's @everyone |
| Category | Created with mapped overwrites; reused if the name exists in the target |
| Text channel | Created in the mapped category with topic/slowmode/NSFW; announcement channels stay announcements; reused if the same name exists in the same category |
| Voice channel | Created with user limit and bitrate; reused if the same name exists in the same category |
| Member overwrites | Skipped with a note (users differ between guilds) |
| Forum / stage channels | Skipped with a note (out of scope) |

Reused objects are never touched: no permissions, topics or settings are
changed on anything that already exists in the target.

## Export file

After a successful apply, the tool writes `template-export-<timestamp>.json`
listing everything that was created (roles, categories, channels with their
new IDs) plus everything that was skipped. That file is your record of what
the tool changed — nothing is ever removed automatically.

## Tests

Offline (no network, no token):

```bash
python tests/test_templatemover.py
```

Covers permission-overwrite copying and dry-run planning: allow/deny
splitting, cross-guild role mapping, member-overwrite skipping, reuse
mapping, unsupported-type notes, plan ordering, and that a dry run never
mutates anything.
