"""LIVE sim — GPS-course heading fusion rescues a biased magnetometer.

A magnetometer with an uncorrected hard-iron offset reads a constant ~25 deg wrong.
The rover drives a route while two HeadingFilters estimate heading from the SAME
noisy signals:

  A (mag only): fuses the biased magnetometer.
  B (mag + GPS course): also fuses GPS course-over-ground (drift-free, magnetically
    immune) once moving fast enough — pulling the estimate off the magnetic bias.

GPS course here is derived by differencing successive (noisy) GPS fixes, so it is
noisy but UNBIASED; averaged over the run it cancels the magnetometer's bias.

    .venv/bin/python tests/live/test_live_heading.py
"""

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mini_plus_agent_kit.estimator import HeadingFilter
from mini_plus_agent_kit.geo import gps_course_and_speed, heading_error_deg

DT = 0.2
SPEED = 1.5                 # m/s
BASE_LAT, BASE_LON = 37.87, -122.25
M = 111_320.0
MLON = M * math.cos(math.radians(BASE_LAT))
MAG_BIAS = 25.0            # deg hard-iron offset (uncalibrated)
MAG_NOISE = 6.0
GPS_SIGMA = 0.3            # Doppler-quality course (displacement per fix >> noise)
FIX_EVERY = 5             # GPS at 1 Hz


def run(use_course, seed=11):
    rng = random.Random(seed)
    f = HeadingFilter()
    x = y = 0.0
    th = 30.0                                   # constant true heading (driving NE-ish)
    prev_fix = None
    t_since = 0.0
    err_sq = n = 0
    for t in range(500):
        mag = (th + MAG_BIAS + rng.gauss(0, MAG_NOISE)) % 360.0
        course = None
        speed = 0.0
        t_since += DT
        if t % FIX_EVERY == 0:
            fix = (BASE_LAT + (y + rng.gauss(0, GPS_SIGMA)) / M,
                   BASE_LON + (x + rng.gauss(0, GPS_SIGMA)) / MLON)
            if prev_fix is not None and use_course:
                course, speed = gps_course_and_speed(prev_fix[0], prev_fix[1],
                                                     fix[0], fix[1], t_since)
            prev_fix = fix
            t_since = 0.0
        h = f.update(DT, gyro_z_dps=0.0, absolute_deg=mag,
                     course_deg=course, speed_mps=speed)
        if t > 100:                              # skip convergence transient
            err_sq += heading_error_deg(h, th) ** 2
            n += 1
        # true kinematics (straight line at constant heading)
        x += SPEED * DT * math.sin(math.radians(th))
        y += SPEED * DT * math.cos(math.radians(th))
    return math.sqrt(err_sq / n)


def main():
    a = run(use_course=False)
    b = run(use_course=True)
    print(f"magnetometer hard-iron bias = {MAG_BIAS:.0f} deg, GPS sigma = {GPS_SIGMA} m")
    print(f"  mag only            heading_rmse = {a:5.1f} deg")
    print(f"  mag + GPS course    heading_rmse = {b:5.1f} deg")
    assert a > MAG_BIAS * 0.6, a               # mag-only is stuck near the bias
    assert b < a * 0.5, (b, a)                 # course fusion roughly halves the error
    print(f"\n  -> GPS-course fusion cut heading error {a / b:.1f}x by overriding the "
          f"biased magnetometer")
    print("\nLIVE HEADING PASSED (speed-gated GPS-course fusion rescues a biased magnetometer)")


if __name__ == "__main__":
    main()
