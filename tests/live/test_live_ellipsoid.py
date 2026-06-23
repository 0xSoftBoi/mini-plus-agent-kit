"""LIVE — full ellipsoid magnetometer calibration vs the diagonal min/max method.

Soft-iron distortion is in general a *rotated* ellipsoid (off-diagonal cross-axis
coupling), not just per-axis scaling. The stdlib min/max calibration only re-centres
and re-scales on the diagonal, so it cannot undo the rotation; the full ellipsoid
least-squares fit (numpy) recovers the complete shaping matrix. We distort a magnetic
field through a known rotated ellipsoid + hard-iron offset, calibrate both ways, and
compare heading RMSE on the horizontal plane.

    .venv/bin/python tests/live/test_live_ellipsoid.py
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np

from mini_plus_agent_kit.estimator import MagnetometerCalibrator, mag_heading_deg
from mini_plus_agent_kit.geo import heading_error_deg

HARD_IRON = np.array([0.4, -0.3, 0.2])


def _rot(ax, ay, az):
    cx, sx = math.cos(ax), math.sin(ax)
    cy, sy = math.cos(ay), math.sin(ay)
    cz, sz = math.cos(az), math.sin(az)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


# soft-iron = R diag(scale) Rᵀ  → a rotated ellipsoid (off-diagonal terms)
R = _rot(0.3, 0.4, 0.6)
SOFT = R @ np.diag([1.7, 0.6, 1.1]) @ R.T


def raw(true_unit):
    return SOFT @ true_unit + HARD_IRON


def main():
    cal = MagnetometerCalibrator()
    # collect samples over a 3-D spread of orientations (a real calibration tumble)
    for az in range(0, 360, 12):
        for el in range(-60, 61, 15):
            ra, re = math.radians(az), math.radians(el)
            t = np.array([math.sin(ra) * math.cos(re), math.cos(ra) * math.cos(re), math.sin(re)])
            r = raw(t)
            cal.add(float(r[0]), float(r[1]), float(r[2]))
    cal.fit()                # diagonal min/max
    cal.fit_ellipsoid()      # full ellipsoid (numpy)

    raw_e = diag_e = ell_e = 0.0
    headings = list(range(0, 360, 5))
    for az in headings:
        ra = math.radians(az)
        t = np.array([math.sin(ra), math.cos(ra), 0.0])     # horizontal field
        r = raw(t)
        mx, my, mz = float(r[0]), float(r[1]), float(r[2])
        raw_e += heading_error_deg(mag_heading_deg(mx, my), az) ** 2
        diag_e += heading_error_deg(cal.heading_deg(mx, my, mz), az) ** 2
        ell_e += heading_error_deg(cal.heading_ellipsoid_deg(mx, my, mz), az) ** 2
    n = len(headings)
    raw_rmse, diag_rmse, ell_rmse = (math.sqrt(e / n) for e in (raw_e, diag_e, ell_e))

    print("rotated soft-iron ellipsoid + hard-iron offset")
    print(f"  uncalibrated heading RMSE      : {raw_rmse:5.1f} deg")
    print(f"  diagonal min/max calibration   : {diag_rmse:5.1f} deg")
    print(f"  full ellipsoid calibration     : {ell_rmse:5.1f} deg")

    assert ell_rmse < 1.0, ell_rmse                 # ellipsoid fit nearly perfect
    assert ell_rmse < diag_rmse * 0.5, (ell_rmse, diag_rmse)   # clearly beats diagonal
    factor = f"{diag_rmse / ell_rmse:.0f}x" if ell_rmse > 0.05 else ">300x"
    print(f"\n  -> on a rotated ellipsoid the full fit cuts heading error to {ell_rmse:.2f} deg, "
          f"{factor} better than the diagonal method")
    print("\nLIVE ELLIPSOID PASSED (full hard+soft-iron calibration recovers heading)")


if __name__ == "__main__":
    main()
