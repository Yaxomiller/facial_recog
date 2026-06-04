import assert from "node:assert/strict";
import test from "node:test";

import { apiClient } from "../src/lib/api.js";
import { startUserCamera } from "../src/lib/media.js";

function createFakeImage() {
  const listeners = new Map([
    ["load", new Set()],
    ["error", new Set()],
  ]);

  return {
    tagName: "IMG",
    crossOrigin: "",
    _src: "",
    addEventListener(type, listener) {
      listeners.get(type)?.add(listener);
    },
    removeEventListener(type, listener) {
      listeners.get(type)?.delete(listener);
    },
    removeAttribute(name) {
      if (name === "src") {
        this._src = "";
      }
    },
    get src() {
      return this._src;
    },
    set src(value) {
      this._src = value;
      queueMicrotask(() => {
        for (const listener of listeners.get("load") || []) {
          listener();
        }
      });
    },
  };
}

test("startUserCamera falls back to the authenticated backend camera stream", async () => {
  const originalNavigator = globalThis.navigator;
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: {
      mediaDevices: {
        async enumerateDevices() {
          return [];
        },
        async getUserMedia() {
          const error = new Error("No camera");
          error.name = "NotFoundError";
          throw error;
        },
      },
    },
  });

  const originalStartLocalCamera = apiClient.startLocalCamera;
  const originalLocalCameraStreamUrl = apiClient.localCameraStreamUrl;
  const originalStopLocalCamera = apiClient.stopLocalCamera;
  const startCalls = [];
  const stopCalls = [];

  apiClient.startLocalCamera = async (token) => {
    startCalls.push(token);
    return { source_name: "Radxa GStreamer pipeline (/dev/video0)" };
  };
  apiClient.localCameraStreamUrl = (token, cacheBust) => `/api/v2/local-camera/stream.mjpg?token=${token}&ts=${cacheBust}`;
  apiClient.stopLocalCamera = async (token) => {
    stopCalls.push(token);
    return { ok: true, running: false };
  };

  try {
    const image = createFakeImage();
    const videoRef = { current: { srcObject: null } };
    const imageRef = { current: image };
    const streamRef = { current: null };

    const session = await startUserCamera({
      token: "session-token",
      videoRef,
      imageRef,
      streamRef,
    });

    assert.equal(session.mode, "backend");
    assert.equal(session.sourceName, "Radxa GStreamer pipeline (/dev/video0)");
    assert.equal(streamRef.current.mode, "backend");
    assert.deepEqual(startCalls, ["session-token"]);
    assert.match(image.src, /\/api\/v2\/local-camera\/stream\.mjpg\?token=session-token/);

    streamRef.current.stop();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(stopCalls, ["session-token"]);
  } finally {
    apiClient.startLocalCamera = originalStartLocalCamera;
    apiClient.localCameraStreamUrl = originalLocalCameraStreamUrl;
    apiClient.stopLocalCamera = originalStopLocalCamera;
    Object.defineProperty(globalThis, "navigator", {
      configurable: true,
      value: originalNavigator,
    });
  }
});
