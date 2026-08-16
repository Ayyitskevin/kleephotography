-- Lead attribution: "how did you hear about us?" (revenue roadmap item 3).
--
-- Every marketing decision so far has been made blind — no form records where a
-- lead came from, so there is no way to know whether the next hour belongs to
-- Instagram, Google, or asking clients for referrals. One optional select on the
-- contact and booking forms, stored verbatim from a fixed answer set
-- (app/public/site_catalog.py REFERRAL_SOURCES; free text never reaches these
-- columns, so the admin rollup cannot be polluted).
--
-- On bookings as well as inquiries, because a paid booking's inquiry row is
-- created LATER (by booking_notify.confirm, possibly after a Stripe webhook) —
-- the answer must survive on the booking until then. Additive, both nullable:
-- NULL means "asked nothing or answered nothing", and every existing row simply
-- predates the question.
ALTER TABLE inquiries ADD COLUMN referral_source TEXT;
ALTER TABLE bookings  ADD COLUMN referral_source TEXT;
