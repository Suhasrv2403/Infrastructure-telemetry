# pipeline/stage3_enrich/

Stage 3 Enrich:
- 3a: time grid (5-min for Powerwall/Powerpack, 1-min for others) with coverage/mode labels.
- 3b: device x event (sessions, dispatch windows, outages).
- 3c: device x day features.
Also owns the device history dimension (firmware, hardware rev, cell lot, site, climate) with
validity periods for as-of joins.

Implemented starting P2-01 (device history dimension) through P2-05 (device-day features).

P2-03 note: the 1-min grid for supercharger_stall/supercharger_cabinet is implemented
(pipeline/stage3_enrich/time_grid.py). The 5-min Powerwall/Powerpack grid named above is
not - no Powerwall/Powerpack fixtures or catalog entries exist anywhere in this repo yet;
see time_grid.py's module docstring for the full scope note.

P2-05 note: 3c device-day features (pipeline/stage3_enrich/device_day.py) are implemented
for the same device classes time_grid.py's grid supports
(supercharger_stall/supercharger_cabinet) - built from Stage 3a GridBucket data, not raw
rows. No Stage 3b (device x event, P2-04) and no device history dimension (P2-01) exist
yet; see device_day.py's module docstring for the full scope note and the dirty-day
recompute design.
