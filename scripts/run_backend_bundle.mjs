import { buildNativeBackend } from "./build_native_backend.mjs";

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
