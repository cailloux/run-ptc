CREATE TABLE network (
    id         bigserial PRIMARY KEY,
    slug       text NOT NULL UNIQUE,
    name       text NOT NULL,
    kind       text NOT NULL CHECK (kind IN ('ptc', 'osm_city', 'zwift_world')),
    srid       integer NOT NULL,
    sports     text[] NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO network (slug, name, kind, srid, sports)
VALUES ('ptc', 'Peachtree City', 'ptc', 32616, '{run}');
