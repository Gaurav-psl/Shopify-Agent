"""
database.py
-----------
SQLAlchemy engine/session setup. Defaults to a local SQLite file so this
runs with zero external setup — swap DATABASE_URL for Postgres/MySQL in
production (e.g. "postgresql+psycopg2://user:pass@host/dbname").
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./shopify_app.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
