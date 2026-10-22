"""The database-backed contract: a kill switch halts a marketplace's queue within 5 seconds, per marketplace.
Needs TEST_ADMIN_DSN. Names are CTK*."""
import pytest

from contracts.suite_db import check_kill_switch_halts_queue


@pytest.mark.parametrize("marketplace", ["ETSY", "GUMROAD", "SHOPIFY"])
def test_CTK1_kill_switch_halts_the_queue_within_5_seconds(db, marketplace):
    check_kill_switch_halts_queue(db, marketplace)
