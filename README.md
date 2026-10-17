# posthog-micropython

Minimal PostHog client for MicroPython (ESP32 and friends). Single file,
speaks the same `/batch/` wire format as [posthog-python](https://github.com/PostHog/posthog-python) —
see its [capture spec](https://github.com/PostHog/sdk-specs) for what lands in the product.

This is a clean-room client, not a port: the real SDK (threads, asyncio,
compression, typing) is far too heavy for an ESP32 heap.

## Files

- `posthog_micropython.py` — the client (drop onto the device)
- `example_esp32.py` — wifi + clock sync + capture loop
- `test_posthog_micropython.py` — offline checks (`python3 -I test_posthog_micropython.py`)
- `sim_test.py` / `real_test.py` — MicroPython unix-port tests (mock server / live PostHog)
- `urequests.py` — vendored from [micropython-lib](https://github.com/micropython/micropython-lib) (MIT)

## Install

```sh
mpremote cp posthog_micropython.py :/
# or: ampy put / mip via a gist
```

## Usage

```python
from posthog_micropython import Client

ph = Client(api_key="phc_...", host="https://us.i.posthog.com", persist=True)
ph.capture("device_boot", properties={"model": "ESP32"})
ph.capture("feature_used", properties={"feature": "night-light"})
ph.flush()  # also auto-flushes when flush_at events are queued
```

- `capture(event, distinct_id=..., properties=..., groups=...)` — queues in RAM;
  auto-generates an id the first time, binds it with `identify()`. `groups`
  attaches `$group` per-event; client-level groups via `group()`.
- `identify(distinct_id, properties=...)` — binds the anon/device id (properties become `$set`).
- `alias(alias)` — `$create_alias`.
- `set({k: v})` / `set_once({k: v})` — person property updates (`$set`/`$set_once` events).
- `group(type, key, group_properties=...)` — group analytics, attached to all later captures,
  plus a `$groupidentify` event when properties given.
- `capture_exception(e)` — `$exception` event with `$exception_list` (no stack frames —
  MicroPython exceptions have string tracebacks only); lands in error tracking.
- `persist=True` — stable device id across reboots (NVS on ESP32, file elsewhere). Without
  it every boot looks like a new user; without identify the id is regenerated — pair with
  `persist=True` for boards that never `identify()`.
- `flush()` — one blocking POST to `/batch/`. On failure the queue is kept for
  the next flush; on success it clears. Queue is byte-capped (default 8 KB,
  oldest dropped) so the heap can't die. Call it periodically from your loop —
  there's no background thread.
- `decide()` / `get_flag(key)` / `get_payload(key)` — feature flags via `/decide/?v=3`.
  Blocking; cache rather than polling.
- Set the RTC (`ntptime.settime()`) or events carry no timestamp and PostHog
  stamps ingest time.

## What's deliberately not here

Queued retries with Retry-After, gzip (MicroPython urequests can't gzip),
surveys, session replay, AI events, and local flag evaluation (flags are
evaluated server-side via `/decide/`; local evaluation of flag definitions is
the next feature if on-device latency matters).