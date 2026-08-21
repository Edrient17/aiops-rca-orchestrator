각 호스트에 대해, 그 창(window) 동안 무슨 일이 있었는지 보여줄 조회 하나를 고른다.

hosts의 각 항목마다 scans에 하나씩 넣는다. host는 주어진 이름 그대로 적는다.
tool_name은 tool_catalog에 있는 것만 쓰고, arguments_json은 그 도구의
input_schema를 지킨다.

host_id가 있으면 Zabbix 이벤트 조회가 대개 가장 곧바르다. host_id가 null이면
Zabbix 도구는 쓸 수 없다 — 그 호스트는 Zabbix가 모르며, host_id를 요구하는
도구는 호출이 거절된다. 그런 호스트는 이름으로 조회할 수 있는 곳에서 찾는다:
에이전트 알림 요약, 또는 로그 색인 질의.

로그 색인에서 이름으로 거를 때는 host.name이 분석되는 필드일 수 있으므로 term
대신 match를 쓰거나 keyword 하위 필드를 쓴다. 느슨한 전문 검색은 이름의 토큰이
겹치는 다른 호스트를 함께 물어온다.

조회할 것이 없으면 scans를 비우고 stop_reason을 적는다.
출력은 요청된 스키마만 따른다.
