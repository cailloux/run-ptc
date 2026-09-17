-- Same problem as route_edge/route_vertex (014): source_signature's key is
-- just "layer", which isn't unique across networks. Two networks both
-- importing a layer named "cartpath" would collide on the same row.
ALTER TABLE source_signature ALTER COLUMN network_id SET NOT NULL;
ALTER TABLE source_signature DROP CONSTRAINT source_signature_pkey, ADD PRIMARY KEY (network_id, layer);
