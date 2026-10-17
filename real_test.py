# real_test.py — real round trip against https://us.i.posthog.com
# run: micropython real_test.py   (from the project dir)
# Live flags used (project 620614): micropython-sim-test (bool),
# esp32-sensor-mode (control/turbo/eco).
import sys

from posthog_micropython import Client

KEY = "phc_uJBozWsNKhLDZAcTcqF5nDtj6hj27SaD8vSSTA4XKuUY"  # project write key (public)
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name, extra)
    if not cond:
        fails.append(name)


ph = Client(api_key=KEY, host="https://us.i.posthog.com", flush_at=100)
ph.on_error = lambda e, b: print("   error:", e)
ph.capture("micropython_sim_boot", distinct_id="micropython-simulator", properties={"sim": "unix port"})
n = ph.flush()
check("real capture: flush returned", n == 1, "sent=%s" % n)
check("real capture: queue cleared", len(ph.queue) == 0)

# boolean flag: expect True for 100% rollout
v1 = ph.get_flag("micropython-sim-test")
check("real flag micropython-sim-test is True", v1 is True, "value: %r" % v1)
p1 = ph.get_payload("micropython-sim-test")
check("real payload micropython-sim-test", p1 == {"level": 3}, "payload: %r" % p1)

# multivariate flag: expect one of control/turbo/eco, plus its payload
v2 = ph.get_flag("esp32-sensor-mode")
check("real flag esp32-sensor-mode is a variant", v2 in ("control", "turbo", "eco"), "value: %r" % v2)
p2 = ph.get_payload("esp32-sensor-mode")
speed_ok = isinstance(p2, dict) and p2.get("speed") in (1, 2, 3)
check("real payload esp32-sensor-mode", speed_ok, "payload: %r" % p2)

if fails:
    sys.exit(1)
print("real round trip OK")