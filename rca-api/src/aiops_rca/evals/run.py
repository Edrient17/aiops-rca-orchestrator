"""Upload the dataset, or run the writer against it and score the result.

    python -m aiops_rca.evals.run baseline  exports.jsonl
    python -m aiops_rca.evals.run upload    exports.jsonl
    python -m aiops_rca.evals.run evaluate  exports.jsonl [--limit N]

`baseline` needs nothing but the file. `upload` and `evaluate` need
LANGSMITH_API_KEY, and `evaluate` calls the writer model once per example --
which costs money and takes minutes, so it is never the default and `--limit` is
there to try it small first.

Export the file from the orchestrator host:

    docker compose exec -T postgres psql -U aiops -d aiops -t -A -c "
      select jsonb_build_object('request_id', r.request_id, 'question', q.question,
             'parsed', r.parsed_request, 'package', r.evidence_package,
             'report', r.rca_report, 'template_output', t.output)::text
      from aiops_reports r
      join aiops_requests q using (request_id)
      left join aiops_report_templates t
        on t.template_id = r.parsed_request->>'request_type'
      order by r.created_at;" > exports.jsonl
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from aiops_rca.evals.harness import (
    DATASET_NAME,
    baseline_findings,
    evaluators,
    load_examples,
    writer_target,
)


def _load(path: Path):
    loaded = load_examples(path)
    print(f"{len(loaded)} examples loaded from {path.name}")
    for reason, count in sorted(loaded.skipped.items()):
        print(f"  skipped {count}: {reason}")
        if loaded.reasons.get(reason):
            print(f"      e.g. {loaded.reasons[reason][:200]}")
    if not loaded.examples:
        sys.exit("no usable examples")
    return loaded


def baseline(path: Path) -> None:
    loaded = _load(path)
    flagged = 0
    by_check: Counter[str] = Counter()
    for request_id, findings in baseline_findings(loaded.examples):
        if not findings:
            continue
        flagged += 1
        print(f"\n{request_id}")
        for finding in findings:
            by_check[finding.check] += 1
            print(f"  {finding.check} | {finding.section_id} | {finding.detail}")
    print(f"\n{flagged}/{len(loaded)} shipped reports carry a finding")
    for check, count in by_check.most_common():
        print(f"  {check}: {count}")


def upload(path: Path) -> None:
    from langsmith import Client

    loaded = _load(path)
    client = Client()
    if client.has_dataset(dataset_name=DATASET_NAME):
        dataset = client.read_dataset(dataset_name=DATASET_NAME)
    else:
        dataset = client.create_dataset(
            dataset_name=DATASET_NAME,
            description=(
                "Stored evidence packages this service really collected. The "
                "writer is re-run against them; the checks in evals/properties "
                "score what it writes."
            ),
        )
    existing = {
        example.inputs.get("request_id")
        for example in client.list_examples(dataset_id=dataset.id)
    }
    fresh = [item for item in loaded.examples if item.request_id not in existing]
    if not fresh:
        print(f"dataset '{DATASET_NAME}' already holds all {len(loaded)} examples")
        return
    client.create_examples(
        dataset_id=dataset.id,
        inputs=[item.as_inputs() for item in fresh],
        # No reference output. The report that shipped is not the right answer:
        # several of these are the defects the checks were written from.
        metadata=[{"request_id": item.request_id} for item in fresh],
    )
    print(f"added {len(fresh)} examples to '{DATASET_NAME}'")


def evaluate(path: Path, limit: int | None) -> None:
    from langsmith import Client

    from aiops_rca.config.settings import Settings
    from aiops_rca.services.llm import OpenAIStructuredModel
    from aiops_rca.services.tracing import configure_tracing

    _load(path)
    settings = Settings()
    model = OpenAIStructuredModel(
        api_key=settings.openai_api_key.get_secret_value(),
        base_url=settings.openai_base_url,
        timeout_seconds=settings.model_timeout_seconds,
        traced=configure_tracing(settings),
    )
    client = Client()
    data = list(client.list_examples(dataset_name=DATASET_NAME))
    if limit:
        data = data[:limit]
    print(f"running the writer over {len(data)} examples with {settings.rca_writer_model}")
    results = client.evaluate(
        writer_target(model, settings.rca_writer_model),
        data=data,
        evaluators=evaluators(),
        experiment_prefix="writer",
        metadata={"writer_model": settings.rca_writer_model},
        max_concurrency=4,
    )
    print(results)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["baseline", "upload", "evaluate"])
    parser.add_argument("path", type=Path)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    if args.command == "baseline":
        baseline(args.path)
    elif args.command == "upload":
        upload(args.path)
    else:
        evaluate(args.path, args.limit)


if __name__ == "__main__":
    main()
