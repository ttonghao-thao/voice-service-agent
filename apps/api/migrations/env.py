import asyncio

from alembic import context
from app.config import Settings
from app.storage.models import Base
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config


def configure(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run():
    engine = create_async_engine(Settings().database_url)
    async with engine.connect() as conn:
        await conn.run_sync(configure)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=Settings().database_url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run())
