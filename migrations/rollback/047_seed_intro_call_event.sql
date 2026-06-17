DELETE FROM availability_rules
WHERE event_type_id IN (SELECT id FROM event_types WHERE slug = 'intro-call');

DELETE FROM event_types WHERE slug = 'intro-call';