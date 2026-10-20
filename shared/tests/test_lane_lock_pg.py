"""LaneLock against a real Postgres. Needs TEST_ADMIN_DSN like the other DB tests. Names are LL*."""
import psycopg
import pytest

from shared.lane_lock import LaneBusy, LaneLock


def conn(db):
    return psycopg.connect(db.dsn, autocommit=True)


def test_LL1_second_session_is_refused_until_the_first_releases(db):
    a, b = conn(db), conn(db)
    try:
        first = LaneLock(a, "ETSY", "shop-1").acquire()
        with pytest.raises(LaneBusy):
            LaneLock(b, "ETSY", "shop-1").acquire()
        first.release()
        LaneLock(b, "ETSY", "shop-1").acquire().release()
    finally:
        a.close()
        b.close()


def test_LL2_different_lanes_do_not_conflict(db):
    a, b = conn(db), conn(db)
    try:
        la = LaneLock(a, "ETSY", "shop-1").acquire()
        lb = LaneLock(b, "ETSY", "shop-2").acquire()          # other account
        lc = LaneLock(b, "SHOPIFY", "shop-1").acquire()       # other marketplace
        for lk in (la, lb, lc):
            lk.release()
    finally:
        a.close()
        b.close()


def test_LL3_a_crashed_holder_releases_when_its_session_dies(db):
    a, b = conn(db), conn(db)
    try:
        LaneLock(a, "ETSY", "shop-1").acquire()
        a.close()                                              # the worker "crashes" without releasing
        LaneLock(b, "ETSY", "shop-1").acquire().release()
    finally:
        b.close()


def test_LL4_context_manager_releases_on_exit_even_after_an_error(db):
    a, b = conn(db), conn(db)
    try:
        with pytest.raises(RuntimeError):
            with LaneLock(a, "ETSY", "shop-1"):
                raise RuntimeError("worker failed")
        LaneLock(b, "ETSY", "shop-1").acquire().release()
    finally:
        a.close()
        b.close()


def test_LL5_one_object_cannot_acquire_twice_and_release_is_idempotent(db):
    a = conn(db)
    try:
        lk = LaneLock(a, "ETSY", "shop-1").acquire()
        with pytest.raises(RuntimeError):
            lk.acquire()
        lk.release()
        lk.release()
    finally:
        a.close()
