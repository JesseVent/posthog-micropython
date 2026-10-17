# posthog_micropython.py — PostHog for MicroPython (ESP32).
# Single file, drop onto the device. Speaks the same wire format as
# posthog-python: POST {"api_key", "sent_at", "batch": [...]} to /batch/.
# No threads: capture() queues in RAM, flush() sends, auto-flush at flush_at
# or after flush_interval ms on the next capture.
#
# Features: capture, identify (anon binding), alias, $set/$set_once person
# properties, group analytics ($group + $groupidentify), exception capture,
# persistent device identity (NVS/file), feature flags via /decide.

import json
import os
import time

try:
    _ticks_ms = time.ticks_ms
    _ticks_diff = time.ticks_diff
except AttributeError:  # CPython fallback (lets the offline test run)
    _ticks_ms = lambda: int(time.time() * 1000)
    _ticks_diff = lambda b, a: b - a

try:
    import urequests as requests
except ImportError:
    import requests  # CPython fallback (urequests is micropython-stdlib)

__version__ = "0.2.0"

SDK_NAME = "posthog-micropython"
DEFAULT_HOST = "https://us.i.posthog.com"
DEVICE_ID_FILE = ".posthog_device_id"

# Event size is the real heap constraint, not event count — the payload is
# built as one JSON string. Cap the whole queue by bytes, keep dropping the
# oldest when full so fresh events always win.
DEFAULT_MAX_QUEUE_BYTES = 8 * 1024


def _uuid():
    b = os.urandom(16)
    b = b[:6] + bytes([b[6] | 0x40, b[7] & 0x0F]) + b[8:16]
    h = "".join("%02x" % c for c in b)
    return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:])


def _persist_device_id(persist):
    """Load (or mint once) a stable device id across reboots, or None."""
    if not persist:
        return None
    # ESP32: NVS
    try:
        if hasattr(os, "NVS"):
            n = os.NVS("phog")
            try:
                b = n.get_blob("did")
            except OSError:
                b = None
            if b:
                return b.decode()
            did = _uuid()
            n.set_blob("did", did)
            n.commit()
            return did
    except Exception:
        pass  # NVS unavailable/broken — fall through to file
    # Unix port / generic: file in cwd
    try:
        with open(DEVICE_ID_FILE) as f:
            v = f.read().strip()
        if v:
            return v
    except OSError:
        pass
    try:
        with open(DEVICE_ID_FILE, "w") as f:
            f.write(_uuid())
        with open(DEVICE_ID_FILE) as f:
            return f.read().strip()
    except OSError:
        return None


class Client:
    def __init__(
        self,
        api_key,
        host=DEFAULT_HOST,
        flush_at=20,
        flush_interval=None,
        timeout=10,
        max_queue_bytes=DEFAULT_MAX_QUEUE_BYTES,
        on_error=None,
        persist=False,
    ):
        """persist: keep a stable device id across reboots (NVS on ESP32,
        a file elsewhere). Without it every boot looks like a new user."""
        if not host.startswith("http"):
            host = "https://" + host
        self.api_key = api_key
        self.host = host.rstrip("/")
        self.flush_at = flush_at
        self.flush_interval = flush_interval
        self.timeout = timeout
        self.max_queue_bytes = max_queue_bytes
        self.on_error = on_error
        self.queue = []
        self._queue_bytes = 0
        self._queue_since = _ticks_ms()
        self._distinct_id = _persist_device_id(persist)
        self._groups = {}  # {group_type: group_key} attached to every capture

    # ---- capture ----

    def capture(self, event, distinct_id=None, properties=None, timestamp=None, groups=None):
        """Queue a capture. Call flush() to send (done automatically when
        flush_at events are queued or flush_interval ms have elapsed)."""
        distinct_id = distinct_id or self._distinct_id
        if distinct_id is None:
            distinct_id = self._distinct_id = _uuid()
        props = {"$lib": SDK_NAME, "$lib_version": __version__}
        if properties:
            props.update(properties)
        grp = dict(self._groups)
        if groups:
            grp.update(groups)
        if grp:
            props["$group"] = grp
        ev = {
            "event": event,
            "distinct_id": str(distinct_id),
            "properties": props,
        }
        if timestamp:
            ev["timestamp"] = timestamp  # ISO-8601 UTC string if known
        self._enqueue(ev)
        return ev

    # ---- person / identity ----

    def identify(self, distinct_id, properties=None):
        """Bind the client's anonymous (or device) id to a stable one, then
        track an $identify event. properties become $set on the person."""
        old = self._distinct_id
        self._distinct_id = str(distinct_id)
        props = dict(properties or {})
        if old and old != self._distinct_id:
            props["$anon_distinct_id"] = old
        self.capture("$identify", properties=props)

    def alias(self, alias, distinct_id=None):
        self.capture("$create_alias", distinct_id=distinct_id, properties={"alias": str(alias)})

    def set(self, person_properties, distinct_id=None):
        """Update person properties ($set)."""
        self.capture("$set", distinct_id=distinct_id, properties={"$set": person_properties})

    def set_once(self, person_properties, distinct_id=None):
        """Set person properties only if not already present ($set_once)."""
        self.capture("$set_once", distinct_id=distinct_id, properties={"$set_once": person_properties})

    # ---- group analytics ----

    def group(self, group_type, group_key, group_properties=None):
        """Attach a group to this client (and thus every later capture) and
        send a $groupidentify event when group_properties are given."""
        self._groups[group_type] = str(group_key)
        if group_properties is not None:
            props = {"$group_type": group_type, "$group_key": str(group_key)}
            props.update(group_properties)
            self.capture("$groupidentify", properties=props)

    # ---- errors ----

    def capture_exception(self, exc, properties=None):
        """Send an $exception event so errors show up in PostHog error
        tracking. MicroPython tracebacks are strings only, so no frames."""
        tp = type(exc).__name__
        msg = str(exc)
        props = {
            "$exception_type": tp,
            "$exception_message": msg,
            "$exception_list": [{"type": tp, "value": msg}],
        }
        if properties:
            props.update(properties)
        self.capture("$exception", properties=props)

    # ---- queue ----

    def _enqueue(self, ev):
        size = len(json.dumps(ev))
        # Drop oldest until the new event fits.
        while self._queue_bytes + size > self.max_queue_bytes and self.queue:
            _, dropped = self.queue.pop(0)
            self._queue_bytes -= dropped
        self.queue.append((ev, size))
        self._queue_bytes += size
        self._auto_flush()

    def flush(self):
        """Send queued events. Returns the number of events sent."""
        if not self.queue:
            return 0
        batch = [ev for ev, _ in self.queue]
        body = {"api_key": self.api_key, "batch": batch, "sent_at": _utcnow()}
        ok = 0
        try:
            res = requests.post(
                self.host + "/batch/",
                json=body,
                headers={"User-Agent": "%s/%s" % (SDK_NAME, __version__)},
                timeout=self.timeout,
            )
            ok = 200 <= res.status_code < 300
            res.close()
            if not ok and self.on_error:
                self.on_error("status %d" % res.status_code, batch)
        except Exception as e:
            if self.on_error:
                self.on_error(e, batch)
        # Drop the queue only on success (ponytail: on failure we keep and
        # retry everything next flush; add per-event retry/partial-drop
        # bookkeeping if you burn through queue space from failures).
        if ok:
            self.queue.clear()
            self._queue_bytes = 0
        self._queue_since = _ticks_ms()
        return len(batch) if ok else 0

    def _auto_flush(self):
        if len(self.queue) >= self.flush_at:
            self.flush()
        elif self.flush_interval is not None:
            due = _ticks_diff(_ticks_ms(), self._queue_since) >= self.flush_interval
            if due and self.queue:
                self.flush()

    def close(self):
        self.flush()

    # ---- feature flags ----

    def decide(self, distinct_id=None):
        """Full /decide/?v=3 response: {"featureFlags", "payloads", ...}.
        Blocking HTTP every call — cache in a loop."""
        import urequests

        body = {
            "api_key": self.api_key,
            "distinct_id": str(distinct_id or self._distinct_id or _uuid()),
        }
        try:
            res = urequests.post(self.host + "/decide/?v=3", json=body, timeout=self.timeout)
            data = res.json()
            res.close()
            return data
        except Exception as e:
            if self.on_error:
                self.on_error(e, None)
            return None

    def get_flag(self, key, distinct_id=None):
        """One feature flag value (str variant / bool / None if off)."""
        data = self.decide(distinct_id) or {}
        return (data.get("featureFlags") or {}).get(key)

    def get_payload(self, key, distinct_id=None):
        """Flag payload (data shipped with the flag) or None. The server
        ships payloads as stringified JSON — parsed into a dict here."""
        data = self.decide(distinct_id) or {}
        payloads = data.get("featureFlagPayloads") or data.get("payloads") or {}
        val = payloads.get(key)
        if isinstance(val, str) and val[:1] in "{[":
            try:
                val = json.loads(val)
            except ValueError:
                pass
        return val


def _utcnow():
    # If the RTC hasn't been synced (NTP/boot-time), time.time() is near-epoch
    # garbage — return None and let PostHog stamp ingest time instead.
    t = time.time()
    if t < 1577836800:  # before 2020-01-01 → no sane clock
        return None
    y, mo, d, h, mi, se, *_ = time.gmtime(t)
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (y, mo, d, h, mi, se)