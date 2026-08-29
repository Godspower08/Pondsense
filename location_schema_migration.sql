-- PondSense — location pinning migration
-- Run this in the Supabase SQL editor against the existing `ponds` table.

ALTER TABLE ponds ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION;
ALTER TABLE ponds ADD COLUMN IF NOT EXISTS lng DOUBLE PRECISION;
ALTER TABLE ponds ADD COLUMN IF NOT EXISTS accuracy_m DOUBLE PRECISION;
ALTER TABLE ponds ADD COLUMN IF NOT EXISTS location_method TEXT; -- 'gps_confirmed' | 'maps_link' | 'manual_pin' | NULL
ALTER TABLE ponds ADD COLUMN IF NOT EXISTS location_token TEXT UNIQUE;

-- Farmer-reported longest dimension across the pond, in meters.
-- Captured on the SAME location-pinning page as the lat/lng pin, not
-- at JOIN time (JOIN stays a short SMS-style command). Used by
-- api_adapter.py to size the FortyGuard AOI bounding box correctly -
-- a pond bigger than the default box gets cropped (reads only one
-- corner's temperature); a pond much smaller than the box gets
-- diluted with surrounding land noise. NULL for ponds registered
-- before this field existed - api_adapter.py falls back to a
-- conservative default box size in that case.
ALTER TABLE ponds ADD COLUMN IF NOT EXISTS pond_width_m DOUBLE PRECISION;

-- Set TRUE the first time a "please pin your pond" gap-notification
-- email goes out for a pond missing lat/lng or pond_width_m. Prevents
-- re-sending the same nudge every orchestrator cycle. Reset to FALSE
-- automatically by farmer_data.py's update_pond_location() the moment
-- the farmer actually submits a location - see orchestrator.py.
ALTER TABLE ponds ADD COLUMN IF NOT EXISTS location_gap_notified BOOLEAN NOT NULL DEFAULT FALSE;

-- zip_code is no longer collected at JOIN time. Safe to drop once
-- you've confirmed nothing else reads it (api_adapter.py, if it was
-- ever wired to zip-based lookups, would need to switch to lat/lng
-- first). Left commented out rather than run automatically:
-- ALTER TABLE ponds DROP COLUMN IF EXISTS zip_code;
