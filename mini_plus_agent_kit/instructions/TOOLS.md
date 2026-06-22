# Environment notes

The exact tools available to you depend on the robot:
- **EarthRover Mini+**: front+rear cameras, GPS, speaker (`speak`), `track_color`,
  `autonav`, blocking heading-feedback `turn`.
- **Waveshare UGV**: single forward camera, **lidar** (use `obstacle_check`), no
  speaker, closed-loop `turn` from yaw. No GPS or missions.

Only the supported verbs are offered to you as tools — if a verb isn't in your tool
list, the current robot doesn't have it.
