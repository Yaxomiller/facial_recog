import assert from "node:assert/strict";
import test from "node:test";

import { apiClient } from "../src/lib/api.js";
import { startUserCamera, stopStream } from "../src/lib/media.js";

function createFakeImage() {
  const listeners = new Map([
    ["load", new Set()],
    ["error", new Set()],
  ]);

  return {
    tagName: "IMG",
    crossOrigin: "",
    naturalWidth: 640,
    naturalHeight: 480,
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

test("startUserCamera always uses the authenticated backend camera feed", async () => {
  const originalStartLocalCamera = apiClient.startLocalCamera;
  const originalLocalCameraFrameUrl = apiClient.localCameraFrameUrl;
  const originalStopLocalCamera = apiClient.stopLocalCamera;
  const startCalls = [];
  const stopCalls = [];

  apiClient.startLocalCamera = async (token) => {
    startCalls.push(token);
    return { source_name: "GStreamer pipeline (/dev/video0)" };
  };
  apiClient.localCameraFrameUrl = (token, cacheBust) => `/api/v2/local-camera/frame?token=${token}&ts=${cacheBust}`;
  apiClient.stopLocalCamera = async (token) => {
    stopCalls.push(token);
    return { ok: true, running: false };
  };

  try {
    const image = createFakeImage();
    const imageRef = { current: image };
    const streamRef = { current: null };

    const session = await startUserCamera({
      token: "session-token",
      imageRef,
      streamRef,
    });

    assert.equal(session.mode, "backend");
    assert.equal(session.sourceName, "GStreamer pipeline (/dev/video0)");
    assert.equal(streamRef.current.mode, "backend");
    assert.deepEqual(startCalls, ["session-token"]);
    assert.match(image.src, /\/api\/v2\/local-camera\/frame\?token=session-token/);

    streamRef.current.stop();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(stopCalls, ["session-token"]);
  } finally {
    apiClient.startLocalCamera = originalStartLocalCamera;
    apiClient.localCameraFrameUrl = originalLocalCameraFrameUrl;
    apiClient.stopLocalCamera = originalStopLocalCamera;
  }
});

test("stopStream clears the backend camera preview", () => {
  const image = createFakeImage();
  image.src = "/api/v2/local-camera/frame?token=session-token";

  let stopCalls = 0;
  const streamRef = {
    current: {
      stop() {
        stopCalls += 1;
      },
    },
  };
  const imageRef = { current: image };

  stopStream(streamRef, null, imageRef);

  assert.equal(stopCalls, 1);
  assert.equal(streamRef.current, null);
  assert.equal(image.src, "");
});
