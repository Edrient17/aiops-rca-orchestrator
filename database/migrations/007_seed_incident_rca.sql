-- The incident RCA that used to be the only report this system could write.
--
-- Its layout lived in the workflow: a fixed schema of fourteen fields and a
-- renderer that knew each one by name. Moving it into a row is what makes it
-- one kind among several rather than the shape everything must fit, and it is
-- the case the template mechanism has to reproduce exactly before any new kind
-- is worth adding.
--
-- Seeded rather than left to an operator because the workflow falls back to
-- this id when a question matches nothing else. Without the row there is no
-- fallback and every unclassified question fails.
--
-- ON CONFLICT DO NOTHING, so edits survive: the operator owns this row once it
-- exists, and re-running the migration must not undo their work. That also
-- makes it self-healing -- a row deleted by accident comes back on the next
-- deploy, with the original wording rather than whatever it had grown into.
INSERT INTO aiops_report_templates (
  template_id, version, enabled, title, description, collection, output
)
VALUES (
  'incident_rca',
  1,
  true,
  '장애 RCA 보고서',
  '특정 호스트에서 일어난 장애나 이상 징후의 원인을 조사해 달라는 요청. '
  || '기준 시각 전후를 조사한다. 정기 요약이 아니라 사건 하나를 다룰 때 고른다.',
  jsonb_build_object(
    'host_selector', jsonb_build_object('mode', 'from_question'),
    'window', jsonb_build_object('policy', 'standard', 'range', 'anchor_relative'),
    'aggregation', NULL,
    'metric_keywords', '[]'::jsonb,
    'limits', '{}'::jsonb,
    'guidance', ''
  ),
  jsonb_build_object(
    'guidance',
    '요약만 존댓말 서술체로 쓰고 나머지는 개조식으로 쓴다. '
    || '확인된 사실과 원인 후보를 섞지 않는다.',
    'sections', jsonb_build_array(
      jsonb_build_object(
        'id', 'summary', 'heading', '요약', 'required', true,
        'requires_problem_event', false,
        'instruction', '운영자가 처음 읽는 문단. 무엇이 관측됐고 무엇이 확인되지 '
          || '않았는지 존댓말 서술체로 3~5문장. body에 쓰고 items는 비운다.'),
      jsonb_build_object(
        'id', 'observed', 'heading', '관측된 형태', 'required', true,
        'requires_problem_event', false,
        'instruction', '증상을 원인이 아니라 관측된 그대로 한 항목으로 적는다.'),
      -- Split from the section above precisely so the guard can apply to the
      -- timing without also withholding the symptom, which is always known.
      jsonb_build_object(
        'id', 'incident_timing', 'heading', '장애 시각', 'required', false,
        'requires_problem_event', true,
        'instruction', '문제 이벤트에서 얻은 심각도·발생·복구·지속 시간. '
          || '조사 구간을 장애 시각으로 옮겨 적지 않는다. 이벤트에 없는 값은 '
          || '항목 자체를 만들지 않는다.'),
      jsonb_build_object(
        'id', 'impact', 'heading', '영향', 'required', false,
        'requires_problem_event', false,
        'instruction', '증거로 확인된 영향만. 확인되지 않았으면 비운다.'),
      jsonb_build_object(
        'id', 'scope', 'heading', '조사 범위', 'required', true,
        'requires_problem_event', false,
        'instruction', '실제로 조사한 호스트와 시간 구간을 한 항목으로.'),
      jsonb_build_object(
        'id', 'facts', 'heading', '확인된 사실', 'required', true,
        'requires_problem_event', false,
        'instruction', '증거가 뒷받침하는 사실만. 항목마다 evidence_refs 필수. '
          || '호스트가 여럿이면 어느 호스트인지 문장에 밝힌다.'),
      jsonb_build_object(
        'id', 'timeline', 'heading', '타임라인', 'required', false,
        'requires_problem_event', true,
        'instruction', '시각 순서가 의미 있을 때만. 모든 항목이 같은 시각이면 '
          || '순서가 아니므로 비운다. 시각은 text 앞에 쓴다.'),
      jsonb_build_object(
        'id', 'signals', 'heading', '관련 신호', 'required', false,
        'requires_problem_event', false,
        'instruction', '장애와 시간적으로 겹치거나 앞뒤에 나타난 다른 관측. '
          || '관계를 label에 적는다(preceded/coincided/followed).'),
      jsonb_build_object(
        'id', 'candidates', 'heading', '원인 후보', 'required', true,
        'requires_problem_event', false,
        'instruction', '관측을 설명할 수 있는 가설. label에 HIGH/MEDIUM/LOW. '
          || '뒷받침은 evidence_refs, 반박은 counter_evidence_refs. '
          || '"문제 없음"은 가설이 아니므로 넣지 않는다. 없으면 비운다.'),
      jsonb_build_object(
        'id', 'recovery', 'heading', '복구', 'required', false,
        'requires_problem_event', true,
        'instruction', '증거로 확인된 복구 조치나 자동 회복.'),
      jsonb_build_object(
        'id', 'immediate_actions', 'heading', '즉시 권고', 'required', true,
        'requires_problem_event', false,
        'instruction', '지금 할 일. 수행된 사실이 아니라 제안임이 드러나게.'),
      jsonb_build_object(
        'id', 'preventive_actions', 'heading', '예방 권고', 'required', true,
        'requires_problem_event', false,
        'instruction', '재발을 막기 위한 제안.'),
      jsonb_build_object(
        'id', 'additional_data', 'heading', '추가 필요 데이터', 'required', true,
        'requires_problem_event', false,
        'instruction', 'Zabbix에 없어 확인하지 못한 것. 로그 본문, 배포 이력 등.'),
      jsonb_build_object(
        'id', 'limitations', 'heading', '분석 한계', 'required', true,
        'requires_problem_event', false,
        'instruction', 'partial, 낮은 coverage_ratio, 적은 sample_count 등 '
          || '이 결론을 얼마나 믿을 수 있는지에 영향을 주는 사정.')
    )
  )
)
ON CONFLICT (template_id) DO NOTHING;

INSERT INTO aiops_report_template_versions (
  template_id, version, enabled, title, description, collection, output
)
SELECT template_id, version, enabled, title, description, collection, output
FROM aiops_report_templates
WHERE template_id = 'incident_rca'
ON CONFLICT (template_id, version) DO NOTHING;
