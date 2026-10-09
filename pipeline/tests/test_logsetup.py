import io, json, logging

from bait import logsetup


def test_json_formatter_emits_level_and_message():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logsetup.JsonFormatter())
    log = logging.getLogger("bait.test")
    log.handlers[:] = [h]
    log.propagate = False
    log.setLevel(logging.INFO)
    log.warning("chain=%s lag=%d", "base", 42)
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("failed")
    lines = [json.loads(x) for x in buf.getvalue().splitlines()]
    assert lines[0]["level"] == "warning" and lines[0]["msg"] == "chain=base lag=42" and lines[0]["logger"] == "bait.test"
    assert lines[1]["level"] == "error" and "ValueError: boom" in lines[1]["exc"]
