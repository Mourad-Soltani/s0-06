import os
from alembic import context
from sqlalchemy import create_engine

config = context.config
url = config.get_main_option("sqlalchemy.url") or os.environ["DATABASE_URL"]  # postgresql+psycopg://...


def run_migrations_online():
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():          # Alembic owns the transaction
            context.run_migrations()


run_migrations_online()
