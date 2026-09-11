-- One row per source feature from a city layer, after dedupe and sliver removal.
CREATE TABLE segment (
    id               bigserial PRIMARY KEY,
    layer            text NOT NULL CHECK (layer IN ('cartpath', 'road')),
    source_key       text NOT NULL,     -- cart path GlobalID, road OBJECTID
    source_oid       integer NOT NULL,  -- cart path OBJECTID_1, road OBJECTID
    name             text,
    seg_type         text,              -- cart path Type, road CLASS
    counted          boolean NOT NULL,
    excluded         boolean NOT NULL DEFAULT false,
    exclusion_reason text,
    length_m         double precision NOT NULL,
    source_edited_at timestamptz,
    geom_hash        text NOT NULL,     -- direction-independent, used for dedupe and refresh
    props            jsonb NOT NULL,    -- every source attribute, as imported
    geom             geometry(MultiLineString, 32616) NOT NULL,
    UNIQUE (layer, source_key)
);

CREATE INDEX segment_geom_idx ON segment USING gist (geom);

-- Duplicate features dropped on import, so exclusions that name them can be
-- mapped to the feature that was kept.
CREATE TABLE source_duplicate (
    layer         text NOT NULL,
    dropped_key   text NOT NULL,
    canonical_key text NOT NULL,
    PRIMARY KEY (layer, dropped_key)
);
