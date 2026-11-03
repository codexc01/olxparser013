import json
import math
import sqlite3
import time
from datetime import datetime
from urllib.parse import urlsplit


def norm(value):
    return " ".join(value.casefold().split())


def parse(raw):
    created = datetime.fromisoformat(raw["created_time"].replace("Z", "+00:00"))
    url = urlsplit(raw["url"])
    if not created.tzinfo or url.scheme != "https" or url.hostname not in ("olx.uz", "www.olx.uz"):
        raise ValueError("Неожиданная дата или ссылка")
    params = next((p["value"] for p in raw["params"] if p["key"] == "price"), {})
    price = params.get("value")
    amount = float(price) if price is not None else None
    if amount is not None and (not math.isfinite(amount) or amount < 0):
        raise ValueError("Некорректная цена")
    return dict(id=int(raw["id"]), title=raw["title"][:300], city=raw["location"]["city"]["name"],
                created=created.timestamp(), url=raw["url"], amount=amount, currency=params.get("currency", ""),
                label=str(params.get("label") or (f"{amount:g} {params.get('currency', '')}" if amount is not None else "По договорённости")),
                words=norm(raw["title"] + " " + (raw.get("description") or "")))


def matches(t, a):
    return ((not t["city"] or norm(t["city"]) == norm(a["city"]))
            and all(norm(w) in a["words"] for w in t["words"].split(",") if w.strip())
            and (t["cap"] is None or (a["currency"] == t["currency"] and a["amount"] is not None
                                      and a["amount"] <= t["cap"])))


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS topics(id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL,
          city TEXT DEFAULT '', words TEXT DEFAULT '', cap REAL, currency TEXT DEFAULT '',
          active INTEGER DEFAULT 1, since REAL, due REAL DEFAULT 0, revision INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS seen(topic INTEGER REFERENCES topics ON DELETE CASCADE, offer INTEGER,
          amount REAL, currency TEXT, touched REAL, PRIMARY KEY(topic,offer));
        CREATE INDEX IF NOT EXISTS seen_age ON seen(touched);
        CREATE TABLE IF NOT EXISTS pending(id INTEGER PRIMARY KEY, topic INTEGER REFERENCES topics ON DELETE CASCADE,
          body TEXT);
        """)

    def topic(self, key):
        return self.db.execute("SELECT * FROM topics WHERE id=?", (key,)).fetchone()

    def remember(self, t, offers):
        with self.db:
            current = self.topic(t["id"])
            if not current or not current["active"] or current["revision"] != t["revision"]:
                return
            for a in offers:
                args = (t["id"], a["id"])
                old = self.db.execute("SELECT * FROM seen WHERE topic=? AND offer=?", args).fetchone()
                drop = (old and old["currency"] == a["currency"] and old["amount"] is not None
                        and a["amount"] is not None and old["amount"] > a["amount"])
                if matches(t, a) and (drop or (not old and a["created"] >= t["since"])):
                    event = {k: v for k, v in a.items() if k != "words"}
                    event.update(query=t["query"], previous=old["amount"] if drop else None)
                    self.db.execute("INSERT INTO pending(topic,body) VALUES(?,?)", (t["id"], json.dumps(event)))
                self.db.execute("INSERT OR REPLACE INTO seen VALUES(?,?,?,?,?)",
                                (*args, a["amount"], a["currency"], time.time()))
