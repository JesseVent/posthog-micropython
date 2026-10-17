# sim_test.py — run with: micropython sim_test.py   (from the project dir)
# 1) full behavior against the local mock (batch + flag evaluation)
# 2) real capture/decide round trip against https://us.i.posthog.com

import json
import sys

from posthog_micropython import Client

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name, extra)
    if not cond:
        fails.append(name)


def report(name, ph):
    print("   [{} on-error: {}]".format(name, getattr(ph, "_last_err", None)))


# ---- 1. mock: capture -> batch ----
ph = Client(api_key="phc_mock", host="http://127.0.0.1:8091", flush_at=100)
ph.capture("device_boot", properties={"model": "ESP32", "fw": "1.0"})
ph.capture("sensor_reading", properties={"c": 42})
n = ph.flush()
check("mock batch: 2 events sent", n == 2)
check("mock batch: queue cleared", len(ph.queue) == 0)

# ---- 2. mock: identify ----
ph2 = Client(api_key="phc_mock", host="http://127.0.0.1:8091", flush_at=100)
ph2.capture("anon")
anon = ph2._distinct_id
ph2.identify("jesse-esp32", {"plan": "beta"})
ph2.flush()
check("identify: stable distinct id", ph2._distinct_id == "jesse-esp32")

# ---- 3. mock: feature flag evaluation via /decide/?v=3 ----
val = ph.get_flag("sensor-mode")
check("flag sensor-mode == variant-a", val == "variant-a")
val2 = ph.get_flag("off-flag")
check("flag off-flag == False (not swallowed)", val2 is False)
val3 = ph.get_flag("missing-flag")
check("missing flag -> None", val3 is None)

# ---- 4. mock: on_error hook fires on server error ----
_orig_post = sys.modules["urequests"].post


def failing_post(url, json=None, headers=None, timeout=None):
    raise OSError("connection refused")


sys.modules["urequests"].post = failing_post
errs = []
ph3 = Client(api_key="k", host="http://127.0.0.1:8091", flush_at=100, on_error=lambda e, b: errs.append(e))
ph3.capture("will-fail")
sent = ph3.flush()
check("failed flush returns 0", sent == 0)
check("failed flush keeps queue for retry", len(ph3.queue) == 1)
check("on_error hook fired", len(errs) == 1)
sys.modules["urequests"].post = _orig_post

# ---- 6. v0.2.0: set/set_once, alias, groups, exception, persist ----
pc = Client(api_key="k", host="http://127.0.0.1:8091", flush_at=100)
pc.set({"fw": "1.0"})
pc.set_once({"model": "ESP32-S3"})
pc.alias("board-x")
ev = pc.capture("g", groups={"room": "r1"})
check("groups: capture() return carries $group", ev["properties"]["$group"] == {"room": "r1"})
pc.group("fleet", "f1", {"loc": "lounge"})
ev = pc.queue[-1][0]
check("groupidentify queued", ev["event"] == "$groupidentify")
check("groupidentify carries $group_type", ev["properties"]["$group_type"] == "fleet")
ev = pc.capture("after")
check("client group attaches to captures", ev["properties"]["$group"] == {"fleet": "f1"})
sent = pc.flush()
check("person/group events sent", sent == 6, "sent=%s" % sent)
try:
    1 / 0
except ZeroDivisionError as e:
    pc.capture_exception(e)
check("exception event queued", pc.queue[-1][0]["event"] == "$exception")
pc.flush()

import os

try:
    os.mkdir("_persisttmp")
except OSError:
    pass  # exists from a previous run
os.chdir("_persisttmp")
try:
    pa = Client(api_key="k", host="http://127.0.0.1:8091", flush_at=100, persist=True)
    did = pa._distinct_id
    check("persisted id minted", bool(did))
    pb = Client(api_key="k", host="http://127.0.0.1:8091", flush_at=100, persist=True)
    check("same id across clients (file store on unix port)", pb._distinct_id == did)
finally:
    os.chdir("..")

pf = Client(api_key="k", host="http://127.0.0.1:8091", flush_at=100)
check("get_flag on micropython", pf.get_flag("sensor-mode") == "variant-a")
check("get_payload on micropython", pf.get_payload("sensor-mode") == {"level": 3})

print()
print("=" * 40)
if fails:
    print("{} FAILED: {}".format(len(fails), fails))
    sys.exit(1)
print("all simulator checks passed")