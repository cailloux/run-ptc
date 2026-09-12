-- The city layer signature as of the last successful import. The nightly
-- refresh compares the live signature against this and imports only when it
-- differs. Written only after an import succeeds, so a failed night leaves
-- the old row and the next night retries.
CREATE TABLE source_signature (
    layer         text PRIMARY KEY CHECK (layer IN ('cartpath', 'road')),
    feature_count integer NOT NULL,
    max_oid       bigint NOT NULL,
    max_edited_at timestamptz,
    imported_at   timestamptz NOT NULL DEFAULT now()
);
