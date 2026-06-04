import { apiClient } from "./api.js";

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
  if (session?.mode === "backend") {
    session.stop();
    streamRef.current = null;
  } else if (session?.mode === "browser") {
    session.stream.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  } else if (streamRef.current) {
    streamRef.current.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }

  if (videoRef.current) {
    videoRef.current.srcObject = null;
  }
  clearImagePreview(imageRef);
}

async function listVideoInputs() {
  if (!navigator.mediaDevices?.enumerateDevices) {
    return [];
  }

  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    return devices.filter((device) => device.kind === "videoinput");
  } catch {
    return [];
  }
}

async function requestCameraStream(constraintsList, videoInputs) {
  let lastError = null;

  for (const constraints of constraintsList) {
    try {
      return await navigator.mediaDevices.getUserMedia(constraints);
    } catch (requestError) {
      lastError = requestError;
    }
  }

  for (const device of videoInputs) {
    try {
      return await navigator.mediaDevices.getUserMedia({
        video: {
          deviceId: { exact: device.deviceId },
        },
        audio: false,
      });
    } catch (requestError) {
      lastError = requestError;
    }
  }

  throw lastError ?? new Error("Camera could not be started.");
}

function normalizeCameraError(requestError, videoInputs) {
  const name = requestError instanceof Error ? requestError.name : "";
  const message = requestError instanceof Error ? requestError.message : "Camera could not be started.";
  const deviceSummary = videoInputs.length
    ? ` Browser sees ${videoInputs.length} video input device${videoInputs.length === 1 ? "" : "s"}.`
    : " Browser does not currently expose any video input devices.";

  if (name === "NotAllowedError" || name === "SecurityError") {
    return new Error("Camera permission was denied. Allow camera access in the browser and try again.");
  }

  if (name === "NotReadableError") {
    return new Error("The camera exists but is busy or unavailable to the browser. Close other apps using the camera and try again.");
  }

  if (name === "NotFoundError" || /requested device not found/i.test(message)) {
    return new Error(
      `No browser-accessible camera was found.${deviceSummary} The app will try the local backend camera bridge instead.`,
    );
  }

  if (name === "OverconstrainedError") {
    return new Error("The browser found a camera, but the requested video mode is unsupported.");
  }

  if (!navigator.mediaDevices?.getUserMedia) {
    return new Error(
      "This app shell does not expose browser camera access. The local backend camera bridge should be used instead.",
    );
  }

  return new Error(`${message}${deviceSummary}`);
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
    return Promise.reject(new Error("Could not capture the current video frame."));
  }

  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  context.drawImage(media, 0, 0, width, height);
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (!blob) {
        reject(new Error("Could not capture the current video frame."));
        return;
      }
      resolve(blob);
    }, type, quality);
  });
}

function bindVideoStream(videoRef, imageRef, stream) {
  clearImagePreview(imageRef);
  if (videoRef.current) {
    videoRef.current.srcObject = stream;
  }
}

function bindImageStream(image, url) {
  return new Promise((resolve, reject) => {
    const handleLoad = () => {
      cleanup();
      resolve();
    };
    const handleError = () => {
      cleanup();
      reject(new Error("Could not load the local camera stream from the backend camera bridge."));
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

async function startBackendCamera(token, videoRef, imageRef, streamRef) {
  const previewImage = imageRef?.current;
  if (!previewImage) {
    throw new Error("The local camera preview element is unavailable.");
  }

  const backendStatus = await apiClient.startLocalCamera(token);
  await bindImageStream(
    previewImage,
    apiClient.localCameraStreamUrl(token, Date.now()),
  );

  const backendSession = {
    mode: "backend",
    stopRequested: false,
    stop() {
      if (this.stopRequested) {
        return;
      }
      this.stopRequested = true;
      if (videoRef.current) {
        videoRef.current.srcObject = null;
      }
      clearImagePreview(imageRef);
      void apiClient.stopLocalCamera(token).catch(() => {});
    },
  };

  streamRef.current = backendSession;
  return {
    mode: "backend",
    sourceName: backendStatus?.source_name || "local-camera",
  };
}

export async function startUserCamera({ token, videoRef, imageRef, streamRef }) {
  if (!navigator.mediaDevices?.getUserMedia) {
    return startBackendCamera(token, videoRef, imageRef, streamRef);
  }

  stopStream(streamRef, videoRef, imageRef);

  const videoInputs = await listVideoInputs();

  try {
    const stream = await requestCameraStream(
      [
        {
          video: {
            facingMode: { ideal: "user" },
            width: { ideal: 1920 },
            height: { ideal: 1080 },
            frameRate: { ideal: 30 },
          },
          audio: false,
        },
        {
          video: {
            width: { ideal: 1920 },
            height: { ideal: 1080 },
            frameRate: { ideal: 30 },
          },
          audio: false,
        },
        {
          video: true,
          audio: false,
        },
      ],
      videoInputs,
    );

    streamRef.current = {
      mode: "browser",
      stream,
    };
    bindVideoStream(videoRef, imageRef, stream);
    if (videoRef.current) {
      try {
        await videoRef.current.play();
      } catch {
        // Some browser shells autoplay the stream without requiring an explicit play call.
      }
    }
    return { mode: "browser" };
  } catch (requestError) {
    try {
      return await startBackendCamera(token, videoRef, imageRef, streamRef);
    } catch (backendError) {
      const browserError = normalizeCameraError(requestError, videoInputs);
      const backendMessage = backendError instanceof Error ? backendError.message : "The local camera bridge could not be started.";
      throw new Error(`${browserError.message} Local camera bridge error: ${backendMessage}`);
    }
  }
}
