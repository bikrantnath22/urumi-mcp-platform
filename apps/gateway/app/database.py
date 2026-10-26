from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from app.config import database_url


engine = create_async_engine(
    database_url(),
    pool_size=5,
    max_overflow=0,
    pool_timeout=5,
    pool_pre_ping=True,
    echo=False,
    hide_parameters=True,
    connect_args={
        "connect_timeout": 5,
        "options": (
            "-c statement_timeout=10000 "
            "-c lock_timeout=2000 "
            "-c timezone=UTC"
        ),
    },
)

Session = async_sessionmaker(
    engine,
    expire_on_commit=False,
)


async def get_session():
    async with Session() as session:
        yield session
