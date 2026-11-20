-- DRAFT ONLY. Registers a dedicated CSV producer for WARD-TEST only.
-- The Ward is checked before insert; no Ward or index is created here.

BEGIN;

DO $migration$
DECLARE
    target_ward_id TEXT := 'WARD-TEST';
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM ward_twin.wards
        WHERE ward_id = target_ward_id
    ) THEN
        RAISE EXCEPTION 'Approved Ward % does not exist', target_ward_id;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM ward_twin.producers
        WHERE ward_id = target_ward_id
          AND type = 'csv'
          AND producer_id = 'csv-import:' || target_ward_id
          AND active = TRUE
          AND key_hash IS NULL
    ) THEN
        NULL;
    ELSIF EXISTS (
        SELECT 1
        FROM ward_twin.producers
        WHERE ward_id = target_ward_id
          AND type = 'csv'
    ) OR EXISTS (
        SELECT 1
        FROM ward_twin.producers
        WHERE producer_id = 'csv-import:' || target_ward_id
    ) THEN
        RAISE EXCEPTION
            'Conflicting CSV producer registration exists for Ward %',
            target_ward_id;
    ELSE
        INSERT INTO ward_twin.producers (
            producer_id,
            type,
            ward_id,
            key_hash,
            active
        )
        VALUES (
            'csv-import:' || target_ward_id,
            'csv',
            target_ward_id,
            NULL,
            TRUE
        );
    END IF;
END
$migration$;

COMMIT;
