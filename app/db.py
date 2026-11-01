from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase


def now():
    return datetime.now(timezone.utc)


def stamp(**kw):
    return Column(DateTime(timezone=True), **kw)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'
    id = Column(BigInteger, primary_key=True, autoincrement=False)
    enabled = Column(Boolean, nullable=False, default=True)
    interval = Column(Integer, default=180, nullable=False)
    region = Column(String(100), default='')
    currency = Column(String(3), default='')
    created = stamp(default=now)


class Search(Base):
    __tablename__ = 'searches'
    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey('users.id', ondelete='CASCADE'), index=True, nullable=False)
    name = Column(String(80), nullable=False)
    url = Column(Text, nullable=False)
    key = Column(String(64), nullable=False, index=True)
    filters = Column(JSON, nullable=False, default=dict)
    interval = Column(Integer, nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    initialized = stamp()
    success = stamp()
    manual = stamp()
    due = stamp(default=now, nullable=False, index=True)
    failures = Column(Integer, default=0, nullable=False)


class Listing(Base):
    __tablename__ = 'listings'
    id = Column(Text, primary_key=True)
    data = Column(JSON, nullable=False)
    first_seen = stamp(default=now)
    last_seen = stamp(default=now)


class Match(Base):
    __tablename__ = 'search_listings'
    search_id = Column(ForeignKey('searches.id', ondelete='CASCADE'), primary_key=True)
    listing_id = Column(ForeignKey('listings.id'), primary_key=True)
    new = Column(Boolean, nullable=False)
    created = stamp(default=now, index=True)


class Notice(Base):
    __tablename__ = 'notification_outbox'
    user_id = Column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    listing_id = Column(ForeignKey('listings.id'), primary_key=True)
    status = Column(String(16), default='pending', nullable=False, index=True)
    attempts = Column(Integer, default=0)
    due = stamp(default=now, index=True)
    sent = stamp()
    message_id = Column(BigInteger)


class Run(Base):
    __tablename__ = 'parser_runs'
    id = Column(Integer, primary_key=True)
    key = Column(String(64), index=True)
    created = stamp(default=now, index=True)
    requests = Column(Integer, nullable=False, default=0)
    elapsed_ms = Column(Integer)
    found = Column(Integer)
    error = Column(String(200))


class Control(Base):
    __tablename__ = 'control'
    id = Column(Integer, primary_key=True)
    paused = Column(Boolean, default=False)
    cooldown = stamp()
    telegram_due = stamp()
