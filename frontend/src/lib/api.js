export class ApiError extends Error {
  constructor(message, status = 500) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const TOKEN_KEY = "attendance_operator_token";
const env = import.meta.env ?? {};

function isTauriRuntime() {
  if (typeof window === "undefined") {
    return false;
  }
  const protocol = window.location?.protocol || "";
  const hostname = window.location?.hostname || "";
  return protocol === "tauri:" || hostname === "tauri.localhost" || "__TAURI_INTERNALS__" in window;
}

const DEFAULT_API_BASE_URL = isTauriRuntime() ? "http://127.0.0.1:8000" : "";
const API_BASE_URL = (env.VITE_API_BASE_URL || DEFAULT_API_BASE_URL).trim().replace(/\/+$/, "");
const API_RETRY_COUNT = Math.max(
  1,
  Number.parseInt(env.VITE_API_RETRY_COUNT || (isTauriRuntime() ? "15" : "1"), 10) || 1,
);
const API_RETRY_DELAY_MS = Math.max(
  0,
  Number.parseInt(env.VITE_API_RETRY_DELAY_MS || (isTauriRuntime() ? "350" : "0"), 10) || 0,
);

function delay(ms) {
  return new Promise((resolve) => globalThis.setTimeout(resolve, ms));
}

function serviceUnavailableMessage() {
  if (isTauriRuntime()) {
    return "Could not reach the local desktop service. Restart the app and make sure the bundled backend can start.";
  }
  return "Could not reach the device service. Make sure the backend is running and that the desktop shell or frontend is pointing to the correct host and port.";
}

function resolveRequestUrl(path) {
  if (/^https?:\/\//i.test(path)) {
    return path;
  }
  return API_BASE_URL ? `${API_BASE_URL}${path}` : path;
}

async function parseApiResponse(response, requestUrl) {
  if (response.status === 204) {
    return null;
  }

  const contentType = response.headers.get("content-type") || "";
  const text = await response.text();
  if (!text) {
    return null;
  }

  const trimmedText = text.trim();
  const looksLikeJson = contentType.includes("application/json") || trimmedText.startsWith("{") || trimmedText.startsWith("[");
  if (looksLikeJson) {
    try {
      return JSON.parse(text);
    } catch (_parseError) {
      throw new ApiError(
        `The device service returned invalid JSON for ${requestUrl}. Check that the frontend and backend are served from the same project build.`,
        response.status || 500,
      );
    }
  }

  const looksLikeHtml = contentType.includes("text/html") || trimmedText.startsWith("<");
  if (looksLikeHtml) {
    throw new ApiError(
      "Expected JSON from the device service, but received HTML instead. Open the app from the active backend URL or configure the frontend API base URL/proxy.",
      response.status || 500,
    );
  }

  throw new ApiError(
    `Expected JSON from the device service, but received ${contentType || "a non-JSON response"} instead.`,
    response.status || 500,
  );
}

function resolveToken(token) {
  if (token) {
    return token;
  }
  return window.localStorage.getItem(TOKEN_KEY) || "";
}

async function request(path, options = {}) {
  const requestUrl = resolveRequestUrl(path);
  let response = null;
  for (let attempt = 1; attempt <= API_RETRY_COUNT; attempt += 1) {
    try {
      response = await fetch(requestUrl, {
        cache: "no-store",
        ...options,
        headers: {
          Accept: "application/json",
          ...(options.headers || {}),
        },
      });
      break;
    } catch (_requestError) {
      if (attempt >= API_RETRY_COUNT) {
        break;
      }
      if (API_RETRY_DELAY_MS > 0) {
        await delay(API_RETRY_DELAY_MS);
      }
    }
  }

  if (!response) {
    throw new ApiError(serviceUnavailableMessage(), 0);
  }

  const payload = await parseApiResponse(response, requestUrl);
  if (!response.ok) {
    const detail = payload && typeof payload === "object" ? payload.detail || payload.message : "";
    throw new ApiError(detail || "Request failed.", response.status);
  }
  return payload;
}

function buildHeaders(token, headers = {}) {
  const resolvedToken = resolveToken(token);
  return {
    ...(resolvedToken ? { Authorization: `Bearer ${resolvedToken}` } : {}),
    ...headers,
  };
}

export const apiClient = {
  getAuthStatus() {
    return request("/api/v2/auth/status");
  },
  login(username, password) {
    return request("/api/v2/auth/login", {
      method: "POST",
      headers: buildHeaders("", { "Content-Type": "application/json" }),
      body: JSON.stringify({ username, password }),
    });
  },
  setupCredentials(username, email, password, confirmPassword) {
    return request("/api/v2/auth/setup", {
      method: "POST",
      headers: buildHeaders("", { "Content-Type": "application/json" }),
      body: JSON.stringify({
        username,
        email,
        password,
        confirm_password: confirmPassword,
      }),
    });
  },
  resetCredentials(payload) {
    return request("/api/v2/auth/reset", {
      method: "POST",
      headers: buildHeaders("", { "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
  },
  me(token) {
    return request("/api/v2/auth/me", { headers: buildHeaders(token) });
  },
  get(path, token) {
    return request(path, { headers: buildHeaders(token) });
  },
  post(path, token, body = null, headers = {}) {
    return request(path, {
      method: "POST",
      headers: buildHeaders(token, headers),
      body,
    });
  },
  detectFaces(token, body) {
    return request("/api/v2/detections", {
      method: "POST",
      headers: buildHeaders(token),
      body,
    });
  },
  getLocalCameraStatus(token) {
    return request("/api/v2/local-camera/status", {
      headers: buildHeaders(token),
    });
  },
  startLocalCamera(token) {
    return request("/api/v2/local-camera/start", {
      method: "POST",
      headers: buildHeaders(token),
    });
  },
  stopLocalCamera(token) {
    return request("/api/v2/local-camera/stop", {
      method: "POST",
      headers: buildHeaders(token),
    });
  },
  localCameraFrameUrl(token, cacheBust = "") {
    const resolvedToken = resolveToken(token);
    const search = new URLSearchParams();
    if (resolvedToken) {
      search.set("token", resolvedToken);
    }
    if (cacheBust) {
      search.set("ts", String(cacheBust));
    }
    const suffix = search.toString();
    return resolveRequestUrl(`/api/v2/local-camera/frame${suffix ? `?${suffix}` : ""}`);
  },
  recognize(token, body) {
    return request("/api/v2/recognitions", {
      method: "POST",
      headers: buildHeaders(token),
      body,
    });
  },
  del(path, token) {
    return request(path, {
      method: "DELETE",
      headers: buildHeaders(token),
    });
  },
  requestUsernameRecovery(email) {
    return request("/api/v2/auth/recovery/username/request", {
      method: "POST",
      headers: buildHeaders("", { "Content-Type": "application/json" }),
      body: JSON.stringify({ email }),
    });
  },
  verifyUsernameRecovery(email, code) {
    return request("/api/v2/auth/recovery/username/verify", {
      method: "POST",
      headers: buildHeaders("", { "Content-Type": "application/json" }),
      body: JSON.stringify({ email, code }),
    });
  },
  requestPasswordRecovery(email) {
    return request("/api/v2/auth/recovery/password/request", {
      method: "POST",
      headers: buildHeaders("", { "Content-Type": "application/json" }),
      body: JSON.stringify({ email }),
    });
  },
  verifyPasswordRecovery(email, code, newPassword, confirmPassword) {
    return request("/api/v2/auth/recovery/password/verify", {
      method: "POST",
      headers: buildHeaders("", { "Content-Type": "application/json" }),
      body: JSON.stringify({
        email,
        code,
        new_password: newPassword,
        confirm_password: confirmPassword,
      }),
    });
  },
  runBreathTest(token, payload) {
    return request("/api/v2/breath-tests", {
      method: "POST",
      headers: buildHeaders(token, { "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
  },
  startBreathTestSession(token, payload) {
    return request("/api/v2/breath-tests/start", {
      method: "POST",
      headers: buildHeaders(token, { "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
  },
  completeBreathTestSession(token, payload) {
    return request("/api/v2/breath-tests/complete", {
      method: "POST",
      headers: buildHeaders(token, { "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
  },
  cancelBreathTestSession(token, sessionId) {
    return request(`/api/v2/breath-tests/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
      headers: buildHeaders(token),
    });
  },
};
