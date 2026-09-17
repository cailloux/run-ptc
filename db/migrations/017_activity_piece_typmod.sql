-- Missed by 016_network_enforce.sql: activity_piece.geom is rebuilt from
-- activity.geom (app/matching.py's rebuild_pieces, via track_pieces()), so
-- once a network's activity.geom can hold a non-32616 SRID, its pieces need
-- to too. No network_id column and no trigger here, same reasoning as
-- node: the SRID is inherited mechanically from activity.geom in the same
-- statement, so a wrong value here is only possible if activity.geom (which
-- the trigger does check) was already wrong.
ALTER TABLE activity_piece ALTER COLUMN geom TYPE geometry(LineString);
