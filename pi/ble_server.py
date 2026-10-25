#!/usr/bin/env python3
"""
Bamnun (밤눈) - Raspberry Pi BLE server, version 2.

Sends the driver's drowsiness status to the phone app over Bluetooth Low Energy,
about once per second, as short UTF-8 text (kept under 20 bytes so it fits in the
default BLE packet):

    "<stage>,<perclos_pct>,<ear>,<yawn>,<nod>"        example: "3,17,0.15,1,0"

    stage        0..4, from the research table (see stage_from_metrics)
    perclos_pct  PERCLOS in percent, whole number, 0..100
    ear          current Eye Aspect Ratio, 2 decimals
    yawn, nod    0 or 1

Run:
    python3 ble_server.py                  # real detector (fill in read_state_real)
    python3 ble_server.py --simulate       # DEVELOPMENT ONLY: fake eyes, no camera
    python3 ble_server.py --perclos-window 30    # shorter window = faster stage changes

Install:   pip install bless
(Needs BlueZ 5.50+, which Raspberry Pi OS includes.)
"""
import argparse
import asyncio
import logging
import random
import time
from collections import deque

# Must match web app (index.html)
SERVICE_UUID = "b4a10001-5c1e-4d6e-9a53-0a1b2c3d4e5f"
EVENT_CHAR_UUID = "b4a10002-5c1e-4d6e-9a53-0a1b2c3d4e5f"
DEVICE_NAME = "Bamnun-Pi"

CLOSED_EAR = 0.18      # EAR below this = eye counts as closed (research doc)
MICROSLEEP_MS = 500    # continuous closure longer than this = microsleep (stage 4)

log = logging.getLogger("bamnun")


# --------------------------------------------------------------------------
# Science: stages and PERCLOS
# --------------------------------------------------------------------------
def stage_from_metrics(perclos_pct, longest_closure_ms=0):
    """Map PERCLOS (%) and the longest recent eye closure (ms) to a stage 0..4.

    From the team's literature review (four-stage table):
        stage 1  PERCLOS  5-10 %   (KSS 6)
        stage 2  PERCLOS 10-15 %   (KSS 7)
        stage 3  PERCLOS 15-20 %   (KSS 8)
        stage 4  PERCLOS > 20 % or a microsleep > 500 ms (KSS 9)
    Below 5 % the driver is treated as alert (KSS 5 or lower).
    """
    if longest_closure_ms > MICROSLEEP_MS or perclos_pct > 20:
        return 4
    if perclos_pct >= 15:
        return 3
    if perclos_pct >= 10:
        return 2
    if perclos_pct >= 5:
        return 1
    return 0


class PerclosTracker:
    """Computes PERCLOS and microsleep length from a stream of EAR values.

    PERCLOS = share of time in the sliding window with the eyes closed.
    Here "closed" means EAR < closed_ear (an approximation of the 80 % closure rule).
    The research suggests a window of 1-3 minutes. A shorter window reacts
    faster but is noisier; for a live demo 20-30 s is a reasonable compromise.
    """

    def __init__(self, window_s=60.0, closed_ear=CLOSED_EAR, microsleep_hold_s=10.0):
        self.window_s = window_s
        self.closed_ear = closed_ear
        self.hold_s = microsleep_hold_s       # how long a long closure keeps counting
        self.frames = deque()                 # (time, closed?)
        self.closure_start = None
        self.recent_closures = deque()        # (end_time, duration_s)

    def update(self, ear, t=None):
        """Feed one EAR sample. Returns (perclos_pct, longest_closure_ms)."""
        t = time.monotonic() if t is None else t
        closed = ear < self.closed_ear
        self.frames.append((t, closed))
        while self.frames and t - self.frames[0][0] > self.window_s:
            self.frames.popleft()

        if closed:
            if self.closure_start is None:
                self.closure_start = t
        elif self.closure_start is not None:
            self.recent_closures.append((t, t - self.closure_start))
            self.closure_start = None
        while self.recent_closures and t - self.recent_closures[0][0] > self.hold_s:
            self.recent_closures.popleft()

        return self.perclos_pct(), self.longest_closure_ms(t)

    def perclos_pct(self):
        fr = list(self.frames)
        if len(fr) < 2:
            return 0.0
        total = fr[-1][0] - fr[0][0]
        if total < 5.0:            # too little data to be meaningful
            return 0.0
        closed_time = 0.0
        for (t0, c0), (t1, _) in zip(fr, fr[1:]):
            if c0:
                closed_time += t1 - t0
        return 100.0 * closed_time / total

    def longest_closure_ms(self, t):
        current = (t - self.closure_start) if self.closure_start is not None else 0.0
        past = max((d for _, d in self.recent_closures), default=0.0)
        return int(1000 * max(current, past))


def encode(state):
    stage, perclos, ear, yawn, nod = state
    return f"{int(stage)},{round(perclos)},{ear:.2f},{int(bool(yawn))},{int(bool(nod))}".encode("utf-8")


# --------------------------------------------------------------------------
# Data sources
# --------------------------------------------------------------------------
def make_real_reader(window_s):
    tracker = PerclosTracker(window_s=window_s)

    def read_state_real():
        """Connect your detector here and return (stage, perclos, ear, yawn, nod).

        Replace the call below with the latest values from your camera pipeline:
            ear   - current Eye Aspect Ratio (average of both eyes)
            yawn  - True if the mouth aspect ratio says the driver is yawning
            nod   - True if head pose says the head is nodding
        """
        ear, yawn, nod = get_detector_output()
        perclos, closure_ms = tracker.update(ear)
        stage = stage_from_metrics(perclos, closure_ms)
        return stage, perclos, ear, yawn, nod

    return read_state_real


def get_detector_output():
    raise NotImplementedError(
        "Implement get_detector_output() to return (ear, yawn, nod), "
        "or run with --simulate for development."
    )


def make_simulator(window_s):
    """DEVELOPMENT ONLY. Fake eyes that slowly get sleepier, then reset.
    The fake EAR goes through the same PERCLOS tracker as real data."""
    tracker = PerclosTracker(window_s=window_s)
    st = {"fatigue": 0.0, "hold": 0}

    def read_state():
        if st["fatigue"] >= 1.0:
            st["hold"] += 1
            if st["hold"] > 20:
                st["fatigue"], st["hold"] = 0.05, 0
        else:
            st["fatigue"] = min(1.0, st["fatigue"] + 0.012)
        f = st["fatigue"]
        closed = random.random() < 0.02 + 0.55 * f
        ear = random.uniform(0.09, 0.15) if closed else random.uniform(0.24, 0.29)
        yawn = random.random() < f * 0.12
        nod = random.random() < f * 0.08
        perclos, closure_ms = tracker.update(ear)
        return stage_from_metrics(perclos, closure_ms), perclos, ear, yawn, nod

    return read_state


# --------------------------------------------------------------------------
# BLE server
# --------------------------------------------------------------------------
async def run(simulate, hz, window_s):
    from bless import (  # imported here so the helpers above can be tested anywhere
        BlessServer,
        GATTAttributePermissions,
        GATTCharacteristicProperties,
    )

    loop = asyncio.get_running_loop()
    server = BlessServer(name=DEVICE_NAME, loop=loop)
    await server.add_new_service(SERVICE_UUID)
    props = GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify
    await server.add_new_characteristic(
        SERVICE_UUID, EVENT_CHAR_UUID, props, bytearray(b"0,0,0.30,0,0"),
        GATTAttributePermissions.readable,
    )
    await server.start()
    log.info("Advertising as %s (simulate=%s, PERCLOS window=%ss)", DEVICE_NAME, simulate, window_s)

    read_state = make_simulator(window_s) if simulate else make_real_reader(window_s)
    period = 1.0 / hz
    try:
        while True:
            state = read_state()
            msg = encode(state)
            server.get_characteristic(EVENT_CHAR_UUID).value = bytearray(msg)
            server.update_value(SERVICE_UUID, EVENT_CHAR_UUID)
            log.info("sent %s", msg.decode())
            await asyncio.sleep(period)
    finally:
        await server.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulate", action="store_true", help="development only: fake data")
    parser.add_argument("--hz", type=float, default=1.0, help="messages per second")
    parser.add_argument("--perclos-window", type=float, default=60.0,
                        help="PERCLOS window in seconds (research: 60-180)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(run(args.simulate, args.hz, args.perclos_window))


if __name__ == "__main__":
    main()
