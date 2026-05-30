export function stopStream(streamRef, videoRef) {
  if (streamRef.current) {
    streamRef.current.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }
  if (videoRef.current) {
    videoRef.current.srcObject = null;
  }
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
      `No browser-accessible camera was found.${deviceSummary} If you are on Radxa, prefer \`python app.py web\` or \`python app.py kiosk\` in Chromium instead of the embedded native shell.`,
    );
  }

  if (name === "OverconstrainedError") {
    return new Error("The browser found a camera, but the requested video mode is unsupported.");
  }

  if (!navigator.mediaDevices?.getUserMedia) {
    return new Error(
      "This app shell does not expose browser camera access. On Radxa, use `python app.py web` or `python app.py kiosk` in Chromium.",
    );
  }

  return new Error(`${message}${deviceSummary}`);
}

export function stopLive(timerRef, streamRef, videoRef) {
  if (timerRef.current) {
    window.clearTimeout(timerRef.current);
    timerRef.current = null;
  }
  stopStream(streamRef, videoRef);
}

export function drawBoxes(canvas, video, boxes = []) {
  if (!canvas || !video) {
    return;
  }
  canvas.width = video.videoWidth || 0;
  canvas.height = video.videoHeight || 0;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.strokeStyle = "#27d0bd";
  context.lineWidth = 3;
  context.setLineDash([]);
  boxes.forEach((box) => {
    context.strokeRect(box.x, box.y, box.width, box.height);
  });
}

function resolveFrameSize(video, maxWidth, maxHeight) {
  const sourceWidth = video.videoWidth || 0;
  const sourceHeight = video.videoHeight || 0;
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

export function frameToBlob(video, canvas, options = {}) {
  const {
    maxWidth = video.videoWidth,
    maxHeight = video.videoHeight,
    type = "image/jpeg",
    quality = 0.92,
  } = options;
  const { width, height } = resolveFrameSize(video, maxWidth, maxHeight);
  if (!width || !height) {
    return Promise.reject(new Error("Could not capture the current video frame."));
  }

  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  context.drawImage(video, 0, 0, width, height);
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

export async function startUserCamera(videoRef, streamRef) {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error(
      "This app shell does not expose browser camera access. On Radxa, use `python app.py web` or `python app.py kiosk` in Chromium.",
    );
  }

  stopStream(streamRef, videoRef);

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

    streamRef.current = stream;
    if (videoRef.current) {
      videoRef.current.srcObject = stream;
      try {
        await videoRef.current.play();
      } catch {
        // Some browser shells autoplay the stream without requiring an explicit play call.
      }
    }
  } catch (requestError) {
    throw normalizeCameraError(requestError, videoInputs);
  }
}
