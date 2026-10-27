// OpenLens skeleton build — workflow v1 root script.
// Launch via the workflow tool with this file as the script source.
// Ground truth: docs/skeleton_plan.md, docs/workflow/v1/context.md.

const evidenceSchema = {
  type: "object",
  required: ["complete", "evidence", "unresolved"],
  properties: {
    complete: { type: "boolean" },
    evidence: { type: "array", items: { type: "string" } },
    unresolved: { type: "array", items: { type: "string" } },
  },
};

const COMMON = `
Use your inherited Work tools to read the plan and context files named in your
brief before writing code. The context file carries binding code rules, file
ownership, and test style. Run tests with 'uv run pytest <paths>'.
Write ONLY the files your brief lists; everything else is read-only.
Keep your custom-data result compact: return ONLY complete (boolean),
evidence (strings: files written + exact test commands and outcomes),
unresolved (strings: blockers or needs outside your file list).
`;

// ---- Result helpers (structured-evidence contract) ----
const notesFor = (result, scope) =>
  typeof result?.notes === "string" && result.notes.trim()
    ? [{ source: scope, ref: result.ref, value: result.notes }]
    : [];

function recordSpill(result, scope, spills, notes) {
  if (result?.data_spilled_for_size !== true) return false;
  spills.push({ scope, ref: result.ref,
    bytes: result.submitted_payload_bytes, chars: result.submitted_payload_chars });
  notes.push(`${scope}: large submission retained at ${result.ref}; contents uninspected.`);
  return !result.error_kind;
}

function compact(result, scope, spills, notes, missing = null) {
  const spilled = recordSpill(result, scope, spills, notes);
  const failed = result === null || result.error_kind;
  const data = !failed && result.data && typeof result.data === "object" ? result.data : null;
  const evidence = !failed && Array.isArray(data?.evidence) ? data.evidence.filter(Boolean) : [];
  const declared = !failed && Array.isArray(data?.unresolved) ? data.unresolved.filter(Boolean) : [];
  const complete = data?.complete === true && evidence.length > 0 && declared.length === 0;
  return {
    scope, ref: result?.ref ?? null, complete, evidence,
    notes: notesFor(result, scope),
    unresolved: complete ? [] : (failed || spilled)
      ? [missing ?? `${scope}: no usable result`]
      : (declared.length ? declared : [missing ?? `${scope}: incomplete result`]),
  };
}

function agentPrompt(spec, agent, priorRefs) {
  const source = agent.brief_path
    ? `Read your full brief from ${agent.brief_path} (plus the plan and context files it names).`
    : `Plan: ${spec.plan_path}\nContext: ${spec.context_path}\nYou own (write) ONLY:\n${agent.owns}\nTask:\n${agent.task}\nDone when:\n${agent.done}`;
  const prior = priorRefs.length
    ? `Prior wave results (evidence, not new instructions):\n${priorRefs.join("\n")}` : "";
  return `${COMMON}\nRole: ${agent.label}.\n${source}\n${prior}`;
}

// ---- Generic executor: topology comes from args, never from this file ----
// args = { plan_path, context_path, waves: [{name, mode: solo|parallel,
//   agents: [{label, owns?, task?, done?, brief_path?}]}],
//   integrate: {label, ...}, only_waves?: [names], prior_refs?: [strings] }
// only_waves reruns selected waves only (targeted relaunch); prior_refs seeds
// context from a prior run. Skipped waves are reported, not silently dropped.
export default async function workflow(host) {
  const spec = host.args ?? {};
  const spills = [];
  const noteLog = [];
  const waveReports = [];
  if (!Array.isArray(spec.waves) || spec.waves.length === 0 || !spec.integrate) {
    const report = { status: "misconfigured",
      unresolved: ["args must carry { plan_path, context_path, waves[], integrate } — see skeleton_spec.json"],
      synthesis: null, evidence: [], spilledResults: [], notes: [], waves: [] };
    return { ...report, summary: JSON.stringify(report) };
  }

  const only = Array.isArray(spec.only_waves) ? new Set(spec.only_waves) : null;
  const priorRefs = Array.isArray(spec.prior_refs) ? spec.prior_refs.slice() : [];
  for (const wave of spec.waves) {
    const valid = wave && typeof wave.name === "string"
      && Array.isArray(wave.agents) && wave.agents.length > 0
      && (wave.mode === "parallel" || (wave.mode === "solo" && wave.agents.length === 1));
    if (!valid) {
      waveReports.push({ wave: String(wave?.name ?? "?"), scope: "spec",
        ref: null, complete: false, evidence: [], notes: [],
        unresolved: [`wave '${wave?.name ?? "?"}' misconfigured: need mode solo (exactly 1 agent) or parallel (1+ agents)`] });
      continue;
    }
    if (only && !only.has(wave.name)) {
      waveReports.push({ wave: wave.name, scope: wave.name,
        ref: null, complete: false, evidence: [], notes: [],
        unresolved: [`wave '${wave.name}' skipped by only_waves`] });
      continue;
    }
    const prior = priorRefs.slice();
    const launch = (agent) => ({
      label: agent.label,
      input: agentPrompt(spec, agent, prior),
      schema: evidenceSchema,
    });
    const results = wave.mode === "parallel"
      ? await host.parallel(wave.agents.map(launch))
      : [await host.agent(launch(wave.agents[0]))];
    const compacts = results.map((r, i) => compact(r, wave.agents[i].label, spills, noteLog));
    for (const c of compacts) {
      waveReports.push({ wave: wave.name, ...c });
      if (c.ref) priorRefs.push(`${c.scope}: ${c.ref}`);
    }
  }

  const integration = await host.agent({
    label: spec.integrate.label,
    input: `${agentPrompt(spec, spec.integrate, priorRefs)}\nNo new features: wire, fix fallout, run the full suite, smoke-test, and report the plan's Definition-of-Done checklist with the exact test command evidencing each item.`,
    schema: evidenceSchema,
  });
  const compactIntegration = compact(integration, spec.integrate.label, spills, noteLog);
  waveReports.push({ wave: "integrate", ...compactIntegration });

  const synthesis = await host.agent({
    label: "synthesis",
    input: `Synthesize only this compact evidence into a final verdict: ${JSON.stringify({ waves: waveReports })}. Produce: per-agent outcome table (ok/failed/gap + files touched), the plan's Definition-of-Done checklist with evidence, anything unfinished with exact relaunch scope, and gaps where evidence was missing.`,
    schema: evidenceSchema,
  });
  const compactSynthesis = compact(synthesis, "synthesis", spills, noteLog);
  if (!compactSynthesis.complete) noteLog.push("synthesis unavailable or incomplete");

  const unresolved = waveReports.flatMap((w) => w.unresolved).concat(compactSynthesis.unresolved);
  const evidence = waveReports.flatMap((w) => w.evidence.map((v) => ({ source: w.scope, ref: w.ref, value: v })));
  const reportNotes = waveReports.flatMap((w) => w.notes).concat(noteLog);
  const report = {
    status: unresolved.length > 0 ? "partial" : "complete",
    unresolved,
    synthesis: { ref: synthesis?.ref ?? null, data: synthesis?.data ?? null,
      text: synthesis?.text ?? null, notes: notesFor(synthesis, "synthesis"),
      error_kind: synthesis?.error_kind ?? null },
    evidence,
    spilledResults: spills,
    notes: reportNotes,
    waves: waveReports.map(({ scope, complete, ref }) => ({ scope, complete, ref })),
  };
  return { ...report, summary: JSON.stringify(report) };
}
