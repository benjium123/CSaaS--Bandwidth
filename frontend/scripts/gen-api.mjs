/**
 * Regenerate openapi.json + src/api/types.gen.ts.
 *
 * Picks an interpreter that actually has the backend installed: $PYTHON if set, the repo
 * venv if present (the local dev reality), otherwise plain `python` (CI, where the backend
 * is pip-installed into the job's interpreter).
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..", "..");

const candidates = [
  process.env.PYTHON,
  path.join(repoRoot, ".venv", "Scripts", "python.exe"), // Windows venv
  path.join(repoRoot, ".venv", "bin", "python"), // POSIX venv
].filter(Boolean);

const python = candidates.find((p) => existsSync(p)) ?? "python";

function run(cmd, args) {
  const res = spawnSync(cmd, args, { stdio: "inherit", cwd: path.join(repoRoot, "frontend"), shell: false });
  if (res.status !== 0) {
    console.error(`\n${cmd} ${args.join(" ")} failed with status ${res.status}`);
    process.exit(res.status ?? 1);
  }
}

console.log(`using python: ${python}`);
run(python, [path.join(repoRoot, "backend", "scripts", "export_openapi.py")]);
// Invoke the CLI through node directly rather than npx: no shell, no .cmd shim, and
// identical behaviour on Windows and Linux.
const cli = path.join(repoRoot, "frontend", "node_modules", "openapi-typescript", "bin", "cli.js");
run(process.execPath, [cli, "openapi.json", "-o", "src/api/types.gen.ts"]);
