-- 045_seed_videographer_feedback.sql
-- Seed the Videographer Feedback Questionnaire (post-project video feedback) plus
-- two cover emails that invite the client to fill it out. Idempotent: each INSERT
-- is guarded by WHERE NOT EXISTS so re-running is a no-op. The questionnaire uses
-- the new 'checkbox' ftype (added in 044) for the multi-select "what we did well".

-- ── Form ───────────────────────────────────────────────────────────────────
INSERT INTO forms (slug, title, kind, intro, active)
SELECT 'video-feedback',
       'Videographer Feedback Questionnaire',
       'questionnaire',
       'Thank you for working with us. Your honest feedback takes about two minutes and helps me sharpen the next film. Nothing here is required except the starred questions — say as much or as little as you like.',
       1
WHERE NOT EXISTS (SELECT 1 FROM forms WHERE slug = 'video-feedback');

-- ── Fields ─────────────────────────────────────────────────────────────────
INSERT INTO form_fields (form_id, label, ftype, required, options, sort_order)
SELECT f.id, 'How do you feel about the final video overall?', 'dropdown', 1,
       '["Loved it","Happy with it","It was fine","Not what I expected"]', 1
FROM forms f WHERE f.slug = 'video-feedback'
  AND NOT EXISTS (SELECT 1 FROM form_fields ff WHERE ff.form_id = f.id AND ff.sort_order = 1);

INSERT INTO form_fields (form_id, label, ftype, required, options, sort_order)
SELECT f.id, 'First impressions — what stood out when you watched it?', 'long_text', 0,
       NULL, 2
FROM forms f WHERE f.slug = 'video-feedback'
  AND NOT EXISTS (SELECT 1 FROM form_fields ff WHERE ff.form_id = f.id AND ff.sort_order = 2);

INSERT INTO form_fields (form_id, label, ftype, required, options, sort_order)
SELECT f.id, 'How well did the final film match your brand and vision?', 'dropdown', 1,
       '["Exactly what I wanted","Mostly there","Somewhat","Missed the mark"]', 3
FROM forms f WHERE f.slug = 'video-feedback'
  AND NOT EXISTS (SELECT 1 FROM form_fields ff WHERE ff.form_id = f.id AND ff.sort_order = 3);

INSERT INTO form_fields (form_id, label, ftype, required, options, sort_order)
SELECT f.id, 'What did we do well? (select all that apply)', 'checkbox', 0,
       '["Communication","Creative direction","Final edit quality","Turnaround time","On-set professionalism","Capturing the food & product"]', 4
FROM forms f WHERE f.slug = 'video-feedback'
  AND NOT EXISTS (SELECT 1 FROM form_fields ff WHERE ff.form_id = f.id AND ff.sort_order = 4);

INSERT INTO form_fields (form_id, label, ftype, required, options, sort_order)
SELECT f.id, 'Anything we could have done better?', 'long_text', 0,
       NULL, 5
FROM forms f WHERE f.slug = 'video-feedback'
  AND NOT EXISTS (SELECT 1 FROM form_fields ff WHERE ff.form_id = f.id AND ff.sort_order = 5);

INSERT INTO form_fields (form_id, label, ftype, required, options, sort_order)
SELECT f.id, 'How was communication and responsiveness throughout the project?', 'dropdown', 0,
       '["Excellent","Good","Okay","Needs work"]', 6
FROM forms f WHERE f.slug = 'video-feedback'
  AND NOT EXISTS (SELECT 1 FROM form_fields ff WHERE ff.form_id = f.id AND ff.sort_order = 6);

INSERT INTO form_fields (form_id, label, ftype, required, options, sort_order)
SELECT f.id, 'Would you work with us again or recommend us?', 'yesno', 1,
       NULL, 7
FROM forms f WHERE f.slug = 'video-feedback'
  AND NOT EXISTS (SELECT 1 FROM form_fields ff WHERE ff.form_id = f.id AND ff.sort_order = 7);

INSERT INTO form_fields (form_id, label, ftype, required, options, sort_order)
SELECT f.id, 'May we share your feedback as a testimonial?', 'yesno', 0,
       NULL, 8
FROM forms f WHERE f.slug = 'video-feedback'
  AND NOT EXISTS (SELECT 1 FROM form_fields ff WHERE ff.form_id = f.id AND ff.sort_order = 8);

-- ── Cover emails ─────────────────────────────────────────────────────────────
INSERT INTO email_templates (name, subject, body)
SELECT 'Video feedback — your film is ready',
       'Your film is ready — and a quick favor',
       'Hi {first_name},

Your final film is ready — I''ve just sent it over and I genuinely loved how this one came together. Thank you for trusting me with [project / brand].

Once you''ve had a chance to watch it, would you spend two minutes telling me how it landed? It''s a short feedback form, and your honest take is the single best way for me to keep raising the bar on every shoot:

[feedback link]

Thank you again for working with me — it was a pleasure.

Best,
{site_name}'
WHERE NOT EXISTS (SELECT 1 FROM email_templates WHERE name = 'Video feedback — your film is ready' AND deleted_at IS NULL);

INSERT INTO email_templates (name, subject, body)
SELECT 'Video feedback — gentle reminder (3-day)',
       'Following up — your thoughts on the video?',
       'Hi {first_name},

Just a gentle nudge on this — no pressure at all. If you have a couple of minutes, I''d love to hear how the film landed for you and whether there''s anything I could have done better:

[feedback link]

Your feedback genuinely shapes how I approach the next project, so it means a lot. And if anything about the delivery needs adjusting, just reply here and I''ll take care of it.

Thanks so much,
{site_name}'
WHERE NOT EXISTS (SELECT 1 FROM email_templates WHERE name = 'Video feedback — gentle reminder (3-day)' AND deleted_at IS NULL);
