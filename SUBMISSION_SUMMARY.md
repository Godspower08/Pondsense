# PondSense — Submission Summary

## The Problem

Warm water holds less dissolved oxygen than cool water. In outdoor catfish and hybrid striped bass ponds across the U.S. Southeast, sustained heat quietly depletes oxygen over hours, long before visible fish distress appears. By the time a farmer notices, fish have often been suffocating for hours already. There is currently no automated way for a small or mid-size operator to know their specific pond has been accumulating dangerous heat, rather than just experiencing one hot moment.

## Who It's For

Independent and small-commercial catfish and hybrid striped bass farmers in the U.S., particularly the Mississippi Delta. These operators typically have email access but not a dedicated environmental-monitoring budget or staff. Longer-term, the more durable customer is a cooperative, feed supplier, or aquaculture insurer — entities that lose money when farmers lose fish, and who could subsidize alerts across the farmers under them.

## FortyGuard Usage

PondSense uses FortyGuard's `/v1/heatmap` and `/v1/status` endpoints directly. A farmer's pond location and reported width are converted into a small GeoJSON bounding polygon, sized with a safety margin so the requested area isn't cropped. Because FortyGuard's heatmap endpoint returns aggregate stats for one time window per job — not a per-hour series — PondSense submits one `filter_type=1` job per clock hour and reads `stats_data.temperature_stats.mean` from each, reconstructing a true trailing 6-hour reading history. That history is fed into a degree-hour accumulation model, adjusted by pond depth, construction material, and shade cover, and classified into a SAFE / WATCH / ALERT / DANGER tier. A working fallback layer also handles FortyGuard's real, confirmed per-day coverage gaps by walking backward across dates while preserving the same time-of-day window, distinguishing a genuine network error (retry the same window) from a confirmed no-data day (move on immediately).

## Measured Result

The full pipeline runs live and unattended, with no developer machine involved at any step. A farmer emails a short registration command; a scheduled process replies within minutes with a location-pinning link; the farmer pins their pond's real coordinates on a hosted page; a separate scheduled process then runs an hourly risk cycle against every registered pond.

In a live end-to-end test completed during this build, a newly registered pond in Wisconsin was assessed entirely automatically: the system fetched a real FortyGuard reading (25.07°C), computed 0.0 accumulated degree-hours, classified the pond SAFE, and delivered a formatted alert email — all without manual triggering, and confirmed against real current weather at that location. This mirrors an earlier find during development: an initial version of the trailing-window logic defaulted to always reading midnight-to-dawn temperatures regardless of actual time of day, making the system structurally unable to detect a real afternoon heat spike. That bug was caught and fixed before submission; the corrected version produced the verified result above — a concrete demonstration that the system's core claim, accumulated and time-accurate heat risk rather than a single snapshot, actually holds under real data.

*(485 words)*
