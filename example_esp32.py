# example.py — ESP32 usage: connect, sync clock, capture, flush.
# Copy posthog_micropython.py and this file onto the device (e.g. via
# mpremote cp posthog_micropython.py :/).

import network
import ntptime
from posthog_micropython import Client

WIFI_SSID = "your-ssid"
WIFI_PASS = "your-password"
POSTHOG_KEY = "phc_..."  # project API key (the public one, not the personal key)
POSTHOG_HOST = "https://us.i.posthog.com"  # or eu.i.posthog.com / self-hosted URL


def wait_for_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(WIFI_SSID, WIFI_PASS)
        while not wlan.isconnected():
            pass
    print("wifi:", wlan.ifconfig()[0])


def main():
    wait_for_wifi()
    # Sync the RTC so events get real timestamps; skip if there's no network time.
    try:
        ntptime.settime()
    except Exception:
        pass

    ph = Client(
        api_key=POSTHOG_KEY,
        host=POSTHOG_HOST,
        flush_at=10,       # auto-send every 10 queued events
        timeout=10,
    )

    def on_error(err, batch):
        print("posthog error:", err)

    ph.on_error = on_error

    ph.capture("device_boot", properties={"firmware": "1.2.0", "model": "ESP32"})
    ph.flush()

    # Feature flag (blocking HTTP call — cache the value if polled often)
    mode = ph.get_flag("esp32-sensor-mode")
    print("sensor-mode:", mode)

    # Main loop: capture periodically, flush happens at flush_at or call ph.flush()
    while True:
        ph.capture("sensor_reading", properties={"c": 42})
        ph.flush()
        import time
        time.sleep(60)


main()