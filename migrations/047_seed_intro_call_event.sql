-- Public booking: 15-min intro call (Calendly-style /book funnel).
-- Idempotent — safe on re-migrate after partial apply.

INSERT OR IGNORE INTO event_types (
  slug, name, description, duration_min, location, color,
  min_notice_hours, booking_window_days, active, position
) VALUES (
  'intro-call',
  '15-min intro call',
  'A quick call to talk through your menu, goals, and timing — no pressure, no hard sell.',
  15,
  'Google Meet',
  '#b3552e',
  12,
  60,
  1,
  0
);

-- Mon–Fri 9:00–17:00 business-local (minutes from midnight).
INSERT OR IGNORE INTO availability_rules (event_type_id, weekday, start_min, end_min)
SELECT e.id, d.wd, 540, 1020
FROM event_types e
CROSS JOIN (
  SELECT 0 AS wd UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4
) AS d
WHERE e.slug = 'intro-call'
  AND NOT EXISTS (
    SELECT 1 FROM availability_rules ar
    WHERE ar.event_type_id = e.id AND ar.weekday = d.wd
  );