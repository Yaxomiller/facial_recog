import { buildNativeBackend } from "./build_native_backend.mjs";
import { spawnSync } from "node:child_process";

// The C++ backend bundle is only needed on Linux (Radxa deployment).
// On Windows, the Tauri shell connects to the Python backend over HTTP,
// so skip this step and let the rest of the build continue.
if (process.platform !== "linux") {
  console.log(
    `Skipping native C++ backend build on ${process.platform} ` +
      "(only required for the Linux/Radxa deployment target).",
  );
  process.exit(0);
}

// On Linux, also skip gracefully if cmake is not on PATH rather than
// crashing the whole Tauri build.
const cmakeCheck = spawnSync("cmake", ["--version"], { stdio: "ignore" });
if (cmakeCheck.error) {
  console.warn(
    "cmake not found on PATH — skipping native C++ backend build.\n" +
      "Install the CMake/OpenCV/SQLite toolchain if you need the bundled native backend.",
  );
  process.exit(0);
}

try {
  buildNativeBackend();
  console.log("Bundled the native C++ backend for the desktop Linux app.");
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  console.error(
    "Could not build the native C++ backend bundle. " +
      "Install the required CMake/OpenCV/SQLite toolchain on the Linux build host and try again.\n" +
      `Reason: ${message}`,
  );
  process.exit(1);
}
