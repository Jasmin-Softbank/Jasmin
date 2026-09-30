# Quick memo board. Built in an afternoon: SQLite file, Flask dev server, no Dockerfile.
import os
import sqlite3
from pathlib import Path

from flask import Flask, redirect, request

app = Flask(__name__)
DB = os.environ.get("MEMO_DB", "memo.db")


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def migrate():
    with conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
        done = {r["name"] for r in c.execute("SELECT name FROM schema_migrations")}
        for f in sorted(Path(__file__).with_name("migrations").glob("*.sql")):
            if f.name not in done:
                c.executescript(f.read_text())
                c.execute("INSERT INTO schema_migrations VALUES (?)", (f.name,))


@app.get("/")
def index():
    with conn() as c:
        rows = c.execute("SELECT body, created_at FROM memos ORDER BY id DESC LIMIT 50").fetchall()
    items = "".join(f"<li>{r['body']} <small>{r['created_at']}</small></li>" for r in rows)
    return f"<h1>Memo</h1><form method=post action=/memos><input name=body><button>add</button></form><ul>{items}</ul>"


@app.post("/memos")
def add():
    with conn() as c:
        c.execute("INSERT INTO memos (body) VALUES (?)", (request.form["body"][:500],))
    return redirect("/")


@app.get("/health")
def health():
    return "ok"


if __name__ == "__main__":
    migrate()
    app.run(port=5000)
