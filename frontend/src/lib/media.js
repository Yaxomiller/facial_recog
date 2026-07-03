import { apiClient } from "./api.js";

const BACKEND_FRAME_INTERVAL_MS = 120;
const BACKEND_FRAME_RETRY_DELAY_MS = 250;

function mediaSize(media) {
  if (!media) {
    return { width: 0, height: 0 };
  }

  if (media.tagName === "IMG") {
    return {
      width: media.naturalWidth || 0,
      height: media.naturalHeight || 0,
    };
  }

  return {
    width: media.videoWidth || 0,
    height: media.videoHeight || 0,
  };
}

export function isMediaReady(media) {
  if (!media) {
    return false;
  }

  if (media.tagName === "IMG") {
    return Boolean(media.naturalWidth > 0 && media.naturalHeight > 0);
  }

  return Boolean(media.readyState >= 2 && media.videoWidth > 0 && media.videoHeight > 0);
}

function clearImagePreview(imageRef) {
  if (imageRef?.current) {
    imageRef.current.removeAttribute("src");
  }
}

export function stopStream(streamRef, videoRef, imageRef = null) {
  const session = streamRef.current;
  if (session?.stop) {
    session.stop();
  }
  streamRef.current = null;

  if (videoRef?.current) {
    videoRef.current.srcObject = null;
  }
  clearImagePreview(imageRef);
}

export function stopLive(timerRef, streamRef, videoRef, imageRef = null) {
  if (timerRef.current) {
    window.clearTimeout(timerRef.current);
    timerRef.current = null;
  }
  stopStream(streamRef, videoRef, imageRef);
}

export function drawBoxes(canvas, media, boxes = []) {
  if (!canvas || !media) {
    return;
  }

  const { width, height } = mediaSize(media);
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.strokeStyle = "#27d0bd";
  context.lineWidth = 3;
  context.setLineDash([]);
  boxes.forEach((box) => {
    context.strokeRect(box.x, box.y, box.width, box.height);
  });
}

function resolveFrameSize(media, maxWidth, maxHeight) {
  const { width: sourceWidth, height: sourceHeight } = mediaSize(media);
  if (!sourceWidth || !sourceHeight) {
    return { width: 0, height: 0 };
  }

  let width = sourceWidth;
  let height = sourceHeight;

  if (maxWidth && width > maxWidth) {
    const scale = maxWidth / width;
    width = maxWidth;
    height = Math.round(height * scale);
  }

  if (maxHeight && height > maxHeight) {
    const scale = maxHeight / height;
    height = maxHeight;
    width = Math.round(width * scale);
  }

  return {
    width: Math.max(1, Math.round(width)),
    height: Math.max(1, Math.round(height)),
  };
}

export function frameToBlob(media, canvas, options = {}) {
  const {
    maxWidth = mediaSize(media).width,
    maxHeight = mediaSize(media).height,
    type = "image/jpeg",
    quality = 0.92,
  } = options;
  const { width, height } = resolveFrameSize(media, maxWidth, maxHeight);
  if (!width || !height) {
    return Promise.reject(new Error("Could not capture the current camera frame."));
  }

  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  context.drawImage(media, 0, 0, width, height);
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (!blob) {
        reject(new Error("Could not capture the current camera frame."));
        return;
      }
      resolve(blob);
    }, type, quality);
  });
}

function loadImageSource(image, url, errorMessage) {
  return new Promise((resolve, reject) => {
    const handleLoad = () => {
      cleanup();
      resolve();
    };
    const handleError = () => {
      cleanup();
      reject(new Error(errorMessage));
    };
    const cleanup = () => {
      image.removeEventListener("load", handleLoad);
      image.removeEventListener("error", handleError);
    };

    image.addEventListener("load", handleLoad);
    image.addEventListener("error", handleError);
    image.crossOrigin = "anonymous";
    image.src = url;
  });
}

async function startBackendCamera(token, imageRef, streamRef) {
  const previewImage = imageRef?.current;
  if (!previewImage) {
    throw new Error("The camera preview element is unavailable.");
  }

  const loadFrame = () =>
    loadImageSource(
      previewImage,
      apiClient.localCameraFrameUrl(token, Date.now()),
      "Could not load a frame from the camera service.",
    );

  const backendStatus = await apiClient.startLocalCamera(token);

  // Prefer the MJPEG stream: one persistent connection the browser decodes
  // natively, instead of a fresh HTTP request per frame. Much smoother and
  // lower latency on embedded devices. Fall back to frame polling on error.
  try {
    await loadImageSource(
      previewImage,
      apiClient.localCameraStreamUrl(token),
      "Could not open the camera stream.",
    );
    const streamSession = {
      mode: "backend-stream",
      stopRequested: false,
      stop() {
        if (this.stopRequested) {
          return;
        }
        this.stopRequested = true;
        clearImagePreview(imageRef);
        void apiClient.stopLocalCamera(token).catch(() => {});
      },
    };
    streamRef.current = streamSession;
    return {
      mode: "backend",
      sourceName: backendStatus?.source_name || "gstreamer-camera",
    };
  } catch (_streamError) {
    // Stream endpoint unavailable — fall through to frame polling below.
  }
  const backendSession = {
    mode: "backend",
    stopRequested: false,
    refreshTimer: null,
    scheduleNextFrame(delay = BACKEND_FRAME_INTERVAL_MS) {
      if (this.stopRequested) {
        return;
      }

      this.refreshTimer = globalThis.setTimeout(async () => {
        this.refreshTimer = null;
        if (this.stopRequested) {
          return;
        }

        try {
          await loadFrame();
          this.scheduleNextFrame(BACKEND_FRAME_INTERVAL_MS);
        } catch (_loadError) {
          if (this.stopRequested) {
            return;
          }
          clearImagePreview(imageRef);
          this.scheduleNextFrame(BACKEND_FRAME_RETRY_DELAY_MS);
        }
      }, delay);
    },
    stop() {
      if (this.stopRequested) {
        return;
      }
      this.stopRequested = true;
      if (this.refreshTimer !== null) {
        globalThis.clearTimeout(this.refreshTimer);
        this.refreshTimer = null;
      }
      clearImagePreview(imageRef);
      void apiClient.stopLocalCamera(token).catch(() => {});
    },
  };

  try {
    await loadFrame();
  } catch (loadError) {
    backendSession.stop();
    throw loadError;
  }

  streamRef.current = backendSession;
  backendSession.scheduleNextFrame();
  return {
    mode: "backend",
    sourceName: backendStatus?.source_name || "gstreamer-camera",
  };
}

export async function startUserCamera({ token, imageRef, streamRef, videoRef = null }) {
  stopStream(streamRef, videoRef, imageRef);
  return startBackendCamera(token, imageRef, streamRef);
}
