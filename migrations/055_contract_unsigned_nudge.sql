-- One-shot flag so a sent-but-unsigned contract gets a single internal nudge to
-- Kevin (contract_reminders), not one every sweep. A contract only moves forward
-- (draft -> sent -> viewed -> signed) and there is no re-send path, so the flag
-- never needs resetting once set.
ALTER TABLE contracts ADD COLUMN nudged_unsigned INTEGER NOT NULL DEFAULT 0;
