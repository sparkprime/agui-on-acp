#!/usr/bin/env node
// Cross-platform backend launcher: sets PYTHONPATH and spawns uvicorn.
// Avoids the POSIX-only `PYTHONPATH=. uvicorn ...` syntax that breaks on Windows.

import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// Windows: --reload spawns a worker that forces WindowsSelectorEventLoopPolicy,
// which breaks asyncio.create_subprocess_exec (NotImplementedError). Drop --reload
// there. macOS/Linux are unaffected.
const reloadFlag = process.platform === "win32" ? [] : ["--reload"];

const args = [
  "backend.main:app",
  ...reloadFlag,
  "--port", "8000",
  "--log-level", "info",
  ...process.argv.slice(2),
];

const child = spawn("uvicorn", args, {
  cwd: repoRoot,
  env: { ...process.env, PYTHONPATH: repoRoot },
  stdio: "inherit",
  shell: process.platform === "win32",
});

const forward = (sig) => () => child.kill(sig);
process.on("SIGINT", forward("SIGINT"));
process.on("SIGTERM", forward("SIGTERM"));

child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 0);
});

child.on("error", (err) => {
  console.error("Failed to spawn uvicorn:", err.message);
  console.error("Is the backend installed? Run: pnpm install:backend");
  process.exit(1);
});
