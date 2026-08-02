"""Alembic environment using the transaction supplied by HubDatabase."""

from alembic import context

from hub.adapters.persistence.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is None:
        raise RuntimeError("Hub migrations require an explicit database connection")
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()

if context.is_offline_mode():
    raise RuntimeError("Hub migrations do not support offline mode")
run_migrations_online()
