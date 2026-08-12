#!/usr/bin/env node
/**
 * Starts the FastAPI service for Turborepo's `server#dev` task.
 *
 * Two things it takes care of that a plain npm script cannot:
 *
 *   1. Working directory. uvicorn must start in apps/api so `api:app` and the
 *      flat imports between the service's modules resolve. Repo-root paths
 *      (`.env`, `docs/`, `outputs/`) are handled on the Python side by
 *      apps/api/paths.py, which anchors on __file__ instead of the cwd.
 *
 *   2. Which Python. This repo gets developed from both WSL and Windows, and the
 *      interpreter that actually has the dependencies installed is not always the
 *      one named `python3` on PATH — under WSL it is often `python.exe`. Rather
 *      than guess from the platform, probe the candidates for an importable
 *      uvicorn and use the first that has it. Set PYTHON to skip the probe.
 */
import { spawn, spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const apiDir = resolve(repoRoot, "apps", "api");

const candidates = process.env.PYTHON
  ? [process.env.PYTHON]
  : ["python3", "python", "python.exe", "py"];

/** True when `cmd` exists and can import uvicorn. */
function hasUvicorn(cmd) {
  const probe = spawnSync(cmd, ["-c", "import uvicorn"], {
    cwd: apiDir,
    stdio: "ignore",
  });
  return probe.status === 0;
}

const python = candidates.find(hasUvicorn);

if (!python) {
  console.error(
    `\nNo se encontró un intérprete de Python con uvicorn instalado.\n` +
      `Probé: ${candidates.join(", ")}\n\n` +
      `Instala las dependencias:\n` +
      `  python3 -m pip install -r apps/api/requirements.txt\n\n` +
      `…o apunta al intérprete correcto, por ejemplo desde WSL:\n` +
      `  PYTHON=python.exe npm run dev\n`,
  );
  process.exit(1);
}

// Anything after the script name goes through to uvicorn, so `--reload`,
// `--port 8010` and friends still work.
const args = ["-m", "uvicorn", "api:app", ...process.argv.slice(2)];
console.log(`[api] ${python} ${args.join(" ")}  (cwd: ${apiDir})`);

const child = spawn(python, args, {
  cwd: apiDir,
  stdio: "inherit",
});

// Turborepo stops a persistent task by signalling this process; pass it on so
// uvicorn shuts down instead of being orphaned.
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => child.kill(signal));
}

child.on("exit", (code, signal) => {
  process.exit(signal ? 1 : (code ?? 0));
});
