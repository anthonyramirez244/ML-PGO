import { query, type ClaudeAgentOptions } from "@anthropic-ai/claude-agent-sdk";
import { execFileSync } from "child_process";
import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const args = process.argv.slice(2);
const advicePath = args.find((a) => !a.startsWith("--"));
const outFlagIndex = args.indexOf("--out");
const topFlagIndex = args.indexOf("--top");
const outPath = outFlagIndex !== -1 ? args[outFlagIndex + 1] : undefined;
const topN = topFlagIndex !== -1 ? parseInt(args[topFlagIndex + 1], 10) : 10;

if (!advicePath) {
  console.error('Usage: npx tsx advisor.ts <path-to-gpa.advice> [--out report.md] [--top N]');
  process.exit(1);
}

const resolvedAdvicePath = path.resolve(advicePath);
if (!fs.existsSync(resolvedAdvicePath)) {
  console.error(`No such file: ${resolvedAdvicePath}`);
  process.exit(1);
}

const reportPath = outPath
  ? path.resolve(outPath)
  : path.join(path.dirname(resolvedAdvicePath), "gpa-advisor-report.md");

function parseAdvice(advicePath: string, top: number) {
  const parserPath = path.join(__dirname, "parse_advice.py");
  const raw = execFileSync("python3", [parserPath, advicePath, "--top", String(top)], {
    encoding: "utf-8",
    maxBuffer: 20 * 1024 * 1024,
  });
  return JSON.parse(raw);
}

const findings = parseAdvice(resolvedAdvicePath, topN);

if (findings.findings.length === 0) {
  console.log(`No actionable findings (ratio > 0) in ${resolvedAdvicePath}. Nothing to report.`);
  process.exit(0);
}

const PROMPT = `
You are Overclock, a GPU performance tuning advisor. You turn raw GPA (GPU Performance Advisor)
profiler output into a short, prioritized, human-readable report a CUDA developer can act on.

You have been given ${findings.findings.length} findings extracted from a gpa.advice file, already
deduplicated and ranked by estimated impact (ratio of stalled cycles attributed to this cause,
multiplied by the estimated speedup). Each finding cites real file:line locations in the CUDA
source.

Findings (JSON):
${JSON.stringify(findings.findings, null, 2)}

Your job:
1. For each finding, Read the cited source file(s) around the given line(s) to see the real code
   causing the stall. Do not guess at code content you have not read.
2. Write one report entry per finding, in this format:

   ### N. <Kernel> — <OptimizerName> (~<speedup>x, <ratio>% of stalls)

   **Where:** <file>:<line>

   **Why:** <plain-English root cause, grounded in the optimizer's description AND the actual
   code you read at that location — not generic boilerplate>

   **What to change:** <concrete suggestion referencing the real variable/line content you read>

3. Order entries by impact score, highest first (the input is already sorted this way — keep that
   order).
4. Skip any finding you cannot ground in the actual source you read.
5. Do not invent numbers. Only use the ratio/speedup values given in the input JSON.
6. Start the report with a one-paragraph summary: which kernel(s) are involved, and the single
   highest-impact fix.

Output ONLY the final Markdown report. No preamble, no meta-commentary about what you're about to
do.
`;

const { ANTHROPIC_API_KEY: _unused, ...envWithoutApiKey } = process.env;

const options: ClaudeAgentOptions = {
  model: "claude-sonnet-5",
  allowedTools: ["Read"],
  maxTurns: 15,
  env: envWithoutApiKey,
};

async function run() {
  console.log(`Overclock: analyzing ${findings.findings.length} findings from ${resolvedAdvicePath}...\n`);

  let report = "";

  for await (const message of query({ prompt: PROMPT, options })) {
    if ("type" in message && message.type === "result") {
      const result = message as { type: string; subtype: string; result?: string };
      if (result.subtype === "success" && result.result) {
        report = result.result;
      } else {
        console.error(`Overclock stopped early: ${result.subtype}`);
        process.exit(1);
      }
    }
  }

  if (!report) {
    console.error("Overclock returned no report.");
    process.exit(1);
  }

  fs.writeFileSync(reportPath, report, "utf-8");
  console.log(`Report written to ${reportPath}`);
}

run();
