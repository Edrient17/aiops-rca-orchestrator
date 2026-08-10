import { readdir, readFile } from "node:fs/promises";
import { basename, resolve } from "node:path";
import { reportTemplateFileSchema } from "./templates.js";
import type { ReportTemplateFile, RequestRepository } from "./types.js";

/**
 * Makes the templates directory the truth about which reports exist.
 *
 * The registry is a database table, which made what production actually ran
 * invisible: reading a row required a token and a shell on the host, and a
 * template tuned over a week lived nowhere else -- rebuild the database and it
 * came back as whatever the seed said, with no diff to notice. Files are the
 * thing an operator can read, review and put in a commit, so the files decide
 * and the table follows them.
 *
 * Runs at ingress start, which means every deploy. A template is added by
 * adding a file.
 */
export interface TemplateSyncResult {
  created: string[];
  updated: string[];
  unchanged: string[];
  removed: string[];
  skippedRemoval: boolean;
}

export interface TemplateSyncOptions {
  repository: RequestRepository;
  directory: string;
  log?: (event: string, fields: Record<string, unknown>) => void;
}

export async function syncTemplates(
  options: TemplateSyncOptions,
): Promise<TemplateSyncResult> {
  const files = await readTemplateFiles(options.directory);
  const result: TemplateSyncResult = {
    created: [],
    updated: [],
    unchanged: [],
    removed: [],
    skippedRemoval: false,
  };

  for (const { template_id, ...body } of files) {
    const saved = await options.repository.saveTemplate(template_id, body);
    if (saved.created) result.created.push(template_id);
    else if (saved.changed) result.updated.push(template_id);
    else result.unchanged.push(template_id);
  }

  // Reading no files is not the same as declaring no templates. An unmounted
  // volume and an empty directory look identical from here, and one of them
  // would take the whole registry with it -- including the fallback every
  // unclassified question lands on. Removal only happens once the directory has
  // said something.
  if (files.length === 0) {
    result.skippedRemoval = true;
    options.log?.("template_sync_no_files", { directory: options.directory });
    return result;
  }

  const declared = new Set(files.map((file) => file.template_id));
  for (const existing of await options.repository.listTemplates(true)) {
    if (declared.has(existing.template_id)) {
      continue;
    }
    // The row goes; aiops_report_template_versions keeps every version it ever
    // had, so a report published under it can still be explained.
    await options.repository.deleteTemplate(existing.template_id);
    result.removed.push(existing.template_id);
  }

  return result;
}

/**
 * Reads and validates every template in the directory before any of them is
 * written, so one malformed file cannot leave the registry half updated.
 */
async function readTemplateFiles(
  directory: string,
): Promise<ReportTemplateFile[]> {
  let names: string[];
  try {
    names = (await readdir(directory)).filter((name) => name.endsWith(".json"));
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    throw new Error(`Cannot read the template directory ${directory}: ${reason}`);
  }

  const files: ReportTemplateFile[] = [];
  const seen = new Map<string, string>();

  for (const name of names.sort()) {
    const path = resolve(directory, name);
    let parsed: unknown;
    try {
      parsed = JSON.parse(await readFile(path, "utf8"));
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      throw new Error(`${name} is not valid JSON: ${reason}`);
    }

    const template = reportTemplateFileSchema.safeParse(parsed);
    if (!template.success) {
      const issues = template.error.issues
        .map((issue) => `${issue.path.join(".") || "(root)"}: ${issue.message}`)
        .join("; ");
      throw new Error(`${name} is not a valid template: ${issues}`);
    }

    const clash = seen.get(template.data.template_id);
    if (clash) {
      throw new Error(
        `${name} and ${clash} both declare template_id ${template.data.template_id}`,
      );
    }
    seen.set(template.data.template_id, basename(name));
    files.push(template.data);
  }

  return files;
}
