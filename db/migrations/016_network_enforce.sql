ALTER TABLE segment ALTER COLUMN network_id SET NOT NULL;

ALTER TABLE segment DROP CONSTRAINT segment_layer_source_key_key,
    ADD CONSTRAINT segment_network_id_layer_source_key_key UNIQUE (network_id, layer, source_key);

-- Relax every network-scoped geometry column's fixed SRID typmod, so a
-- network with a different UTM zone (e.g. a Zwift world) can store its own
-- geometry in the same columns. activity.track_raw is deliberately excluded:
-- it stays fixed at 4326, independent of any one network (013_network_box.sql).
--
-- network_box (013_network_box.sql) depends on segment.geom, so it has to be
-- dropped and recreated around the ALTER -- Postgres refuses to change the
-- type of a column a view depends on, even a typmod-widening change.
DROP VIEW network_box;

ALTER TABLE segment ALTER COLUMN geom TYPE geometry(MultiLineString);
ALTER TABLE activity ALTER COLUMN geom TYPE geometry(MultiLineString);
ALTER TABLE node ALTER COLUMN geom TYPE geometry(Point);
ALTER TABLE route_edge ALTER COLUMN geom TYPE geometry(LineString);
ALTER TABLE route_vertex ALTER COLUMN geom TYPE geometry(Point);

CREATE VIEW network_box AS
SELECT s.network_id, n.srid,
       ST_Transform(ST_SetSRID(ST_Extent(s.geom)::geometry, n.srid), 4326) AS box_4326
FROM segment s
JOIN network n ON n.id = s.network_id
GROUP BY s.network_id, n.srid;

-- A query that forgets to filter by network_id and mixes two networks'
-- geometry now fails loud (PostGIS already hard-errors on ST_Distance/
-- ST_DWithin between mismatched SRIDs), instead of silently storing the
-- wrong network's geometry in the first place.
--
-- node has no network_id column (its network is only known indirectly via
-- segment_id) and is deliberately left untriggered: its geometry is always
-- computed from its segment's geometry in the same statement
-- (app/importer.py's regenerate_nodes), so a wrong SRID there is only
-- possible if the segment's own geometry was already wrong -- which this
-- trigger, on segment, already catches.
CREATE FUNCTION check_network_srid() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    expected integer;
BEGIN
    IF NEW.geom IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT srid INTO expected FROM network WHERE id = NEW.network_id;
    IF ST_SRID(NEW.geom) <> expected THEN
        RAISE EXCEPTION 'geom SRID % does not match network_id % (expected SRID %)',
            ST_SRID(NEW.geom), NEW.network_id, expected;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER check_network_srid BEFORE INSERT OR UPDATE ON segment
    FOR EACH ROW EXECUTE FUNCTION check_network_srid();
CREATE TRIGGER check_network_srid BEFORE INSERT OR UPDATE ON activity
    FOR EACH ROW EXECUTE FUNCTION check_network_srid();
CREATE TRIGGER check_network_srid BEFORE INSERT OR UPDATE ON route_edge
    FOR EACH ROW EXECUTE FUNCTION check_network_srid();
CREATE TRIGGER check_network_srid BEFORE INSERT OR UPDATE ON route_vertex
    FOR EACH ROW EXECUTE FUNCTION check_network_srid();
