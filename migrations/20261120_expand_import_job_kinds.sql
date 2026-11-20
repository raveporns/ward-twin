BEGIN;

ALTER TABLE ward_twin.import_jobs
    DROP CONSTRAINT IF EXISTS import_jobs_kind_check;

ALTER TABLE ward_twin.import_jobs
    ADD CONSTRAINT import_jobs_kind_check
    CHECK (kind IN ('encounters', 'observations', 'outcomes'));

COMMIT;
