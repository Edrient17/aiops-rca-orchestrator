-- Sections declare the observations they are written from.
--
-- A template used to be two independent halves: `collection` said what to
-- gather and `output` said what to write, with nothing checking that the first
-- could supply the second. A monthly capacity report ran, found a real
-- incident, spent its iterations reasoning about that, and produced a report
-- whose two capacity sections were empty. Every individual step had behaved
-- correctly; nothing had ever asked whether the report could still be written.
--
-- `requires_effects` names the tool-registry effects a section is built from.
-- The coverage sweep collects them whether or not the reasoning wants them,
-- the stop guard will not finish a run while one is still uncollected, and a
-- section whose effect could not be collected is marked so the writer states
-- the reason instead of emitting an empty heading.
--
-- Patched in per section id rather than replacing `output`, because these rows
-- are operator-owned: the wording of an instruction may have been edited since
-- it was seeded, and a wholesale replace would silently revert that work.
--
-- Idempotent by the guard below: once any section carries the key, the whole
-- statement is a no-op, so the every-start re-apply cannot keep bumping the
-- version.
UPDATE aiops_report_templates
SET
  output = jsonb_set(
    output,
    '{sections}',
    (
      SELECT jsonb_agg(
        CASE section ->> 'id'
          WHEN 'availability' THEN
            section || '{"requires_effects": ["incident_events"]}'::jsonb
          WHEN 'capacity_trend' THEN
            section || '{"requires_effects": ["metric_change"]}'::jsonb
          WHEN 'resource_pressure' THEN
            section || '{"requires_effects": ["metric_level", "metric_trend"]}'::jsonb
          -- summary, watchlist and limitations are written from whatever the
          -- investigation found. Declaring an effect for them would make a
          -- narrative section depend on one particular observation existing.
          ELSE section
        END
        ORDER BY position
      )
      FROM jsonb_array_elements(output -> 'sections')
        WITH ORDINALITY AS elements(section, position)
    )
  ),
  version = version + 1,
  updated_at = now()
WHERE template_id = 'monthly_capacity_report'
  AND NOT EXISTS (
    SELECT 1
    FROM jsonb_array_elements(output -> 'sections') AS existing(section)
    WHERE existing.section ? 'requires_effects'
  );
