from shared.verify_timeouts import TEMU_PREWARM_TIMEOUT_S, verify_timeout_for


def test_temu_timeout():
    assert verify_timeout_for("temu") == 120.0


def test_default_timeout():
    assert verify_timeout_for("takealot") == 20.0


def test_prewarm_budget():
    assert TEMU_PREWARM_TIMEOUT_S >= 120.0
