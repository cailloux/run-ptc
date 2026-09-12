-- The routing graph, rebuilt from segment by `python -m app.cli graph` and
-- after every city import. A standard pgRouting edges table, so routing and
-- future route-planning functions (cost matrices, TSP, via) run on it as is.
CREATE TABLE route_vertex (
    id        bigint PRIMARY KEY,           -- from pgr_extractVertices
    component bigint,                       -- from pgr_connectedComponents
    geom      geometry(Point, 32616) NOT NULL
);

CREATE INDEX route_vertex_geom_idx ON route_vertex USING gist (geom);

CREATE TABLE route_edge (
    id           bigint PRIMARY KEY,
    source       bigint,
    target       bigint,
    cost         double precision NOT NULL,  -- length in meters
    reverse_cost double precision NOT NULL,  -- same: runners ignore OneWay
    segment_id   bigint NOT NULL REFERENCES segment (id) ON DELETE CASCADE,
    part_idx     integer NOT NULL,
    -- The stretch of the segment's part this edge covers, as fractions of the
    -- part's length. Maps nodes (part_idx, seq) onto edges.
    part_from    double precision NOT NULL,
    part_to      double precision NOT NULL,
    geom         geometry(LineString, 32616) NOT NULL
);

CREATE INDEX route_edge_geom_idx ON route_edge USING gist (geom);
CREATE INDEX route_edge_source_idx ON route_edge (source);
CREATE INDEX route_edge_target_idx ON route_edge (target);
CREATE INDEX route_edge_segment_idx ON route_edge (segment_id);
