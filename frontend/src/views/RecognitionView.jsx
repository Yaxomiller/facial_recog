import { useEffect, useRef, useState } from "react";
import Panel from "../components/Panel";
import { apiClient, ApiError } from "../lib/api";
import { drawBoxes, frameToBlob, startUserCamera, stopLive } from "../lib/media";

const CAMERA_ID = "device-front-camera";
const EXHALE_SECONDS = 10;
const DETECTION_INTERVAL_MS = 500;
const RECOGNITION_INTERVAL_MS = 1200;
const EXHALE_FACE_CHECK_INTERVAL_MS = 3000;
const STABLE_SINGLE_FACE_FRAMES = 2;
const DETECTION_FRAME_MAX_WIDTH = 960;
const RECOGNITION_FRAME_MAX_WIDTH = 1280;

export default function RecognitionView({ token, onUpdated, onSessionExpired = () => {} }) {
  const [message, setMessage] = useState("Press start to scan a face.");
  const [identifiedMatch, setIdentifiedMatch] = useState(null);
  const [breathResult, setBreathResult] = useState(null);
  const [running, setRunning] = useState(false);
  const [exhaling, setExhaling] = useState(false);
  const [countdown, setCountdown] = useState(0);
  const [lastUnknown, setLastUnknown] = useState(false);
  const [lastReason, setLastReason] = useState("");
  const videoRef = useRef(null);
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
  const stableSingleFaceFramesRef = useRef(0);
  const lastRecognitionAttemptRef = useRef(0);

  useEffect(() => {
    exhalingRef.current = exhaling;
  }, [exhaling]);

  useEffect(() => {
    identifiedMatchRef.current = identifiedMatch;
  }, [identifiedMatch]);

  useEffect(() => () => {
    clearExhaleTimer();
    clearExhaleCheckTimer();
    stopLive(timerRef, streamRef, videoRef);
  }, []);

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
  }

  async function captureFrameBlob(options) {
    if (!videoRef.current || videoRef.current.readyState < 2) {
      return null;
    }

    return frameToBlob(videoRef.current, canvasRef.current, options);
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
    drawBoxes(overlayRef.current, videoRef.current, result.boxes || []);
    return result;
  }

  async function captureRecognitionFrame() {
    const blob = await captureFrameBlob({
      maxWidth: RECOGNITION_FRAME_MAX_WIDTH,
      quality: 0.88,
    });
    if (!blob) {
      return null;
    }

    const form = new FormData();
    form.append("camera_id", CAMERA_ID);
    form.append("top_k", "3");
    form.append("image", blob, "scan.jpg");
    const result = await apiClient.recognize(token, form);
    drawBoxes(overlayRef.current, videoRef.current, result.boxes || []);
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

  function startScanLoop() {
    clearScanTimer();
    const runScan = async () => {
      if (requestInFlightRef.current || exhalingRef.current || !streamRef.current) {
        return;
      }

      requestInFlightRef.current = true;
      try {
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

        if (stableSingleFaceFramesRef.current < STABLE_SINGLE_FACE_FRAMES) {
          return;
        }

        const now = Date.now();
        if (now - lastRecognitionAttemptRef.current < RECOGNITION_INTERVAL_MS) {
          return;
        }

        lastRecognitionAttemptRef.current = now;
        const result = await captureRecognitionFrame();
        if (!result) {
          return;
        }

        if (result.matches?.length) {
          clearScanTimer();
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
          handleRecognitionFailure(
            firstDebugReason(result, "Face detected, but no valid employee match was confirmed."),
          );
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
          }, DETECTION_INTERVAL_MS);
        }
      }
    };

    void runScan();
  }

  function restartScanningAfterFaceMismatch(reason) {
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
      handleStart();
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
      setMessage("Camera started. Hold the face steady and look at the screen.");
      await startUserCamera(videoRef, streamRef);
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

    exhaleCancelledRef.current = false;
    setBreathResult(null);
    setExhaleState(true);
    setCountdown(EXHALE_SECONDS);
    clearRecognitionFailure();
    setMessage("Exhale now. Keep your face in view until the timer finishes.");

    exhaleCheckTimerRef.current = window.setInterval(() => {
      void verifyExhaleIdentity(identifiedMatchRef.current || matchToVerify);
    }, EXHALE_FACE_CHECK_INTERVAL_MS);

    let remaining = EXHALE_SECONDS;
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

        const result = await apiClient.runBreathTest(token, {
          worker_id: verifiedMatch.worker_id,
          camera_id: CAMERA_ID,
          matched_score: verifiedMatch.score,
        });
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
        const text = requestError instanceof Error ? requestError.message : "Breath test failed.";
        setMessage(text);
      } finally {
        requestInFlightRef.current = false;
        setExhaleState(false);
        if (!exhaleCancelledRef.current) {
          drawBoxes(overlayRef.current, videoRef.current, []);
          stopLive(timerRef, streamRef, videoRef);
          setRunning(false);
        }
      }
    }, 1000);
  }

  function handleStop() {
    clearExhaleTimer();
    clearExhaleCheckTimer();
    exhaleCancelledRef.current = true;
    resetScanProgress();
    stopLive(timerRef, streamRef, videoRef);
    requestInFlightRef.current = false;
    drawBoxes(overlayRef.current, videoRef.current, []);
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
          <video ref={videoRef} autoPlay playsInline muted />
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
