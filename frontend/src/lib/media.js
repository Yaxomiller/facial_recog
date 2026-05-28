export function stopStream(streamRef, videoRef) {
  if (streamRef.current) {
    streamRef.current.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }
  if (videoRef.current) {
    videoRef.current.srcObject = null;
  }
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
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: { ideal: "user" },
        width: { ideal: 1920 },
        height: { ideal: 1080 },
        frameRate: { ideal: 30 },
      },
      audio: false,
    });
  } catch {
    stream = await navigator.mediaDevices.getUserMedia({
      video: true,
      audio: false,
    });
  }
  streamRef.current = stream;
  if (videoRef.current) {
    videoRef.current.srcObject = stream;
  }
}
