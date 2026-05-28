import { useEffect, useState } from "react";
import { apiClient, ApiError } from "./lib/api";
import LoginView from "./views/LoginView";
import OverviewView from "./views/OverviewView";
import EnrollView from "./views/EnrollView";
import RecognitionView from "./views/RecognitionView";
import WorkersView from "./views/WorkersView";
import AttendanceView from "./views/AttendanceView";

const TOKEN_KEY = "attendance_operator_token";
const FACTORY_NAME = import.meta.env.VITE_FACTORY_NAME || "Factory Name";

const VIEW_TITLES = {
  overview: {
    title: "Home",
    subtitle: "Choose one action to continue.",
  },
  enroll: {
    title: "New User",
    subtitle: "Add employee details and capture photo.",
  },
  recognize: {
    title: "Scan",
    subtitle: "Scan a face, run the breath test, and mark attendance.",
  },
  workers: {
    title: "Database",
    subtitle: "View registered employees.",
  },
  attendance: {
    title: "Attendance History",
    subtitle: "View breath checks and attendance records.",
  },
};

export default function App() {
  const [token, setToken] = useState(() => window.localStorage.getItem(TOKEN_KEY) || "");
  const [view, setView] = useState("overview");
  const [session, setSession] = useState(null);
  const [authStatus, setAuthStatus] = useState({
    configured: false,
    setup_required: true,
    source: "none",
    email_configured: false,
    email_recovery_enabled: false,
  });
  const [status, setStatus] = useState(null);
  const [workers, setWorkers] = useState([]);
  const [attendance, setAttendance] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (token) {
      void loadDeviceData(token);
      return;
    }
    setSession(null);
    setStatus(null);
    setWorkers([]);
    setAttendance([]);
    void loadAuthStatus();
  }, [token]);

  function resetSessionState(nextError = "") {
    window.localStorage.removeItem(TOKEN_KEY);
    setToken("");
    setSession(null);
    setStatus(null);
    setWorkers([]);
    setAttendance([]);
    setView("overview");
    setError(nextError);
    setMessage("");
  }

  async function loadAuthStatus() {
    setLoading(true);
    try {
      const authState = await apiClient.getAuthStatus();
      setAuthStatus(authState);
      setError("");
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : "Could not load login state.";
      setError(message);
    } finally {
      setLoading(false);
    }
  }

  async function loadDeviceData(activeToken = token, silent = false) {
    if (!activeToken) {
      return;
    }

    if (silent) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }

    try {
      const me = await apiClient.me(activeToken);
      const [statusData, workersData, attendanceData] = await Promise.all([
        apiClient.get("/api/v2/status", activeToken),
        apiClient.get("/api/v2/workers", activeToken),
        apiClient.get("/api/v2/attendance", activeToken),
      ]);
      setSession(me);
      setStatus(statusData);
      setWorkers(workersData);
      setAttendance(attendanceData);
      setAuthStatus({ configured: true, setup_required: false, source: "session" });
      setError("");
      setMessage("");
    } catch (requestError) {
      const nextError = requestError instanceof ApiError && requestError.status === 401
        ? "Session expired. Please log in again."
        : requestError instanceof Error
          ? requestError.message
          : "Could not load the device data.";
      resetSessionState(nextError);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  function storeSession(loginResponse) {
    window.localStorage.setItem(TOKEN_KEY, loginResponse.token);
    setToken(loginResponse.token);
    setView("overview");
    setMessage("");
  }

  async function handleLogin(username, password) {
    setError("");
    setMessage("");
    try {
      const response = await apiClient.login(username, password);
      storeSession(response);
    } catch (requestError) {
      if (requestError instanceof ApiError) {
        setError(requestError.message);
        return;
      }
      throw requestError;
    }
  }

  async function handleSetup(username, email, password, confirmPassword) {
    setError("");
    setMessage("");
    try {
      const response = await apiClient.setupCredentials(username, email, password, confirmPassword);
      storeSession(response);
    } catch (requestError) {
      if (requestError instanceof ApiError) {
        setError(requestError.message);
        return;
      }
      throw requestError;
    }
  }

  async function handleReset(payload) {
    setError("");
    setMessage("");
    try {
      const response = await apiClient.resetCredentials(payload);
      window.localStorage.removeItem(TOKEN_KEY);
      setToken("");
      setSession(null);
      setStatus(null);
      setWorkers([]);
      setAttendance([]);
      setView("overview");
      setMessage(response.message);
    } catch (requestError) {
      if (requestError instanceof ApiError) {
        setError(requestError.message);
        return;
      }
      throw requestError;
    }
  }

  async function handleRequestUsernameRecovery(email) {
    setError("");
    setMessage("");
    try {
      const response = await apiClient.requestUsernameRecovery(email);
      setMessage(response.message);
      return response;
    } catch (requestError) {
      if (requestError instanceof ApiError) {
        setError(requestError.message);
        throw requestError;
      }
      throw requestError;
    }
  }

  async function handleVerifyUsernameRecovery(email, code) {
    setError("");
    setMessage("");
    try {
      const response = await apiClient.verifyUsernameRecovery(email, code);
      setMessage(response.message);
      return response;
    } catch (requestError) {
      if (requestError instanceof ApiError) {
        setError(requestError.message);
        throw requestError;
      }
      throw requestError;
    }
  }

  async function handleRequestPasswordRecovery(email) {
    setError("");
    setMessage("");
    try {
      const response = await apiClient.requestPasswordRecovery(email);
      setMessage(response.message);
      return response;
    } catch (requestError) {
      if (requestError instanceof ApiError) {
        setError(requestError.message);
        throw requestError;
      }
      throw requestError;
    }
  }

  async function handleVerifyPasswordRecovery(email, code, newPassword, confirmPassword) {
    setError("");
    setMessage("");
    try {
      const response = await apiClient.verifyPasswordRecovery(email, code, newPassword, confirmPassword);
      setMessage(response.message);
      return response;
    } catch (requestError) {
      if (requestError instanceof ApiError) {
        setError(requestError.message);
        throw requestError;
      }
      throw requestError;
    }
  }

  async function handleLogout() {
    if (token) {
      try {
        await apiClient.post("/api/v2/auth/logout", token);
      } catch {}
    }
    resetSessionState("");
  }

  async function handleRefresh() {
    if (session) {
      await loadDeviceData(token, true);
      return;
    }
    await loadAuthStatus();
  }

  function handleSessionExpired(nextError = "Session expired. Please log in again.") {
    resetSessionState(nextError);
  }

  const viewMeta = VIEW_TITLES[view] ?? VIEW_TITLES.overview;

  return (
    <div className="device-shell">
      <main className="device-screen">
        {loading ? (
          <div className="loading-shell">
            <div className="loading-orb" />
            <div className="loading-copy">Loading device...</div>
          </div>
        ) : null}

        {!loading && !session ? (
          <LoginView
            authStatus={authStatus}
            factoryName={FACTORY_NAME}
            onLogin={handleLogin}
            onSetup={handleSetup}
            onReset={handleReset}
            onRequestUsernameRecovery={handleRequestUsernameRecovery}
            onVerifyUsernameRecovery={handleVerifyUsernameRecovery}
            onRequestPasswordRecovery={handleRequestPasswordRecovery}
            onVerifyPasswordRecovery={handleVerifyPasswordRecovery}
            error={error}
            message={message}
          />
        ) : null}

        {!loading && session ? (
          <>
            <header className="device-header">
              <div className="device-header-main-row">
                <div>
                  <div className="device-brand-line">Tresenso Face Attendance</div>
                  <div className="device-header-title-row">
                    <h1>{viewMeta.title}</h1>
                    {view === "overview" ? (
                      <div className="header-employee-pill">
                        <span>Reg Emp</span>
                        <strong>{status?.indexed_workers ?? 0}</strong>
                      </div>
                    ) : null}
                  </div>
                  <p>{viewMeta.subtitle}</p>
                </div>

                {view !== "overview" ? (
                  <button className="button button-ghost button-home-corner" onClick={() => setView("overview")}>
                    Home
                  </button>
                ) : null}
              </div>

              {view === "overview" ? (
                <div className="device-header-actions">
                  <button className="button button-ghost" onClick={handleRefresh}>
                    {refreshing ? "Refreshing..." : "Refresh"}
                  </button>
                  <button className="button button-dark" onClick={handleLogout}>Logout</button>
                </div>
              ) : null}
            </header>

            {error ? <div className="alert error">{error}</div> : null}

            {view === "overview" ? (
              <OverviewView onJump={setView} />
            ) : null}

            {view === "enroll" ? (
              <EnrollView token={token} onUpdated={() => loadDeviceData(token, true)} />
            ) : null}

            {view === "recognize" ? (
              <RecognitionView
                token={token}
                onUpdated={() => loadDeviceData(token, true)}
                onSessionExpired={handleSessionExpired}
              />
            ) : null}

            {view === "workers" ? (
              <WorkersView
                token={token}
                workers={workers}
                onUpdated={() => loadDeviceData(token, true)}
                onSessionExpired={handleSessionExpired}
              />
            ) : null}

            {view === "attendance" ? (
              <AttendanceView attendance={attendance} />
            ) : null}
          </>
        ) : null}
      </main>

      <footer className="device-footer">Made by Tresenso Tech Pvt Ltd</footer>
    </div>
  );
}
