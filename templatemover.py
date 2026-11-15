#!/usr/bin/env python3
"""TemplateMover - copy a Discord server structure to another guild.

Copies categories, text/voice channels, roles and permission overwrites from
a template guild to a target guild.

Dry-run by default: prints exactly what would be created, changes nothing.
--apply asks for confirmation, then creates the missing objects and writes a
template-export-<timestamp>.json file listing everything that was created.

The tool never deletes or modifies existing target-guild content: roles,
categories and channels that already exist under the same name are reused
and left untouched.

Requires: discord.py (pip install discord.py) and a bot token with the
guilds intent enabled, member of both guilds, with Manage Channels +
Manage Roles in the target guild.
"""

import argparse
import asyncio
import json
import os
import sys
import time

import discord

REASON = "Template clone"


# --- output: [+] ok  [!] needs attention  [x] failure -------------------------

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
if USE_COLOR and os.name == "nt":
    os.system("")  # enable ANSI escape codes on Windows terminals


def paint(text, code):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def ok(msg):
    print(paint("[+]", "32"), msg)


def warn(msg):
    print(paint("[!]", "33"), msg)


def fail(msg):
    print(paint("[x]", "31"), msg, file=sys.stderr)


def section(name):
    print()
    print(paint(f"== {name} ==", "36"))


# --- pure plan helpers (unit-tested, no API access) ---------------------------

def overwrites_to_pairs(perms):
    """Split a Permissions object into (allow_names, deny_names)."""
    allow, deny = [], []
    for flag, value in perms:
        if value is True:
            allow.append(flag)
        elif value is False:
            deny.append(flag)
    return allow, deny


def map_overwrites(overwrites, role_map):
    """Rebuild overwrites for the target guild.

    Only mapped roles survive; member overwrites and unmapped roles are
    dropped with a note (users and managed roles cannot be copied across).
    """
    entries, notes = [], []
    for target_obj, perms in overwrites.items():
        mapped = role_map.get(target_obj.id)
        if mapped is None:
            name = getattr(target_obj, "name", f"id {target_obj.id}")
            notes.append(f"skipped overwrite for '{name}' (unmapped role or member)")
            continue
        allow, deny = overwrites_to_pairs(perms)
        if allow or deny:
            entries.append({"role": mapped, "allow": allow, "deny": deny})
    return entries, notes


def plan_channel(channel, role_map, plan, notes, category_name, existing_channels):
    if channel.type in (discord.ChannelType.text, discord.ChannelType.news):
        kind = "text"
    elif channel.type is discord.ChannelType.voice:
        kind = "voice"
    else:
        notes.append(f"skip {channel.type.name} channel '{channel.name}' (not supported)")
        return
    key = (category_name.lower() if category_name else None, channel.name.lower())
    if key in existing_channels:
        notes.append(f"channel #{channel.name} already exists in target - reusing it, left untouched")
        return
    entries, ch_notes = map_overwrites(channel.overwrites, role_map)
    notes.extend(ch_notes)
    item = {"action": "channel", "kind": kind, "name": channel.name,
            "category": category_name, "overwrites": entries}
    if kind == "text":
        item.update(topic=channel.topic or "", slowmode=channel.slowmode_delay,
                    nsfw=channel.nsfw, news=channel.type is discord.ChannelType.news)
    else:
        item.update(user_limit=channel.user_limit, bitrate=channel.bitrate)
    plan.append(item)


def plan_guild(template, target):
    """Build the creation plan (roles first, then categories, then channels)."""
    plan, notes = [], []
    role_map = {template.default_role.id: target.default_role.name}
    planned_names = set()
    target_role_names = {r.name.lower() for r in target.roles
                         if not r.is_default() and not r.is_managed()}
    for role in sorted(template.roles, key=lambda r: r.position, reverse=True):
        if role.is_default():
            continue
        if role.is_managed():
            notes.append(f"skip managed role @{role.name}")
            continue
        if role.name.lower() in target_role_names:
            role_map[role.id] = role.name
            notes.append(f"role '{role.name}' -> reusing existing target role")
        elif role.name.lower() in planned_names:
            role_map[role.id] = role.name
            notes.append(f"template has duplicate role '{role.name}'; mapping both to one target role")
        else:
            planned_names.add(role.name.lower())
            perm_names = [flag for flag, value in role.permissions if value]
            plan.append({"action": "role", "name": role.name, "template_id": role.id,
                         "color": str(role.colour) if role.colour.value else None,
                         "hoist": role.hoist, "mentionable": role.mentionable,
                         "permissions": role.permissions.value, "permission_names": perm_names})
            role_map[role.id] = role.name

    target_cat_names = {c.name.lower() for c in target.categories}
    existing_channels = {(c.category.name.lower() if c.category else None, c.name.lower())
                         for c in target.channels}
    for cat in template.categories:
        entries, cat_notes = map_overwrites(cat.overwrites, role_map)
        notes.extend(cat_notes)
        if cat.name.lower() in target_cat_names:
            notes.append(f"category '{cat.name}' already exists in target - reusing it, left untouched")
        else:
            plan.append({"action": "category", "name": cat.name, "overwrites": entries})
        for channel in cat.channels:
            plan_channel(channel, role_map, plan, notes, cat.name, existing_channels)
    for channel in template.channels:
        if channel.category is None and channel.type is not discord.ChannelType.category:
            plan_channel(channel, role_map, plan, notes, None, existing_channels)
    return plan, notes


def confirmed(count, target_name, input_fn=input):
    try:
        answer = input_fn(f"Type APPLY to create {count} item(s) in '{target_name}': ").strip()
    except EOFError:
        return False
    return answer == "APPLY"


def format_overwrites(entries):
    if not entries:
        return ""
    parts = []
    for entry in entries:
        bits = []
        if entry["allow"]:
            bits.append("+" + ",".join(entry["allow"]))
        if entry["deny"]:
            bits.append("-" + ",".join(entry["deny"]))
        parts.append(f"{entry['role']} ({', '.join(bits)})")
    return "    overwrites: " + "; ".join(parts)


def print_plan(plan, notes, template, target, apply_mode):
    print("TemplateMover")
    print(f"template : {template.name} ({template.id})")
    print(f"target   : {target.name} ({target.id})")
    print("mode     : " + ("apply" if apply_mode else "dry run (nothing will be created)"))
    labels = {"role": "Roles", "category": "Categories", "channel": "Channels"}
    for action, label in labels.items():
        items = [i for i in plan if i["action"] == action]
        section(label)
        for item in items:
            if action == "role":
                perms = ", ".join(item["permission_names"][:6]) or "none"
                if len(item["permission_names"]) > 6:
                    perms += f", +{len(item['permission_names']) - 6} more"
                extras = [item["color"], "hoist" if item["hoist"] else None]
                extras = ", ".join(e for e in extras if e)
                ok(f"create role {item['name']} ({perms}{(' | ' + extras) if extras else ''})")
            elif action == "category":
                ok(f"create category {item['name']}")
            elif item["kind"] == "text":
                where = f" in {item['category']}" if item["category"] else ""
                news = ", announcement" if item["news"] else ""
                ok(f"create text #{item['name']}{where}{news}")
            else:
                where = f" in {item['category']}" if item["category"] else ""
                ok(f"create voice #{item['name']}{where} (limit {item['user_limit']})")
            line = format_overwrites(item.get("overwrites", []))
            if line:
                print(line)
    section("Notes")
    if notes:
        for note in notes:
            warn(note)
    else:
        ok("nothing skipped")


# --- apply (the only code that touches the Discord API mutatively) ------------

def resolve_overwrites(entries, target):
    by_name = {r.name: r for r in target.roles}
    overwrites = {}
    for entry in entries:
        role = by_name.get(entry["role"])
        if role is None:
            warn(f"could not resolve overwrite target '{entry['role']}' - skipped")
            continue
        po = discord.PermissionOverwrite()
        for flag in entry["allow"]:
            setattr(po, flag, True)
        for flag in entry["deny"]:
            setattr(po, flag, False)
        overwrites[role] = po
    return overwrites


async def apply_plan(plan, target):
    created = {"roles": [], "categories": [], "channels": []}
    errors = 0
    for item in plan:
        try:
            if item["action"] == "role":
                role = await target.create_role(
                    name=item["name"],
                    colour=discord.Colour(int(item["color"].lstrip("#"), 16)) if item["color"] else discord.Colour.default(),
                    hoist=item["hoist"], mentionable=item["mentionable"],
                    permissions=discord.Permissions(item["permissions"]), reason=REASON)
                created["roles"].append({"name": role.name, "id": role.id})
                ok(f"created role {role.name} (id {role.id})")
            elif item["action"] == "category":
                cat = await target.create_category(
                    name=item["name"], overwrites=resolve_overwrites(item["overwrites"], target), reason=REASON)
                created["categories"].append({"name": cat.name, "id": cat.id})
                ok(f"created category {cat.name} (id {cat.id})")
            else:
                category = next((c for c in target.categories if c.name == item["category"]), None) \
                    if item["category"] else None
                overwrites = resolve_overwrites(item["overwrites"], target)
                if item["kind"] == "text":
                    channel = await target.create_text_channel(
                        name=item["name"], category=category, topic=item["topic"] or None,
                        slowmode_delay=item["slowmode"], nsfw=item["nsfw"], news=item["news"],
                        overwrites=overwrites, reason=REASON)
                else:
                    channel = await target.create_voice_channel(
                        name=item["name"], category=category, user_limit=item["user_limit"],
                        bitrate=item["bitrate"], overwrites=overwrites, reason=REASON)
                created["channels"].append({"kind": item["kind"], "name": channel.name,
                                            "id": channel.id,
                                            "category_id": category.id if category else None})
                ok(f"created {item['kind']} channel #{channel.name} (id {channel.id})")
            await asyncio.sleep(0.5)  # be nice to rate limits
        except Exception as e:  # keep going; one bad channel must not stop the rest
            errors += 1
            fail(f"failed to create {item['action']} '{item.get('name', '?')}': {type(e).__name__}: {e}")
    return created, errors


def write_export(created, notes, template, target, directory="."):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(directory, f"template-export-{stamp}.json")
    data = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "template_guild": {"id": template.id, "name": template.name},
            "target_guild": {"id": target.id, "name": target.name},
            "created": created, "skipped": notes}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return path


# --- cli ----------------------------------------------------------------------

async def fetch_guild_or_fail(bot, guild_id, label):
    try:
        await bot.fetch_guild(guild_id)
    except discord.NotFound:
        fail(f"{label} guild {guild_id} not found - is the bot a member of it?")
        return None
    except discord.Forbidden:
        fail(f"no access to {label} guild {guild_id}")
        return None
    except discord.HTTPException as e:
        fail(f"Discord API error while fetching {label} guild: {e}")
        return None
    full = bot.get_guild(guild_id)  # cached object carries channels and roles
    if full is None:
        fail(f"{label} guild {guild_id} data unavailable")
        return None
    return full


async def run_flow(bot, args):
    template = await fetch_guild_or_fail(bot, args.template_id, "template")
    target = await fetch_guild_or_fail(bot, args.target_id, "target")
    if template is None or target is None:
        return 1
    if template.id == target.id:
        fail("template and target are the same guild")
        return 1
    me = target.me
    if not me.guild_permissions.manage_channels or not me.guild_permissions.manage_roles:
        fail("bot needs Manage Channels and Manage Roles in the target guild")
        return 1

    plan, notes = plan_guild(template, target)
    print_plan(plan, notes, template, target, args.apply)
    if not plan:
        ok("nothing to create - target already covers the template")
        return 0
    if not args.apply:
        warn("dry run - nothing was created. Re-run with --apply to create this structure.")
        return 0
    print()
    if not confirmed(len(plan), target.name):
        warn("cancelled - nothing was changed")
        return 0

    print()
    created, errors = await apply_plan(plan, target)
    path = write_export(created, notes, template, target)
    ok(f"wrote {path} (what was created - nothing was deleted)")
    if errors:
        warn(f"{errors} item(s) failed - see [x] lines above")
    return 1 if errors else 0


class Mover(discord.Client):
    def __init__(self, args):
        intents = discord.Intents.default()
        intents.guilds = True
        super().__init__(intents=intents)
        self.args = args
        self.exit_code = 1

    async def on_ready(self):
        ok(f"logged in as {self.user}")
        try:
            self.exit_code = await run_flow(self, self.args)
        except Exception as e:
            fail(f"unexpected error: {type(e).__name__}: {e}")
            self.exit_code = 1
        finally:
            await self.close()


def main():
    parser = argparse.ArgumentParser(
        description="Copy a Discord server structure to another guild (dry-run by default).")
    parser.add_argument("--template-id", type=int, required=True, help="guild id to copy from")
    parser.add_argument("--target-id", type=int, required=True, help="guild id to copy into")
    parser.add_argument("--apply", action="store_true",
                        help="ask for confirmation, then create in the target guild")
    parser.add_argument("--token", default=os.environ.get("DISCORD_TOKEN"),
                        help="bot token (defaults to the DISCORD_TOKEN env var)")
    args = parser.parse_args()

    if not args.token:
        fail("no bot token - pass --token or set the DISCORD_TOKEN env var")
        sys.exit(1)

    bot = Mover(args)
    try:
        bot.run(args.token, log_handler=None)
    except discord.LoginFailure:
        fail("invalid bot token")
        sys.exit(1)
    except KeyboardInterrupt:
        warn("interrupted")
        sys.exit(130)
    sys.exit(bot.exit_code)


if __name__ == "__main__":
    main()
