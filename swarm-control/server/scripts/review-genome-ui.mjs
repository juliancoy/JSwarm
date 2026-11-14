// The research deck requires an owner snapshot rather than public demo data.
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
const script = fileURLToPath(
  new URL(
    "../../../../HarborGenetics/tests/browser/review_genome_explorer.py",
    import.meta.url,
  ),
);
if (!process.argv.includes("--snapshot")) {
  console.error(
    "Usage: node server/scripts/review-genome-ui.mjs --snapshot /private/research.json [--output /private/screenshots]",
  );
  process.exit(2);
}
const result = spawnSync(
  process.env.PYTHON || "python3",
  [script, ...process.argv.slice(2)],
  { stdio: "inherit" },
);
if (result.error) throw result.error;
process.exit(result.status ?? 1);
