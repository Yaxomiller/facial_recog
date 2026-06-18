import { existsSync, mkdirSync, rmSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(scriptDir, "..");
const sourceDir = path.join(rootDir, "native_backend");
const buildDir = path.join(rootDir, "build", "native_backend");
const runtimeDir = path.join(rootDir, "frontend", "src-tauri", "backend-runtime", "attendance-native-backend");
const binaryName = process.platform === "win32" ? "attendance-native-backend.exe" : "attendance-native-backend";
const binaryPath = path.join(runtimeDir, binaryName);

function run(command, args) {
  const result = spawnSync(command, args, {
    cwd: rootDir,
    stdio: "inherit",
    env: process.env,
  });

  if (result.error) {
    throw result.error;
  }

  if ((result.status ?? 0) !== 0) {
    throw new Error(`${command} ${args.join(" ")} exited with status ${result.status ?? 1}`);
  }
}

export function buildNativeBackend() {
  if (!existsSync(sourceDir)) {
    throw new Error(`Native backend source folder not found: ${sourceDir}`);
  }

  rmSync(runtimeDir, { recursive: true, force: true });
  mkdirSync(runtimeDir, { recursive: true });

  run("cmake", ["-S", sourceDir, "-B", buildDir, "-DCMAKE_BUILD_TYPE=Release"]);
  run("cmake", ["--build", buildDir, "--config", "Release"]);
  run("cmake", ["--install", buildDir, "--config", "Release", "--prefix", runtimeDir]);

  if (!existsSync(binaryPath)) {
    throw new Error(`Expected native backend executable at ${binaryPath}, but it was not created.`);
  }

  console.log(`Bundled native backend written to ${binaryPath}`);
  return binaryPath;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  buildNativeBackend();
}
