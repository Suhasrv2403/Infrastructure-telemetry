# pipeline/stage3_enrich/

Stage 3 Enrich:
- 3a: time grid (5-min for Powerwall/Powerpack, 1-min for others) with coverage/mode labels.
- 3b: device x event (sessions, dispatch windows, outages).
- 3c: device x day features.
Also owns the device history dimension (firmware, hardware rev, cell lot, site, climate) with
validity periods for as-of joins.

Implemented starting P2-01 (device history dimension) through P2-05 (device-day features).
