# Offline self-check: stubs urequests and asserts the wire payload matches
# posthog-python's /batch/ format and the queue/flush behaviour holds up.
# Run with: python3 -I test_posthog_micropython.py

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["_STUB"] = "1"

SENT = []


class _Res:
    status_code = 200

    def close(self):
        pass


def post(url, json=None, headers=None, timeout=None):
    SENT.append({"url": url, "body": json})
    return _Res()


import types

sys.modules["urequests"] = types.SimpleNamespace(post=post)

import posthog_micropython as ph

# ---- wire payload ----
c = ph.Client(api_key="phc_test", flush_at=100)  # big flush_at → manual flush
c.capture("test_event", distinct_id="user1", properties={"a": 1})
c.capture("second", distinct_id="user1")
sent = c.flush()
assert sent == 2, sent
r = SENT[-1]
assert r["url"] == "https://us.i.posthog.com/batch/", r["url"]
body = r["body"]
assert body["api_key"] == "phc_test" and "sent_at" in body
assert len(body["batch"]) == 2
ev = body["batch"][0]
assert ev["event"] == "test_event" and ev["distinct_id"] == "user1"
assert ev["properties"]["a"] == 1 and ev["properties"]["$lib"] == ph.SDK_NAME
assert c.queue == [] and c._queue_bytes == 0

# ---- auto distinct id + identify ----
c2 = ph.Client(api_key="phc_test", flush_at=100)
c2.capture("anon")
anon = c2._distinct_id
assert anon  # generated, stable
c2.identify("user42", {"plan": "pro"})
id_ev = [e for e, _ in c2.queue if e["event"] == "$identify"][0]
assert id_ev["distinct_id"] == "user42"
assert id_ev["properties"]["$anon_distinct_id"] == anon
c2.capture("later")  # now tagged with stable id, not anon
assert c2.queue[-1][0]["distinct_id"] == "user42"

# ---- auto flush at flush_at ----
c3 = ph.Client(api_key="k", flush_at=5)
for _ in range(5):
    c3.capture("tick")
assert len(SENT[-1]["body"]["batch"]) == 5  # flushed automatically
assert c3.queue == []

# ---- byte cap drops oldest, keeps fresh ----
c4 = ph.Client(api_key="k", flush_at=100, max_queue_bytes=400)
for i in range(30):
    c4.capture("big", properties={"msg": "x" * 30})
assert len(c4.queue) < 30
assert [e for e, _ in c4.queue][-1]["properties"]["msg"] == "x" * 30  # freshest survives

# ---- failed flush keeps the queue ----
failcount = [0]


def post_fail(url, **kw):
    SENT.append(url)
    failcount[0] += 1
    e = _Res()
    e.status_code = 500
    return e


sys.modules["urequests"].post = post_fail
c5 = ph.Client(api_key="k", flush_at=100, on_error=lambda e, b: None)
c5.capture("retry_me")
assert c5.flush() == 0
assert len(c5.queue) == 1  # kept for next flush
sys.modules["urequests"].post = post

print("all checks passed")
# ============================================================
# 0.2.0 features: set/set_once/alias/group/groupidentify/exception/persist/flags
SENT.clear()

# ---- $set / $set_once person properties ----
pc = ph.Client(api_key="k", flush_at=100)
pc.set({"firmware": "1.2.0"})
pc.set_once({"model": "ESP32-S3"})
pc.flush()
batch = SENT[-1]["body"]["batch"]
assert batch[-2]["event"] == "$set" and batch[-2]["properties"]["$set"] == {"firmware": "1.2.0"}
assert batch[-1]["event"] == "$set_once" and batch[-1]["properties"]["$set_once"] == {"model": "ESP32-S3"}

# ---- alias ----
pc.alias("board-07")
pc.flush()
ev = SENT[-1]["body"]["batch"][-1]
assert ev["event"] == "$create_alias" and ev["properties"]["alias"] == "board-07"
assert ev["distinct_id"] == pc._distinct_id

# ---- groups: attach to captures, $groupidentify on props ----
pc.group("project", "p-42")
pc.group("fleet", "fleet-1", {"location": "lounge"})
pc.flush()
ev = SENT[-1]["body"]["batch"][-1]
assert ev["event"] == "$groupidentify"
assert ev["properties"]["$group_type"] == "fleet" and ev["properties"]["location"] == "lounge"
pc.capture("with_group", groups={"room": "r1"})
pc.flush()
ev = SENT[-1]["body"]["batch"][-1]
assert ev["properties"]["$group"] == {"project": "p-42", "fleet": "fleet-1", "room": "r1"}
pc.capture("plain")
pc.flush()
ev = SENT[-1]["body"]["batch"][-1]
assert ev["properties"]["$group"] == {"project": "p-42", "fleet": "fleet-1"}  # client-level group only

# ---- exception capture ----
try:
    1 / 0
except ZeroDivisionError as e:
    pc.capture_exception(e, properties={"device": "D1001"})
pc.flush()
ev = SENT[-1]["body"]["batch"][-1]
assert ev["event"] == "$exception"
assert ev["properties"]["$exception_type"] == "ZeroDivisionError"
assert ev["properties"]["$exception_list"][0]["type"] == "ZeroDivisionError"
assert ev["properties"]["device"] == "D1001"

# ---- persistent device identity (file store on this runner) ----
import tempfile, os as _os

os_cwd = _os.getcwd()
_os.chdir(tempfile.mkdtemp())
try:
    pa = ph.Client(api_key="k", flush_at=100, persist=True)
    did1 = pa._distinct_id
    assert did1, "persisted id minted"
    pa.capture("boot1")
    pb = ph.Client(api_key="k", flush_at=100, persist=True)
    assert pb._distinct_id == did1, "same device id across clients"
finally:
    _os.chdir(os_cwd)

# ---- flags: get_flag + get_payload reuse one decide response shape ----


def post_decide(url, json=None, headers=None, timeout=None):
    SENT.append(url)
    class R:
        def json(self):
            return {"featureFlags": {"mode": "turbo"}, "payloads": {"mode": {"speed": 2}}}

        def close(self):
            pass
    return R()


sys.modules["urequests"].post = post_decide
pf = ph.Client(api_key="k", flush_at=100)
pf.capture("seed")  # sets stable distinct id... (no, capture does not flush — fine)
got = pf.get_flag("mode")
assert got == "turbo", got
payload = pf.get_payload("mode")
assert payload == {"speed": 2}, payload
assert pf.get_payload("nope") is None
sys.modules["urequests"].post = post

print("v0.2.0 feature checks passed")