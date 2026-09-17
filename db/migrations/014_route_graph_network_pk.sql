-- route_edge.id and route_vertex.id are assigned per build_graph() run (a
-- row_number()/pgr_extractVertices sequence that restarts at 1 each time),
-- not a network-wide sequence. Two networks' graphs would collide on the
-- same small ids under a plain "id" primary key, so network_id joins the key.
ALTER TABLE route_edge ALTER COLUMN network_id SET NOT NULL;
ALTER TABLE route_vertex ALTER COLUMN network_id SET NOT NULL;

ALTER TABLE route_edge DROP CONSTRAINT route_edge_pkey, ADD PRIMARY KEY (network_id, id);
ALTER TABLE route_vertex DROP CONSTRAINT route_vertex_pkey, ADD PRIMARY KEY (network_id, id);
