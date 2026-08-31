/**
 * A finished report as the Slack message an operator reads.
 *
 * Ported from the `Format LangGraph RCA` Code node, which was the only part of
 * the n8n workflow doing work rather than relaying HTTP. Every comment below
 * describes something that went wrong in a real report; they are carried across
 * because the reasons did not stop being true when the language changed.
 *
 * The environment is passed in rather than read here, so the same input always
 * produces the same output and the port can be held against the markdown n8n
 * already wrote for a hundred and seventeen real reports.
 */

export interface FormatConfig {
  // `| undefined` spelled out because the project sets exactOptionalPropertyTypes:
  // a caller passing through an unset environment variable is the ordinary case.
  zabbixFrontendUrl?: string | null | undefined;
  kibanaUrl?: string | null | undefined;
  kibanaDataViewId?: string | null | undefined;
}

export interface TemplateSectionSpec {
  id: string;
  heading?: string;
  required?: boolean;
  requires_problem_event?: boolean;
}

export interface ReportItemLike {
  text?: string;
  label?: string | null;
  evidence_refs?: string[];
  counter_evidence_refs?: string[];
}

export interface ReportSectionLike {
  id: string;
  body?: string | null;
  items?: ReportItemLike[];
}

export interface EvidenceLike {
  evidence_id?: string;
  window?: { from?: string; to?: string } | null;
  resource_ids?: {
    host_id?: string | null;
    event_id?: string | null;
    trigger_id?: string | null;
    item_id?: string | null;
  } | null;
  search_query?: string | null;
}

export interface FormatInput {
  request: { request_id: string; user_id?: string | null };
  template: { output?: { sections?: TemplateSectionSpec[] } | null };
  evidencePackage: {
    evidence?: EvidenceLike[];
    query_context?:
    | { hosts?: { host?: string; host_id?: string | null }[] }
    | null;
  };
  report: { title?: string; sections?: ReportSectionLike[] };
}

export interface FormattedReport {
  slackMarkdown: string;
  evidenceRefCount: number;
}

/** Slack rejects a longer message; the cut is the last thing that happens. */
const MAX_SLACK_CHARS = 39_000;

/** Past this many the header lists a few and counts the rest. */
const MAX_LISTED_HOSTS = 8;

const trimSlashes = (value: string | null | undefined): string =>
  (value ?? "").replace(/\/+$/, "");

const escapeRe = (value: string): string =>
  value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/**
 * A value going inside Rison's single-quoted string.
 *
 * Rison escapes with `!`, so a bang doubles and a quote is `!'`. Without this
 * a host name carrying an apostrophe would close the string early and the
 * whole `_a` parameter would fail to parse -- the same class of breakage as
 * putting ES|QL in a KQL field, arriving through the value instead of the
 * language.
 */
const rison = (value: string): string =>
  value.replace(/!/g, "!!").replace(/'/g, "!'");

/**
 * Whether a stored query can be handed to Discover as KQL.
 *
 * `search_query` carries whatever the collector sent, and that is not one
 * language. The reports this formatter was ported from stored KQL, and the
 * link worked. The collector sends an ES|QL statement for `esql` now, and a
 * serialised DSL body for `search` -- neither of which a `language:kuery`
 * parameter can parse, so the footnote opened on a syntax error instead of on
 * the lines it cited.
 *
 * They are told apart by how each begins, which is the part that differs: a
 * DSL body is an object, and ES|QL is a source command followed by pipes.
 * Anything else is left alone, because narrowing to the host is a real loss
 * of precision and should only happen where the alternative does not work.
 */
const isKuery = (query: string): boolean => {
  const text = query.trim();
  if (!text || text.startsWith("{") || text.startsWith("[")) return false;
  return !/^(FROM|ROW|SHOW)\b/i.test(text) && !text.includes("|");
};

/**
 * A timestamp as Zabbix's filter expects to receive it.
 *
 * `sv-SE` is not a language choice: it formats as `YYYY-MM-DD HH:mm:ss`, which
 * is the shape the frontend parses.
 */
const asTime = (value: string | undefined): string | null => {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? String(value)
    : parsed.toLocaleString("sv-SE", { timeZone: "Asia/Seoul" });
};

export function formatReport(
  input: FormatInput,
  config: FormatConfig = {},
): FormattedReport {
  const { request, template, evidencePackage: evidence, report } = input;

  // The template owns the layout: it names the sections, in order, with their
  // headings. The writer only says what goes in each, keyed by id, so a
  // reworded heading or a section the writer invented cannot change the
  // document.
  const spec = template.output?.sections ?? [];
  const filled = new Map(
    (report.sections ?? []).map((section) => [section.id, section] as const),
  );

  // A real Zabbix event id is numeric. The agent also emits synthetic ones such
  // as zbx:event:no-problem-events-... to record that it looked and found
  // nothing, and those must not license a timeline: the writer has been observed
  // copying the investigation window into incident timing on a healthy host,
  // which renders as an hour-long outage that never happened.
  const backedByEvent = (evidence.evidence ?? []).some((item) =>
    /^zbx:event:\d+$/.test(item.evidence_id ?? ""),
  );

  const refs: string[] = [];
  const refNumber = (id: string): number => {
    const seen = refs.indexOf(id);
    if (seen >= 0) return seen + 1;
    refs.push(id);
    return refs.length;
  };
  const cite = (ids: string[] | undefined): string =>
    Array.isArray(ids) && ids.length > 0
      ? " " + ids.map((id) => "[" + refNumber(id) + "]").join("")
      : "";

  // After reading the report the operator's next move is to open the graph, so
  // the footnotes carry a way back into Zabbix. Everything needed is already on
  // the evidence entry: resource_ids identifies what to open, window scopes it
  // to the interval that was actually examined.
  const evidenceById = new Map(
    (evidence.evidence ?? []).map((item) => [item.evidence_id, item] as const),
  );
  const zabbixBase = trimSlashes(config.zabbixFrontendUrl);
  const kibanaBase = trimSlashes(config.kibanaUrl);
  const kibanaDataView = config.kibanaDataViewId ?? "";

  // Footnote numbering only ever reached items[].evidence_refs, so an evidence
  // id written into prose printed raw -- a summary paragraph interrupted by
  // zbx:metric:55052:1785855600-1785942000-1h stops being readable. The writer
  // is told not to, but that is a request; this makes it not matter. Converted
  // rather than deleted, so the citation survives as the marker it should have
  // been, and only ids the investigation actually produced are touched.
  const knownIds = [...evidenceById.keys()]
    .filter((id): id is string => Boolean(id))
    .sort((left, right) => right.length - left.length);
  const idPattern =
    knownIds.length > 0
      ? new RegExp(knownIds.map(escapeRe).join("|"), "g")
      : null;
  const citeInline = (text: string): string => {
    if (!text || !idPattern) return text;
    // replace() walks left to right, so markers number in reading order.
    return (
      text
        .replace(idPattern, (id) => "[" + refNumber(id) + "]")
        // A bracket that held nothing but ids now holds nothing but markers.
        .replace(/\(\s*((?:\[\d+\]\s*[,;]?\s*)+)\)/g, (_, marks: string) =>
          marks.replace(/[\s,;]+/g, ""),
        )
        .replace(/\s+([.,)])/g, "$1")
    );
  };

  // Which host each piece of evidence belongs to. resource_ids carries the
  // Zabbix id, and Kibana has never heard of that -- the name is what the log
  // index stores, and query_context is where the two were put side by side.
  // Only hosts Zabbix knows can be found this way: evidence identifies its
  // host by Zabbix id and a host discovered in a log search has none. Entries
  // with no id are left out rather than keyed on null, or every such evidence
  // would look up the same wrong name.
  const hostNameById = new Map(
    (evidence.query_context?.hosts ?? [])
      .filter((entry) => Boolean(entry.host_id))
      .map((entry) => [entry.host_id, entry.host] as const),
  );

  // A log citation quotes a line out of a window. The link opens Discover on
  // that window, for that host, so the reader lands where the quote came from
  // rather than at the top of the index.
  //
  // Both settings are required because neither works alone: without the data
  // view id Discover opens with no index selected, and the id means nothing
  // without the address it lives at. Absent either, log footnotes simply carry
  // no link, which is what they did before.
  const kibanaLink = (id: string): { url: string; label: string } | null => {
    if (!kibanaBase || !kibanaDataView) return null;
    const item = evidenceById.get(id);
    const window = item?.window ?? {};
    if (!window.from || !window.to) return null;

    const host = hostNameById.get(item?.resource_ids?.host_id ?? undefined);
    // The search the evidence came from, when it is a language this parameter
    // can read. Falling back to the host alone opens everything that host
    // logged in the window, which is a different thing from what was cited --
    // but it resolves, and a query in the wrong language does not.
    const stored = item?.search_query;
    const q = "'";
    const query =
      stored && isKuery(stored)
        ? stored
        : host
          ? 'host.name:"' + host + '"'
          : "*";
    const time =
      "(time:(from:" + q + rison(window.from) + q +
      ",to:" + q + rison(window.to) + q + "))";
    const app =
      "(index:" + q + rison(kibanaDataView) + q +
      ",query:(language:kuery,query:" + q + rison(query) + q + "))";

    return {
      url:
        kibanaBase + "/app/discover#/?_g=" + encodeURIComponent(time) +
        "&_a=" + encodeURIComponent(app),
      label: "로그",
    };
  };

  const zabbixLink = (id: string): { url: string; label: string } | null => {
    if (!zabbixBase) return null;
    // Only Zabbix ids get Zabbix links, stated as a whitelist rather than a list
    // of exclusions. Evidence from anywhere else has no Zabbix object behind it,
    // but it usually carries a host_id, so the fallback at the bottom would
    // happily produce a "최근 데이터" link -- sending the reader to metrics under
    // a citation that quoted an audit record or a log line. When a new source is
    // added, its footnote should degrade to a bare id, not to a wrong link.
    if (!id.startsWith("zbx:")) return null;
    const item = evidenceById.get(id);
    const ids = item?.resource_ids ?? {};
    const window = item?.window ?? {};

    let range = "";
    const from = asTime(window.from);
    const to = asTime(window.to);
    if (from && to) {
      range = "&from=" + encodeURIComponent(from) + "&to=" + encodeURIComponent(to);
    }

    // Follow what the evidence actually is, so the link matches the id beside
    // it. An event footnote that opened a metric graph would send the reader
    // somewhere the citation never claimed.
    const eventLink =
      ids.event_id && ids.trigger_id
        ? {
            url:
              zabbixBase + "/tr_events.php?triggerid=" + ids.trigger_id +
              "&eventid=" + ids.event_id,
            label: "이벤트",
          }
        : null;
    const graphLink = ids.item_id
      ? {
          url:
            zabbixBase + "/history.php?action=showgraph&itemids%5B%5D=" +
            ids.item_id + range,
          label: "그래프",
        }
      : null;

    if (id.startsWith("zbx:event:") || id.startsWith("zbx:trigger:")) {
      if (eventLink) return eventLink;
      if (graphLink) return graphLink;
    } else if (graphLink) {
      return graphLink;
    }

    if (ids.host_id) {
      return {
        url:
          zabbixBase + "/zabbix.php?action=latest.view&filter_hostids%5B%5D=" +
          ids.host_id + "&filter_set=1",
        label: "최근 데이터",
      };
    }
    return null;
  };

  /**
   * The writer's own tag, rendered once.
   *
   * Uppercasing did nothing to a Korean tag and shouted an English one, which
   * is how `observed_failure` came out as *[OBSERVED_FAILURE]* beside a line
   * about a peak in log volume. The label is rendered as written.
   *
   * A text that opens with its own bracket is the same convention twice, and
   * a reader got `[OBSERVED_FAILURE] [변동 구간] 전체 로그 최고치는...`. The
   * prompt says to write the tag once; this makes a line that ignores it read
   * as though it had.
   */
  const withoutLeadingTag = (text: string): string =>
    text.replace(/^\s*\[[^\]\n]{1,40}\]\s*/, "");

  const renderItem = (item: ReportItemLike): string => {
    const label = item.label ? "*[" + String(item.label) + "]* " : "";
    const text = item.label
      ? withoutLeadingTag(item.text ?? "")
      : (item.text ?? "");
    const lines = ["• " + label + citeInline(text) + cite(item.evidence_refs)];
    const against = cite(item.counter_evidence_refs);
    if (against) lines.push("    ↳ 반박" + against);
    return lines.join("\n");
  };

  // The hosts come from what the investigation resolved rather than from the
  // report, so the header cannot claim coverage the evidence does not show.
  const hosts = (evidence.query_context?.hosts ?? [])
    .map((entry) => entry.host)
    .filter((host): host is string => Boolean(host));
  const hostLabel =
    hosts.length === 0
      ? "확인 불가"
      : hosts.length <= MAX_LISTED_HOSTS
        ? hosts.join(", ")
        : hosts.slice(0, MAX_LISTED_HOSTS).join(", ") +
          " 외 " + (hosts.length - MAX_LISTED_HOSTS) + "대";

  const meta = ["• 요청 ID: `" + request.request_id + "`"];
  if (request.user_id) meta.push("• 요청자: <@" + request.user_id + ">");
  meta.push("• 호스트: " + hostLabel);

  const sections = ["📋 *" + report.title + "*", meta.join("\n")];

  for (const declared of spec) {
    if (declared.requires_problem_event && !backedByEvent) continue;

    const section = filled.get(declared.id);
    const body = section?.body ? citeInline(String(section.body).trim()) : "";
    const items = section?.items ?? [];
    if (!body && items.length === 0) {
      // A section the template insists on is reported as empty rather than
      // dropped, so its absence reads as a finding instead of an oversight.
      if (declared.required) {
        sections.push("*" + declared.heading + "*\n• _해당 없음_");
      }
      continue;
    }

    const parts: string[] = [];
    if (body) parts.push(body);
    if (items.length > 0) parts.push(items.map(renderItem).join("\n"));
    sections.push("*" + declared.heading + "*\n" + parts.join("\n\n"));
  }

  // Built last so every marker above has already been numbered.
  if (refs.length > 0) {
    sections.push(
      "*근거*\n" +
        refs
          .map((id, index) => {
            // The query goes into the link, not onto the page. A collector that
            // names a dozen services produces a KQL string longer than the
            // finding it supports, and the citation list stops being readable.
            // Clicking still arrives at the same lines, which is what the query
            // was for.
            const link = id.startsWith("log:") ? kibanaLink(id) : zabbixLink(id);
            const marker = "`[" + (index + 1) + "]`";
            // The id is the collector's own bookkeeping and says nothing to a
            // reader that the number and the link do not. It stays only when
            // there is no link, so the line still identifies what it cites.
            return link
              ? marker + " <" + link.url + "|" + link.label + ">"
              : marker + " `" + id + "`";
          })
          .join("\n"),
    );
  }

  return {
    slackMarkdown: sections.join("\n\n").slice(0, MAX_SLACK_CHARS),
    evidenceRefCount: refs.length,
  };
}
