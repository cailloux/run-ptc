-- Nodes are generated per part of a counted segment and never span parts.
CREATE TABLE node (
    id              bigserial PRIMARY KEY,
    segment_id      bigint NOT NULL REFERENCES segment (id) ON DELETE CASCADE,
    part_idx        integer NOT NULL,  -- 0-based index into segment.geom
    seq             integer NOT NULL,  -- 0-based position along the part
    radius_m        real,              -- set in Phase 3
    hit_activity_id bigint,
    hit_at          timestamptz,
    geom            geometry(Point, 32616) NOT NULL,
    UNIQUE (segment_id, part_idx, seq)
);

CREATE INDEX node_geom_idx ON node USING gist (geom);
