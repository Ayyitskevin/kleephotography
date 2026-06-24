-- Idempotent backfill when the public marketing site has no portfolio-starred assets.
-- Python bootstrap.ensure_public_showcase() mirrors this on startup; the migration
-- covers existing deployments that restart before the new code lands.

UPDATE assets SET portfolio=1, portfolio_tag='dishes'  WHERE id=6  AND portfolio=0;
UPDATE assets SET portfolio=1, portfolio_tag='drinks'  WHERE id=7  AND portfolio=0;
UPDATE assets SET portfolio=1, portfolio_tag='pastry'  WHERE id=8  AND portfolio=0;
UPDATE assets SET portfolio=1, portfolio_tag='interiors' WHERE id=9 AND portfolio=0;
UPDATE assets SET portfolio=1, portfolio_tag='dishes'  WHERE id=10 AND portfolio=0;
UPDATE assets SET portfolio=1, portfolio_tag='drinks'  WHERE id=11 AND portfolio=0;
UPDATE assets SET portfolio=1, portfolio_tag='motion' WHERE kind='video' AND status='ready' AND portfolio=0;

UPDATE galleries
SET cs_published=1,
    cs_tagline='A tasting menu, shot at its peak.',
    cs_brief='A full menu refresh and brand library in a single service window — plating, pours, and the dining room, delivered as a same-week gallery with social crops baked in.',
    cs_credits='Client: Mise Demo
Scope: Menu refresh · brand library
Deliverables: 6 finals · social crop pack
Turnaround: Same-week gallery',
    cs_location='Asheville, NC'
WHERE id=1 AND cs_published=0;

INSERT INTO testimonials (quote, attribution_name, business, gallery_id, position, published)
SELECT
  'Our reservations jumped the week the new photos went live. Kevin made the food look exactly like the room feels.',
  'Maria Solis',
  'Cúrate',
  (SELECT id FROM galleries WHERE id=1),
  0,
  1
WHERE NOT EXISTS (SELECT 1 FROM testimonials WHERE published=1);

INSERT INTO testimonials (quote, attribution_name, business, gallery_id, position, published)
SELECT
  'Fastest turnaround we have ever had, and the social crops mean our marketing person stopped re-cropping everything by hand.',
  'Dev Carter', 'High Five Coffee', NULL, 1, 1
WHERE (SELECT COUNT(*) FROM testimonials WHERE published=1) = 1;

INSERT INTO testimonials (quote, attribution_name, business, gallery_id, position, published)
SELECT
  'He shot a full menu refresh between lunch and dinner service without ever getting in the way. Rare.',
  'Jamie Booth', 'Bull & Beggar', NULL, 2, 1
WHERE (SELECT COUNT(*) FROM testimonials WHERE published=1) = 2;