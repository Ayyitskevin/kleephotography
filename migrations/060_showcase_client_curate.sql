-- Relabel demo showcase from "Mise Demo" → real client attribution on the public site.
UPDATE galleries
SET title='Seasonal Tasting Menu',
    client_name='Cúrate',
    cs_published=1,
    cs_tagline=COALESCE(NULLIF(cs_tagline, ''), 'A tasting menu, shot at its peak.'),
    cs_brief=COALESCE(NULLIF(cs_brief, ''), 'A full menu refresh and brand library in a single service window — plating, pours, and the dining room, delivered as a same-week gallery with social crops baked in.'),
    cs_credits=CASE
        WHEN cs_credits IS NULL OR cs_credits='' OR cs_credits LIKE '%Mise Demo%'
        THEN 'Client: Cúrate
Scope: Menu refresh · brand library
Deliverables: 6 finals · social crop pack
Turnaround: Same-week gallery'
        ELSE cs_credits END,
    cs_location=COALESCE(NULLIF(cs_location, ''), 'Asheville, NC')
WHERE id=1 AND (client_name IS NULL OR client_name IN ('', 'Mise Demo'));
