import { useEffect, useRef, useState } from "react";
import Panel from "../components/Panel";
import { apiClient } from "../lib/api";
import { frameToBlob, isMediaReady, startUserCamera, stopStream } from "../lib/media";

const MIN_ENROLLMENT_IMAGES = 3;
const CAPTURE_GUIDANCE = [
  "Look straight at the camera.",
  "Turn slightly to the left.",
  "Turn slightly to the right.",
  "Lift your chin slightly.",
  "Lower your chin slightly.",
];

export default function EnrollView({ token, onUpdated }) {
  const [employeeCode, setEmployeeCode] = useState("");
  const [name, setName] = useState("");
  const [frames, setFrames] = useState([]);
  const [cameraMode, setCameraMode] = useState(null);
  const [message, setMessage] = useState(
    `Enter details and capture at least ${MIN_ENROLLMENT_IMAGES} clear photos. Start with front, slight left, and slight right views.`,
  );
  const [busy, setBusy] = useState(false);
  const videoRef = useRef(null);
  const imageRef = useRef(null);
  const canvasRef = useRef(null);
  const streamRef = useRef(null);
  const framesRef = useRef([]);

  useEffect(() => {
    framesRef.current = frames;
  }, [frames]);

  useEffect(() => {
    return () => {
      stopStream(streamRef, videoRef, imageRef);
      framesRef.current.forEach((frame) => URL.revokeObjectURL(frame.preview));
    };
  }, []);

  function activeMediaElement() {
    return cameraMode === "backend" ? imageRef.current : videoRef.current;
  }

  function clearFrames() {
    frames.forEach((frame) => URL.revokeObjectURL(frame.preview));
    setFrames([]);
  }

  async function handleStartCamera() {
    try {
      const session = await startUserCamera({
        token,
        videoRef,
        imageRef,
        streamRef,
      });
      setCameraMode(session.mode);
      setMessage(
        session.mode === "backend"
          ? `Local camera bridge is live. Capture ${MIN_ENROLLMENT_IMAGES} clear face pictures. ${CAPTURE_GUIDANCE[framesRef.current.length] || CAPTURE_GUIDANCE[0]}`
          : `Camera is live. Capture ${MIN_ENROLLMENT_IMAGES} clear face pictures. ${CAPTURE_GUIDANCE[framesRef.current.length] || CAPTURE_GUIDANCE[0]}`,
      );
    } catch (requestError) {
      const text = requestError instanceof Error ? requestError.message : "Camera could not be started.";
      setMessage(text);
    }
  }

  async function handleCapture() {
    const media = activeMediaElement();
    if (!isMediaReady(media)) {
      setMessage("Camera is not ready.");
      return;
    }
    const blob = await frameToBlob(media, canvasRef.current);
    const preview = URL.createObjectURL(blob);
    setFrames((current) => [
      ...current,
      { blob, preview, filename: `capture-${Date.now()}.jpg` },
    ]);
    const nextHint = CAPTURE_GUIDANCE[frames.length + 1];
    setMessage(
      nextHint
        ? `Picture captured. ${frames.length + 1} of ${MIN_ENROLLMENT_IMAGES} taken. Next: ${nextHint}`
        : `Picture captured. ${frames.length + 1} of ${MIN_ENROLLMENT_IMAGES} taken.`,
    );
  }

  async function handleEnroll(event) {
    event.preventDefault();
    if (frames.length < MIN_ENROLLMENT_IMAGES) {
      setMessage(`Add at least ${MIN_ENROLLMENT_IMAGES} employee pictures.`);
      return;
    }

    setBusy(true);
    try {
      const form = new FormData();
      form.append("employee_code", employeeCode.trim());
      form.append("name", name.trim());
      form.append("replace_existing", "true");
      frames.forEach((frame) => form.append("images", frame.blob, frame.filename));
      await apiClient.post("/api/v2/workers/enroll", token, form);
      setEmployeeCode("");
      setName("");
      clearFrames();
      setMessage("New user added successfully.");
      await onUpdated();
    } catch (requestError) {
      const text = requestError instanceof Error ? requestError.message : "Could not add the new user.";
      setMessage(text);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="device-stack">
      <Panel eyebrow="Step 1" title="Employee Details">
        <form className="form-stack" onSubmit={handleEnroll}>
          <label>
            <span>Employee ID</span>
            <input
              value={employeeCode}
              onChange={(event) => setEmployeeCode(event.target.value)}
              placeholder="Enter employee ID"
              required
            />
          </label>

          <label>
            <span>Name</span>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Enter employee name"
              required
            />
          </label>

          <div className="button-row">
            <button className="button button-secondary" onClick={handleStartCamera} type="button">Open Camera</button>
            <button className="button button-ghost" onClick={handleCapture} type="button">Take Picture</button>
            <button className="button button-ghost" onClick={clearFrames} type="button">Clear Pictures</button>
            <button className="button button-primary" type="submit" disabled={busy || frames.length < MIN_ENROLLMENT_IMAGES}>
              {busy ? "Saving..." : frames.length < MIN_ENROLLMENT_IMAGES ? `Need ${MIN_ENROLLMENT_IMAGES} Photos` : "Save User"}
            </button>
          </div>

          <div className="alert info">{message}</div>
        </form>
      </Panel>

      <Panel eyebrow="Step 2" title="Camera Preview">
        <div className="video-frame">
          <video ref={videoRef} autoPlay playsInline muted hidden={cameraMode === "backend"} />
          <img ref={imageRef} alt="Camera preview" crossOrigin="anonymous" hidden={cameraMode !== "backend"} />
          <canvas ref={canvasRef} hidden />
        </div>

        <div className="capture-strip">
          {frames.length ? (
            frames.map((frame) => (
              <img key={frame.preview} src={frame.preview} alt="Employee preview" />
            ))
          ) : (
            <div className="empty-state">{`Added pictures will appear here. Capture at least ${MIN_ENROLLMENT_IMAGES}.`}</div>
          )}
        </div>
      </Panel>
    </div>
  );
}
