# Pan/tilt engineering report

Check estimator NEES, path error, the fault campaign, and the setpoint audit.

## Checks

- nees: pass. NEES=3.2708200768152946
- path-rmse: pass. RMSE=0.2471781076988839
- firmware-campaign: pass. passed=8
- tuning: pass. before/after present
- setpoint-sim-only: pass. mode=sim

## Estimator and axis

- Backend: short
- Average NEES: 3.2708200768152946
- Position RMSE (m): 0.2471781076988839
- Pan RMS (rad): 0.09578337426538916
- Rise time (s): 0.2680000000000002
- Overshoot (%): 9.559244738775513

## Tuning

- Rationale: Overshoot is above 8 percent, so position and rate gains were reduced.
- Delta overshoot (percentage points): -3.83098896726411

## Firmware campaign

- Mode: lightweight
- Passed cases: 8

## Setpoint audit

- Sent: True
- Mode: sim
- Rejection: none

Commands were limited to a sim or Renode shell. No physical positioner was addressed.
