ALTER TABLE segment ADD COLUMN network_id bigint REFERENCES network (id);
ALTER TABLE activity ADD COLUMN network_id bigint REFERENCES network (id);
ALTER TABLE source_signature ADD COLUMN network_id bigint REFERENCES network (id);
ALTER TABLE route_edge ADD COLUMN network_id bigint REFERENCES network (id);
ALTER TABLE route_vertex ADD COLUMN network_id bigint REFERENCES network (id);

UPDATE segment SET network_id = (SELECT id FROM network WHERE slug = 'ptc');
UPDATE activity SET network_id = (SELECT id FROM network WHERE slug = 'ptc');
UPDATE source_signature SET network_id = (SELECT id FROM network WHERE slug = 'ptc');
UPDATE route_edge SET network_id = (SELECT id FROM network WHERE slug = 'ptc');
UPDATE route_vertex SET network_id = (SELECT id FROM network WHERE slug = 'ptc');

CREATE INDEX segment_network_idx ON segment (network_id);
CREATE INDEX activity_network_idx ON activity (network_id);
CREATE INDEX source_signature_network_idx ON source_signature (network_id);
CREATE INDEX route_edge_network_idx ON route_edge (network_id);
CREATE INDEX route_vertex_network_idx ON route_vertex (network_id);
