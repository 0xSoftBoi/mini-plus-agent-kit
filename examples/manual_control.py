"""Direct SDK control without the agent — drive a square and grab a frame.

    python examples/manual_control.py
"""

import time

from mini_plus_agent_kit import EarthRoverClient


def main() -> None:
    with EarthRoverClient("http://localhost:8000") as rover:
        print("telemetry:", rover.data().summary())

        for i in range(4):
            print(f"side {i + 1}: forward")
            rover.control(linear=0.6, angular=0.0)
            time.sleep(1.0)
            rover.stop()

            print(f"corner {i + 1}: turn")
            rover.control(linear=0.0, angular=0.6)
            time.sleep(0.7)
            rover.stop()

        # Save a front frame to disk.
        front = rover.front()
        with open("front.jpg", "wb") as f:
            f.write(EarthRoverClient.decode_frame(front["front_frame"]))
        print("saved front.jpg")


if __name__ == "__main__":
    main()
