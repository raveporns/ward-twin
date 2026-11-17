BEGIN;

-- Existing alerts remain unclaimed; no historical claim timestamp is inferred.
ALTER TABLE ward_twin.alerts
    ADD COLUMN IF NOT EXISTS claim_status TEXT NOT NULL
    DEFAULT 'unclaimed';

ALTER TABLE ward_twin.alerts
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'alerts_claim_state_ck'
          AND conrelid = 'ward_twin.alerts'::regclass
    ) THEN
        ALTER TABLE ward_twin.alerts
            ADD CONSTRAINT alerts_claim_state_ck
            CHECK (
                (
                    claim_status = 'unclaimed'
                    AND claimed_at IS NULL
                )
                OR (
                    claim_status = 'claimed'
                    AND assigned_to IS NOT NULL
                    AND claimed_at IS NOT NULL
                )
            );
    END IF;
END
$$;

COMMIT;
