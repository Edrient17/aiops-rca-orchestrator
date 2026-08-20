Zabbix가 모르는 호스트 이름을 다른 증거원에서 찾는다.

unresolved의 각 이름에 대해, tool_catalog에서 그 이름이 등장할 만한 도구를 하나
골라 tool_name과 arguments_json으로 조회한다. 로그 색인은 host.name 같은 필드에,
에이전트 목록은 name 필드에 호스트 이름을 담는다.

attempts에 이전 조회와 그 응답이 들어 있다. 거기서 호스트 이름을 읽어낼 수 있으면
hosts에 담는다. host는 응답에 나온 그대로 적는다. host_id는 응답이 Zabbix의 숫자
id를 실제로 담고 있을 때만 적고, 아니면 null로 둔다 — 이름을 id 자리에 넣지 않는다.
found_by는 그 이름을 준 도구 이름이다.

더 찾을 곳이 없거나 이미 다 찾았으면 tool_name을 null로 두고 stop_reason을 적는다.
카탈로그에 없는 도구는 지목하지 않는다. 출력은 요청된 스키마만 따른다.
