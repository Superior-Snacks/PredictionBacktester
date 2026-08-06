"""
check_discord.py -- preflight for the Discord remote-control setup. Run it BEFORE the bot, so a bad token or
a missing intent shows up as a clear failure instead of "the bot ignores my commands".

    python check_discord.py            # read-only checks
    python check_discord.py --post     # also send one test message through the webhook

Checks, in dependency order (each one is meaningless if the previous failed):
  1. env       DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID present (alerts also need DISCORD_WEBHOOK_URL)
  2. token     GET /users/@me            -> the token is valid and names the bot
  3. channel   GET /channels/{id}        -> the bot can SEE the channel (View Channel)
  4. history   GET .../messages?limit=5  -> the bot can READ it (Read Message History)
  5. intent    are recent human messages' contents READABLE? Empty text on every message is the classic
               "Message Content Intent is off" signature -- the bot connects fine and silently does nothing.
  6. webhook   optional POST, so you can confirm replies actually land in the channel you're watching

SECRETS: values are never printed -- only presence, HTTP status, and lengths. Message TEXT is never shown
either (only its length), so running this can't spill channel contents into a log.
"""
from __future__ import annotations

import os
import sys

import httpx

from env_util import load_dotenv_upwards

load_dotenv_upwards()

API = "https://discord.com/api/v10"
OK, BAD = "[ OK ]", "[FAIL]"


def main() -> int:
    post = "--post" in sys.argv
    token = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    chan = (os.environ.get("DISCORD_CHANNEL_ID") or "").strip()
    hook = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()

    print("1. ENV")
    print(f"   {OK if token else BAD} DISCORD_BOT_TOKEN   {'present' if token else 'MISSING - commands cannot work'}")
    print(f"   {OK if chan else BAD} DISCORD_CHANNEL_ID  {'present' if chan else 'MISSING - commands cannot work'}")
    print(f"   {OK if hook else BAD} DISCORD_WEBHOOK_URL {'present' if hook else 'MISSING - the bot cannot reply/alert'}")
    if not (token and chan):
        print("\nBoth the token and the channel id are required; the listener no-ops without them.")
        return 1

    h = {"Authorization": f"Bot {token}"}
    with httpx.Client(timeout=15.0) as c:
        print("\n2. TOKEN")
        r = c.get(f"{API}/users/@me", headers=h)
        if r.status_code != 200:
            print(f"   {BAD} GET /users/@me -> HTTP {r.status_code}. The token is wrong, revoked, or is an "
                  "OAuth/client secret rather than the BOT token (Portal -> Bot -> Reset Token).")
            return 1
        me = r.json()
        print(f"   {OK} authenticated as: {me.get('username')}#{me.get('discriminator', '0')} (id {me.get('id')})")

        print("\n3. CHANNEL")
        r = c.get(f"{API}/channels/{chan}", headers=h)
        if r.status_code != 200:
            print(f"   {BAD} GET /channels/{chan} -> HTTP {r.status_code}. Either the id is wrong, or the bot "
                  "is not in that server / lacks View Channel. (Right-click the channel -> Copy Channel ID; "
                  "needs Developer Mode on.)")
            return 1
        ch = r.json()
        print(f"   {OK} channel '#{ch.get('name')}' (guild {ch.get('guild_id')})")

        print("\n4. HISTORY")
        r = c.get(f"{API}/channels/{chan}/messages?limit=5", headers=h)
        if r.status_code != 200:
            print(f"   {BAD} reading messages -> HTTP {r.status_code}. The bot needs 'Read Message History' "
                  "on this channel.")
            return 1
        msgs = r.json()
        print(f"   {OK} read {len(msgs)} recent message(s)")

        print("\n5. MESSAGE CONTENT INTENT")
        humans = [m for m in msgs if not (m.get("author") or {}).get("bot")]
        if not humans:
            print("   [ ?? ] no HUMAN messages in the last 5 - post something in the channel and re-run to "
                  "confirm the intent. (Bot/webhook posts don't prove it: your own bot can always read those.)")
        else:
            readable = sum(1 for m in humans if (m.get("content") or "").strip())
            if readable:
                print(f"   {OK} {readable}/{len(humans)} human message(s) have readable text - intent is ON")
            else:
                print(f"   {BAD} all {len(humans)} human message(s) came back with EMPTY text -> the "
                      "MESSAGE CONTENT INTENT is OFF.")
                print("          Portal -> your app -> Bot -> Privileged Gateway Intents -> Message Content "
                      "Intent -> Save, then restart the bot.")
                print("          Commands will be silently ignored until this is on.")

        if post:
            print("\n6. WEBHOOK POST")
            if not hook:
                print(f"   {BAD} no DISCORD_WEBHOOK_URL to post with")
            else:
                r = c.post(hook, json={"content": "**[HardVen]** preflight: remote control wired up. "
                                                  "Send `commands` for the menu."})
                print(f"   {OK if r.status_code < 300 else BAD} webhook POST -> HTTP {r.status_code}"
                      + ("" if r.status_code < 300 else " (check the URL is a valid webhook)"))

    print("\nNext: start the bot and send `commands` in the channel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
