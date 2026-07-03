import { useEffect, useRef, useState } from "react";
import Panel from "../components/Panel";
import { apiClient, ApiError } from "../lib/api";
import { drawBoxes, frameToBlob, isMediaReady, startUserCamera, stopLive } from "../lib/media";

const CAMERA_ID = "device-front-camera";
const EXHALE_SECONDS = 10;
const SCAN_LOOP_DELAY_MS = 250;
const EXHALE_FACE_CHECK_INTERVAL_MS = 3000;
const STABLE_SINGLE_FACE_FRAMES = 1;
const DETECTION_FRAME_MAX_WIDTH = 960;
const RECOGNITION_FRAME_MAX_WIDTH = 960;
const UNRECOGNIZED_WARNING_MS = 12000;

// Internal threshold rejections are the system's concern, not the user's.
// Translate them into actionable guidance and keep scanning; only report
// "not recognized" after sustained effort fails.
function classifyReason(reason = "") {
  const text = String(reason).toLowerCase();
  if (!text || text.includes("pending") || text.includes("waiting") || text.includes("more frames")) {
    return { kind: "progress", message: "Verifying identity. Hold still and look at the camera." };
  }
  if (text.includes("multiple faces") || text.includes("one employee")) {
    return { kind: "coach", message: "One person at a time. Keep only one face in front of the camera." };
  }
  if (text.includes("too small") || text.includes("closer")) {
    return { kind: "coach", message: "Move a little closer to the camera." };
  }
  if (text.includes("blur") || text.includes("not stable") || text.includes("repeated")) {
    return { kind: "coach", message: "Hold still for a moment while the scan completes." };
  }
  if (text.includes("too dark") || text.includes("too bright") || text.includes("light")) {
    return { kind: "coach", message: "Adjust your position so your face is evenly lit." };
  }
  if (text.includes("re-enroll") || text.includes("no enrolled") || text.includes("enrolled samples")) {
    return {
      kind: "blocked",
      message: "Enrollment is incomplete for this employee. Ask the administrator to re-enroll with 5 clear photos.",
    };
  }
  return { kind: "unknown", message: "Verifying identity. Keep looking at the camera." };
}

export default function RecognitionView({ token, onUpdated, onSessionExpired = () => {} }) {
  const [message, setMessage] = useState("Press start to scan a face.");
  const [identifiedMatch, setIdentifiedMatch] = useState(null);
  const [breathResult, setBreathResult] = useState(null);
  const [running, setRunning] = useState(false);
  const [exhaling, setExhaling] = useState(false);
  const [countdown, setCountdown] = useState(0);
  const [lastUnknown, setLastUnknown] = useState(false);
  const [lastReason, setLastReason] = useState("");
  const imageRef = useRef(null);
  const canvasRef = useRef(null);
  const overlayRef = useRef(null);
  const streamRef = useRef(null);
  const timerRef = useRef(null);
  const exhaleTimerRef = useRef(null);
  const exhaleCheckTimerRef = useRef(null);
  const requestInFlightRef = useRef(false);
  const exhalingRef = useRef(false);
  const identifiedMatchRef = useRef(null);
  const exhaleCancelledRef = useRef(false);
  const breathSessionRef = useRef(null);
  const stableSingleFaceFramesRef = useRef(0);
  const lastRecognitionAttemptRef = useRef(0);
  const unrecognizedSinceRef = useRef(0);

  useEffect(() => {
    exhalingRef.current = exhaling;
  }, [exhaling]);

  useEffect(() => {
    identifiedMatchRef.current = identifiedMatch;
  }, [identifiedMatch]);

  useEffect(() => () => {
    clearExhaleTimer();
    clearExhaleCheckTimer();
    void cancelActiveBreathSession();
    stopLive(timerRef, streamRef, null, imageRef);
  }, []);

  function activeMediaElement() {
    return imageRef.current;
  }

  function clearScanTimer() {
    if (timerRef.current) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }

  function clearExhaleTimer() {
    if (exhaleTimerRef.current) {
      window.clearInterval(exhaleTimerRef.current);
      exhaleTimerRef.current = null;
    }
  }

  function clearExhaleCheckTimer() {
    if (exhaleCheckTimerRef.current) {
      window.clearInterval(exhaleCheckTimerRef.current);
      exhaleCheckTimerRef.current = null;
    }
  }

  function resetScanProgress() {
    stableSingleFaceFramesRef.current = 0;
    lastRecognitionAttemptRef.current = 0;
    unrecognizedSinceRef.current = 0;
  }

  async function captureFrameBlob(options) {
    const media = activeMediaElement();
    if (!isMediaReady(media)) {
      return null;
    }

    return frameToBlob(media, canvasRef.current, options);
  }

  async function captureDetectionFrame() {
    const blob = await captureFrameBlob({
      maxWidth: DETECTION_FRAME_MAX_WIDTH,
      quality: 0.8,
    });
    if (!blob) {
      return null;
    }

    const form = new FormData();
    form.append("image", blob, "detect.jpg");
    const result = await apiClient.detectFaces(token, form);
    drawBoxes(overlayRef.current, activeMediaElement(), result.boxes || []);
    return result;
  }

  async function captureRecognitionFrame() {
    const blob = await captureFrameBlob({
      maxWidth: RECOGNITION_FRAME_MAX_WIDTH,
      quality: 0.85,
    });
    if (!blob) {
      return null;
    }

    const form = new FormData();
    form.append("camera_id", CAMERA_ID);
    form.append("top_k", "3");
    form.append("image", blob, "scan.jpg");
    const result = await apiClient.recognize(token, form);
    drawBoxes(overlayRef.current, activeMediaElement(), result.boxes || []);
    return result;
  }

  function firstDebugReason(result, fallbackMessage) {
    return result.debug_faces?.find((face) => face.reason)?.reason || fallbackMessage;
  }

  function handleRecognitionFailure(reason, messageText = "Face detected. Matching is still not confirmed.") {
    setLastUnknown(true);
    setLastReason(reason);
    setMessage(messageText);
  }

  function clearRecognitionFailure() {
    setLastUnknown(false);
    setLastReason("");
  }

  function storeIdentifiedMatch(match) {
    identifiedMatchRef.current = match;
    setIdentifiedMatch(match);
  }

  function setExhaleState(value) {
    exhalingRef.current = value;
    setExhaling(value);
  }

  async function cancelActiveBreathSession() {
    const sessionId = breathSessionRef.current;
    if (!sessionId) {
      return;
    }

    breathSessionRef.current = null;
    try {
      await apiClient.cancelBreathTestSession(token, sessionId);
    } catch (_requestError) {
      // Ignore cancellation errors here because we are already unwinding the UI state.
    }
  }

  function startScanLoop() {
    clearScanTimer();
    const runScan = async () => {
      if (requestInFlightRef.current || exhalingRef.current || !streamRef.current) {
        return;
      }

      requestInFlightRef.current = true;
      try {
        // Detection phase: cheap face-presence check until exactly one face
        // is in view. Once locked, every subsequent cycle goes straight to
        // recognition (which detects internally) — no duplicate scanning.
        if (stableSingleFaceFramesRef.current < STABLE_SINGLE_FACE_FRAMES) {
          const detectionResult = await captureDetectionFrame();
          if (!detectionResult) {
            return;
          }

          const detectedFaces = detectionResult.detected_faces || 0;
          if (detectedFaces === 0) {
            resetScanProgress();
            clearRecognitionFailure();
            setMessage("No face detected.");
            return;
          }

          if (detectedFaces > 1) {
            resetScanProgress();
            handleRecognitionFailure(
              "Only one employee can be scanned at a time.",
              "Multiple faces detected. Keep one person in front of the camera.",
            );
            return;
          }

          stableSingleFaceFramesRef.current += 1;
          clearRecognitionFailure();
          setMessage("Face detected. Hold still while identity is checked.");
        }

        const result = await captureRecognitionFrame();
        if (!result) {
          return;
        }

        if (result.matches?.length) {
          clearScanTimer();
          resetScanProgress();
          storeIdentifiedMatch(result.matches[0]);
          setBreathResult(null);
          clearRecognitionFailure();
          setMessage(`${result.matches[0].name} identified. Press Exhale to start the breath test.`);
          return;
        }

        if ((result.detected_faces || 0) > 1) {
          resetScanProgress();
          handleRecognitionFailure(
            "Only one employee can be verified at a time.",
            "Multiple faces detected. Keep one person in front of the camera.",
          );
          return;
        }

        if (result.detected_faces > 0) {
          const classified = classifyReason(firstDebugReason(result, ""));

          if (classified.kind === "blocked") {
            handleRecognitionFailure(classified.message, classified.message);
            return;
          }

          const now = Date.now();
          if (!unrecognizedSinceRef.current) {
            unrecognizedSinceRef.current = now;
          }

          // Keep working toward the thresholds; escalate to a visible
          // "not recognized" verdict only after sustained failure.
          if (classified.kind === "unknown" && now - unrecognizedSinceRef.current >= UNRECOGNIZED_WARNING_MS) {
            handleRecognitionFailure(
              "No enrolled employee matched this face after repeated checks.",
              "Face not recognized. Try again, or contact the administrator if you are enrolled.",
            );
            return;
          }

          clearRecognitionFailure();
          setMessage(classified.message);
          return;
        }

        resetScanProgress();
        clearRecognitionFailure();
        setMessage("No face detected.");
      } catch (requestError) {
        if (requestError instanceof ApiError && requestError.status === 401) {
          onSessionExpired("Session expired during scanning. Please log in again.");
          handleStop();
          return;
        }
        const text = requestError instanceof Error ? requestError.message : "Scan request failed.";
        setMessage(text);
      } finally {
        requestInFlightRef.current = false;
        if (streamRef.current && !identifiedMatchRef.current && !exhalingRef.current) {
          timerRef.current = window.setTimeout(() => {
            void runScan();
          }, SCAN_LOOP_DELAY_MS);
        }
      }
    };

    void runScan();
  }

  function restartScanningAfterFaceMismatch(reason) {
    void cancelActiveBreathSession();
    exhaleCancelledRef.current = true;
    clearExhaleTimer();
    clearExhaleCheckTimer();
    resetScanProgress();
    setCountdown(0);
    setExhaleState(false);
    setBreathResult(null);
    storeIdentifiedMatch(null);
    handleRecognitionFailure(
      reason,
      "Face changed during exhale. Scan again to restart the breath test.",
    );
    if (streamRef.current) {
      setRunning(true);
      startScanLoop();
    } else {
      setRunning(false);
      void handleStart();
    }
  }

  async function verifyExhaleIdentity(expectedMatch) {
    if (!expectedMatch || requestInFlightRef.current) {
      return true;
    }

    requestInFlightRef.current = true;
    try {
      const result = await captureRecognitionFrame();
      if (!result) {
        return true;
      }

      const sameWorkerMatch = result.matches?.find((match) => match.worker_id === expectedMatch.worker_id);
      if (sameWorkerMatch) {
        storeIdentifiedMatch(sameWorkerMatch);
        clearRecognitionFailure();
        return true;
      }

      if (result.matches?.length) {
        restartScanningAfterFaceMismatch(
          `${result.matches[0].name} was detected during exhale instead of ${expectedMatch.name}.`,
        );
        return false;
      }

      if (result.detected_faces > 0) {
        restartScanningAfterFaceMismatch(
          firstDebugReason(result, `The face no longer matches ${expectedMatch.name}.`),
        );
        return false;
      }

      restartScanningAfterFaceMismatch(
        `No verified face was visible for ${expectedMatch.name} during exhale.`,
      );
      return false;
    } catch (requestError) {
      if (requestError instanceof ApiError && requestError.status === 401) {
        onSessionExpired("Session expired during exhale verification. Please log in again.");
        handleStop();
        return false;
      }
      const text = requestError instanceof Error ? requestError.message : "Face verification failed.";
      restartScanningAfterFaceMismatch(text);
      return false;
    } finally {
      requestInFlightRef.current = false;
    }
  }

  async function waitForPendingRecognition() {
    while (requestInFlightRef.current) {
      // Let the final face re-check finish before the breath test is recorded.
      // This keeps exhale completion tied to the latest verified identity.
      // eslint-disable-next-line no-await-in-loop
      await new Promise((resolve) => {
        window.setTimeout(resolve, 80);
      });
    }
  }

  async function handleStart() {
    if (running) {
      return;
    }

    try {
      clearExhaleTimer();
      clearExhaleCheckTimer();
      exhaleCancelledRef.current = false;
      resetScanProgress();
      storeIdentifiedMatch(null);
      setBreathResult(null);
      setCountdown(0);
      setExhaleState(false);
      clearRecognitionFailure();
      setRunning(true);
      await startUserCamera({
        token,
        imageRef,
        streamRef,
      });
      setMessage("Camera started. Hold the face steady and look at the screen.");
      startScanLoop();
    } catch (requestError) {
      setRunning(false);
      requestInFlightRef.current = false;
      const text = requestError instanceof Error ? requestError.message : "Camera could not be started.";
      setMessage(text);
    }
  }

  async function handleExhale() {
    if (!identifiedMatch || exhaling) {
      return;
    }

    const matchToVerify = identifiedMatchRef.current || identifiedMatch;
    const initialVerificationPassed = await verifyExhaleIdentity(matchToVerify);
    if (!initialVerificationPassed) {
      return;
    }

    let breathSession = null;
    try {
      breathSession = await apiClient.startBreathTestSession(token, {
        worker_id: matchToVerify.worker_id,
        camera_id: CAMERA_ID,
      });
    } catch (requestError) {
      if (requestError instanceof ApiError && requestError.status === 401) {
        onSessionExpired("Session expired before the breath test started. Please log in again.");
        handleStop();
        return;
      }
      const text = requestError instanceof Error ? requestError.message : "Breath test could not be started.";
      setMessage(text);
      return;
    }

    breathSessionRef.current = breathSession.session_id;
    const breathSeconds = Math.max(1, Math.ceil(Number(breathSession.sample_seconds) || EXHALE_SECONDS));
    exhaleCancelledRef.current = false;
    setBreathResult(null);
    setExhaleState(true);
    setCountdown(breathSeconds);
    clearRecognitionFailure();
    setMessage("Exhale now. Keep your face in view until the timer finishes.");

    exhaleCheckTimerRef.current = window.setInterval(() => {
      void verifyExhaleIdentity(identifiedMatchRef.current || matchToVerify);
    }, EXHALE_FACE_CHECK_INTERVAL_MS);

    let remaining = breathSeconds;
    exhaleTimerRef.current = window.setInterval(async () => {
      remaining -= 1;
      if (remaining > 0) {
        setCountdown(remaining);
        return;
      }

      clearExhaleTimer();
      clearExhaleCheckTimer();
      setCountdown(0);

      try {
        await waitForPendingRecognition();
        if (exhaleCancelledRef.current) {
          return;
        }

        const verifiedMatch = identifiedMatchRef.current || matchToVerify;
        if (!verifiedMatch) {
          restartScanningAfterFaceMismatch("The verified user was lost before the breath test finished.");
          return;
        }

        const sessionId = breathSessionRef.current;
        if (!sessionId) {
          setMessage("The breath test session was lost before the result could be stored.");
          return;
        }

        const result = await apiClient.completeBreathTestSession(token, {
          session_id: sessionId,
          matched_score: verifiedMatch.score,
        });
        breathSessionRef.current = null;
        setBreathResult(result);
        if (!result.overall_clear) {
          const failures = [
            result.alcohol_clear ? null : "alcohol",
            result.cannabis_clear ? null : "cannabis",
          ].filter(Boolean).join(" and ");
          setMessage(`${result.name} failed the ${failures} check. Attendance not marked.`);
        } else if (result.attendance_marked) {
          setMessage(`${result.name} cleared the breath test. Attendance marked.`);
        } else {
          setMessage(`${result.name} cleared the breath test, but attendance was already marked recently.`);
        }
        await onUpdated();
      } catch (requestError) {
        if (requestError instanceof ApiError && requestError.status === 401) {
          onSessionExpired("Session expired during the breath test. Please log in again.");
          return;
        }
        await cancelActiveBreathSession();
        const text = requestError instanceof Error ? requestError.message : "Breath test failed.";
        setMessage(text);
      } finally {
        requestInFlightRef.current = false;
        setExhaleState(false);
        if (!exhaleCancelledRef.current) {
          drawBoxes(overlayRef.current, activeMediaElement(), []);
          stopLive(timerRef, streamRef, null, imageRef);
          setRunning(false);
        }
      }
    }, 1000);
  }

  function handleStop() {
    clearExhaleTimer();
    clearExhaleCheckTimer();
    void cancelActiveBreathSession();
    exhaleCancelledRef.current = true;
    resetScanProgress();
    stopLive(timerRef, streamRef, null, imageRef);
    requestInFlightRef.current = false;
    drawBoxes(overlayRef.current, activeMediaElement(), []);
    storeIdentifiedMatch(null);
    setBreathResult(null);
    setCountdown(0);
    setExhaleState(false);
    clearRecognitionFailure();
    setRunning(false);
    setMessage("Scan stopped.");
  }

  return (
    <div className="device-stack">
      <Panel
        eyebrow="Live Camera"
        title="Scan Face"
        actions={(
          <div className="button-row">
            <button className="button button-secondary" onClick={handleStart} disabled={running}>Start</button>
            <button className="button button-ghost" onClick={handleStop} type="button">Stop</button>
          </div>
        )}
      >
        <div className="alert info">{message}</div>
        <div className="video-frame">
          <img ref={imageRef} alt="Camera preview" crossOrigin="anonymous" />
          <canvas ref={overlayRef} className="overlay-canvas" />
          <canvas ref={canvasRef} hidden />
        </div>
      </Panel>

      <Panel eyebrow="Result" title="Breath Check">
        {identifiedMatch ? (
          <div className="result-grid">
            <div className="result-card result-card-success">
              <strong>{identifiedMatch.name}</strong>
              <div className="mono">Employee ID: {identifiedMatch.employee_code}</div>
              <div className="mono">Match Confidence: {Number(identifiedMatch.score).toFixed(3)}</div>
              <button
                className="button button-primary button-block"
                onClick={handleExhale}
                disabled={exhaling || !!breathResult}
                type="button"
              >
                {exhaling ? `Exhale ${countdown}s` : breathResult ? "Exhale Complete" : "Exhale"}
              </button>
            </div>

            {breathResult ? (
              <div className="result-card breath-readings-card">
                <div className="breath-readings-grid">
                  <div className="breath-reading-tile">
                    <span>Alcohol</span>
                    <strong>{Number(breathResult.alcohol_ppb).toFixed(1)} ppb</strong>
                    <div className={`pill ${breathResult.alcohol_clear ? "pill-pass" : "pill-fail"}`}>
                      Result: {breathResult.alcohol_clear ? "Yes" : "No"}
                    </div>
                  </div>

                  <div className="breath-reading-tile">
                    <span>Cannabis</span>
                    <strong>{Number(breathResult.cannabis_ppb).toFixed(1)} ppb</strong>
                    <div className={`pill ${breathResult.cannabis_clear ? "pill-pass" : "pill-fail"}`}>
                      Result: {breathResult.cannabis_clear ? "Yes" : "No"}
                    </div>
                  </div>
                </div>

                <div className={`pill ${breathResult.attendance_marked ? "pill-pass" : "pill-fail"}`}>
                  Attendance: {breathResult.attendance_marked ? "Marked" : "Not Marked"}
                </div>
              </div>
            ) : null}
          </div>
        ) : null}

        {!identifiedMatch && lastUnknown ? (
          <div className="result-card result-card-warning">
            <strong>User not confirmed</strong>
            <p>{lastReason || "No confirmed employee match was found for the scanned face."}</p>
          </div>
        ) : null}

        {!identifiedMatch && !lastUnknown ? (
          <div className="empty-state">The identified worker and breath result will appear here.</div>
        ) : null}
      </Panel>
    </div>
  );
}
