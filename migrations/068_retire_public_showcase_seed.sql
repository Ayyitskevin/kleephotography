-- Retire prototype/invented public proof while preserving rows for audit and rollback.
-- Match exact invented-content fingerprints so genuine proof remains live.

UPDATE testimonials
SET published=0
WHERE quote IN (
    'Our reservations jumped the week the new photos went live. Kevin made the food look exactly like the room feels.',
    'Fastest turnaround we have ever had, and the social crops mean our marketing person stopped re-cropping everything by hand.',
    'He shot a full menu refresh between lunch and dinner service without ever getting in the way. Rare.'
);

UPDATE galleries
SET cs_published=0
WHERE cs_tagline='A tasting menu, shot at its peak.'
  AND cs_brief='A full menu refresh and brand library in a single service window — plating, pours, and the dining room, delivered as a same-week gallery with social crops baked in.'
  AND replace(cs_credits, char(13), '')='Client: Independent restaurant
Scope: Menu refresh · brand library
Deliverables: 6 finals · social crop pack
Turnaround: Same-week gallery';
