"""Keirai session management CLI - works without (and alongside) the bot.

  python cli.py list                          # all stored sessions
  python cli.py delete <chat>[:<thread|main>] # clear one session's context
  python cli.py rename <chat>[:<thread|main>] <name>

<chat> is the Telegram chat id, <thread> the topic id (omit or 'main' for
the main chat). Renames the stored session/topic name.
"""
import argparse
import datetime
import sys

import config as config_mod
from sessions import Store


def parse_key(key):
    """'123' / '123:456' / '123:main' -> (chat_id, thread_id_or_None)."""
    if ":" in key:
        chat, thread = key.split(":", 1)
        thread = None if thread.lower() in ("main", "0", "") else int(thread)
        return int(chat), thread
    return int(key), None


def _names(store):
    return {(chat_id, thread_id): name
            for chat_id, thread_id, name in store.load_topics()}


def cmd_list(store, args):
    rows = sorted(store.list_sessions(), key=lambda r: -r[4])
    if not rows:
        print("no sessions stored")
        return 0
    names = _names(store)
    for chat_id, thread_id, model, messages, updated in rows:
        key = (chat_id, thread_id)
        when = datetime.datetime.fromtimestamp(updated).strftime("%Y-%m-%d %H:%M")
        print("chat=%s thread=%s name=%-20s msgs=%-3d model=%-24s last=%s"
              % (chat_id, "main" if thread_id == 0 else thread_id,
                 names.get(key) or "-", len(messages), model or "-", when))
    return 0


def cmd_delete(store, args):
    chat_id, thread_id = parse_key(args.key)
    rows = [r for r in store.list_sessions()
            if r[0] == chat_id and r[1] == (thread_id or 0)]
    if not rows:
        print("no such session: %s" % args.key)
        return 1
    store.delete_session(chat_id, thread_id or 0)
    print("deleted context of chat=%s thread=%s"
          % (chat_id, thread_id or "main"))
    print("note: the Telegram topic (if any) is not touched - delete it in Telegram")
    return 0


def cmd_rename(store, args):
    chat_id, thread_id = parse_key(args.key)
    if thread_id is None:
        print("main chat has no topic to rename; use a topic key like 123:456")
        return 1
    store.set_topic_name(chat_id, thread_id, args.name)
    print("renamed session chat=%s thread=%s -> %r" % (chat_id, thread_id, args.name))
    return 0


def run(argv=None):
    parser = argparse.ArgumentParser(prog="keirai-cli", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list stored sessions")
    p_del = sub.add_parser("delete", help="delete one session's context")
    p_del.add_argument("key", help="chat_id[:thread_id|main]")
    p_ren = sub.add_parser("rename", help="rename a session/topic")
    p_ren.add_argument("key", help="chat_id:thread_id")
    p_ren.add_argument("name")
    args = parser.parse_args(argv)

    cfg = config_mod.load()
    store = Store(config_mod.sessions_path(cfg))
    try:
        handler = {"list": cmd_list, "delete": cmd_delete, "rename": cmd_rename}[args.cmd]
        return handler(store, args)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(run())
