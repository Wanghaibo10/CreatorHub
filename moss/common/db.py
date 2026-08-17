"""数据库初始化与会话。含 SQLite 轻量自动迁移(为已有表补缺失列)。

SQLite 并发说明:Web 请求(main.py ~150 处 get_session)与后台轮询(monitor.py ~90 处)
共用同一个库文件。默认 rollback-journal 模式下任意写事务锁全库,高并发时会直接抛
`database is locked`。这里在每个连接建立时统一设 PRAGMA:
  - WAL: 读写不互斥,写写才排队,是本项目并发形态(多读+间歇写)的正确模式
  - busy_timeout=30s: 写写冲突时等锁而不是立刻抛错(配合 connect_args timeout)
  - synchronous=NORMAL: WAL 下的推荐档,断电最多丢最后一个未 checkpoint 批次,不会损坏库
"""
from __future__ import annotations

from sqlalchemy import event, inspect, text
from sqlmodel import SQLModel, Session, create_engine

_engine = None


def _set_sqlite_pragma(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def _auto_migrate(engine):
    """为已存在的表补上模型里新增的列(SQLite 友好,仅 ADD COLUMN)。"""
    insp = inspect(engine)
    for table in SQLModel.metadata.tables.values():
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            coltype = col.type.compile(engine.dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'
            # 模型若给了标量默认值,作为 SQL DEFAULT 写入 —— SQLite 会用它回填已有行,
            # 避免新列在旧数据上为 NULL(例如 platform 列需回填为 'douyin')。
            scalar = (getattr(col.default, "arg", None)
                      if col.default is not None and getattr(col.default, "is_scalar", False)
                      else None)
            if isinstance(scalar, bool):
                ddl += f" DEFAULT {1 if scalar else 0}"
            elif isinstance(scalar, str):
                ddl += " DEFAULT '" + scalar.replace("'", "''") + "'"
            elif isinstance(scalar, (int, float)):
                ddl += f" DEFAULT {scalar}"
            elif not col.nullable:
                ddl += " DEFAULT ''"
            with engine.begin() as conn:
                conn.execute(text(ddl))


def init_db(db_path: str):
    global _engine
    _engine = create_engine(
        f"sqlite:///{db_path}",
        # timeout 是 sqlite3 驱动层的等锁秒数,和 PRAGMA busy_timeout 语义相同,
        # 两处都设是因为 PRAGMA 只对已建立的连接生效,驱动层参数兜住连接建立本身。
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(_engine, "connect", _set_sqlite_pragma)
    SQLModel.metadata.create_all(_engine)
    _auto_migrate(_engine)
    return _engine


def get_session() -> Session:
    assert _engine is not None, "init_db() 未调用"
    return Session(_engine)
