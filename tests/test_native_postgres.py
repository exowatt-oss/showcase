"""Native PostgreSQL integration checks.

Run explicitly with SHOWCASE_TEST_DATABASE_URL pointing at a dedicated
PostgreSQL database/schema/user. The default fast suite intentionally skips it.
"""

import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db import Base, Presentation, _build_engine


@pytest.mark.skipif(
    not os.environ.get("SHOWCASE_TEST_DATABASE_URL"),
    reason="set SHOWCASE_TEST_DATABASE_URL for native PostgreSQL integration",
)
def test_native_postgres_engine_and_transaction_round_trip():
    url = os.environ["SHOWCASE_TEST_DATABASE_URL"]
    engine = _build_engine(url)
    slug = f"native-pg-{uuid.uuid4().hex[:12]}"
    try:
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        with Session.begin() as session:
            session.add(
                Presentation(
                    slug=slug,
                    title="native integration",
                    html="<p>native</p>",
                    size_bytes=13,
                    owner_hash=None,
                )
            )
        with Session() as session:
            row = session.execute(
                select(Presentation).where(Presentation.slug == slug)
            ).scalar_one()
            assert row.html == "<p>native</p>"
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql("DELETE FROM presentations WHERE slug = %s", (slug,))
        Base.metadata.drop_all(engine)
        engine.dispose()
