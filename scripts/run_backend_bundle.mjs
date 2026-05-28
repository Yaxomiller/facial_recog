import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(scriptDir, "..");
const bundleScript = path.join(scriptDir, "bundle_backend.py");

const configuredPython = process.env.ATTENDANCE_PYTHON?.trim();
const candidates = [
  configuredPython ? [configuredPython] : null,
  [path.join(rootDir, ".venv", "Scripts", "python.exe")],
  [path.join(rootDir, ".venv", "bin", "python")],
  ["python"],
  ["python3"],
  ["py", "-3"],
].filter(Boolean);

function isPathLike(command) {
  return command.includes("\\") || command.includes("/") || command.includes(":");
}

for (const candidate of candidates) {
  const [command, ...prefixArgs] = candidate;
  if (isPathLike(command) && !existsSync(command)) {
    continue;
  }

  const result = spawnSync(command, [...prefixArgs, bundleScript], {
    cwd: rootDir,
    stdio: "inherit",
    env: process.env,
  });

  if (!result.error) {
    process.exit(result.status ?? 0);
  }

  if (result.error.code !== "ENOENT") {
    throw result.error;
  }
}

console.error(
  "Could not find a Python interpreter for the desktop backend bundle. " +
    "Set ATTENDANCE_PYTHON or create the project virtual environment first.",
);
process.exit(1);
