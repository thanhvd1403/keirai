"""SQLite session store - sessions, topics and model choice survive restarts.

Stdlib sqlite3. Write-through: main updates state then persists the row.
thread_id 0 is the sentinel for "main chat, no topic".
"""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  chat_id INTEGER NOT NULL,
  thread_id INTEGER NOT NULL,
  model TEXT,
  messages TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (chat_id, thread_id)
);
CREATE TABLE IF NOT EXISTS topics (
  chat_id INTEGER NOT NULL,
  thread_id INTEGER NOT NULL,
  name TEXT,
  PRIMARY KEY (chat_id, thread_id)
);
"""


class Store:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------ sessions

    def save_session(self, chat_id, thread_id, model, messages):
        if not messages:
            self.delete_session(chat_id, thread_id)
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO sessions (chat_id, thread_id, model, messages, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, thread_id, model, json.dumps(messages), time.time()))
        self.conn.commit()

    def delete_session(self, chat_id, thread_id):
        self.conn.execute("DELETE FROM sessions WHERE chat_id=? AND thread_id=?",
                          (chat_id, thread_id))
        self.conn.commit()

    def load_sessions(self):
        """[(chat_id, thread_id, model_or_None, messages, updated_at)]"""
        rows = self.conn.execute(
            "SELECT chat_id, thread_id, model, messages, updated_at FROM sessions").fetchall()
        out = []
        for chat_id, thread_id, model, messages, updated in rows:
            try:
                out.append((chat_id, thread_id, model, json.loads(messages), updated))
            except ValueError:
                continue  # corrupt row: skip
        return out

    def list_sessions(self):
        return self.load_sessions()

    def delete_chat(self, chat_id):
        """Remove all sessions + topic rows for a chat. Returns topic thread ids."""
        topics = [r[0] for r in self.conn.execute(
            "SELECT thread_id FROM topics WHERE chat_id=?", (chat_id,)).fetchall()]
        self.conn.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))
        self.conn.execute("DELETE FROM topics WHERE chat_id=?", (chat_id,))
        self.conn.commit()
        return topics

    # ------------------------------------------------ topics

    def add_topic(self, chat_id, thread_id, name=None):
        self.conn.execute("INSERT OR REPLACE INTO topics (chat_id, thread_id, name) "
                          "VALUES (?, ?, ?)", (chat_id, thread_id, name))
        self.conn.commit()

    def set_topic_name(self, chat_id, thread_id, name):
        self.conn.execute("UPDATE topics SET name=? WHERE chat_id=? AND thread_id=?",
                          (name, chat_id, thread_id))
        self.conn.commit()

    def load_topics(self):
        """[(chat_id, thread_id, name)]"""
        return self.conn.execute(
            "SELECT chat_id, thread_id, name FROM topics").fetchall()

    def close(self):
        self.conn.close()
