import { useEffect, useState } from "react";

export default function LoginView({
  authStatus,
  factoryName,
  onLogin,
  onSetup,
  onRequestUsernameRecovery,
  onVerifyUsernameRecovery,
  onRequestPasswordRecovery,
  onVerifyPasswordRecovery,
  error,
  message,
}) {
  const [mode, setMode] = useState(authStatus?.setup_required ? "setup" : "login");
  const [busy, setBusy] = useState(false);
  const [loginUsername, setLoginUsername] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [setupEmail, setSetupEmail] = useState("");
  const [setupUsername, setSetupUsername] = useState("");
  const [setupPassword, setSetupPassword] = useState("");
  const [setupConfirmPassword, setSetupConfirmPassword] = useState("");
  const [usernameRecovery, setUsernameRecovery] = useState({
    email: "",
    code: "",
  });
  const [passwordRecovery, setPasswordRecovery] = useState({
    email: "",
    code: "",
    newPassword: "",
    confirmPassword: "",
  });
  const [recoveredUsername, setRecoveredUsername] = useState("");

  useEffect(() => {
    setMode(authStatus?.setup_required ? "setup" : "login");
  }, [authStatus?.setup_required]);

  async function handleLoginSubmit(event) {
    event.preventDefault();
    setBusy(true);
    try {
      await onLogin(loginUsername, loginPassword);
    } finally {
      setBusy(false);
    }
  }

  async function handleSetupSubmit(event) {
    event.preventDefault();
    setBusy(true);
    try {
      await onSetup(setupUsername, setupEmail, setupPassword, setupConfirmPassword);
    } finally {
      setBusy(false);
    }
  }

  async function handleUsernameRecoveryRequest(event) {
    event.preventDefault();
    setBusy(true);
    try {
      await onRequestUsernameRecovery(usernameRecovery.email);
      setMode("forgot-username-verify");
    } finally {
      setBusy(false);
    }
  }

  async function handleUsernameRecoveryVerify(event) {
    event.preventDefault();
    setBusy(true);
    try {
      const response = await onVerifyUsernameRecovery(usernameRecovery.email, usernameRecovery.code);
      setRecoveredUsername(response.username);
      setLoginUsername(response.username);
      setLoginPassword("");
      setMode("login");
    } finally {
      setBusy(false);
    }
  }

  async function handlePasswordRecoveryRequest(event) {
    event.preventDefault();
    setBusy(true);
    try {
      await onRequestPasswordRecovery(passwordRecovery.email);
      setMode("forgot-password-verify");
    } finally {
      setBusy(false);
    }
  }

  async function handlePasswordRecoveryVerify(event) {
    event.preventDefault();
    setBusy(true);
    try {
      await onVerifyPasswordRecovery(
        passwordRecovery.email,
        passwordRecovery.code,
        passwordRecovery.newPassword,
        passwordRecovery.confirmPassword,
      );
      setPasswordRecovery((current) => ({
        ...current,
        code: "",
        newPassword: "",
        confirmPassword: "",
      }));
      setLoginUsername(recoveredUsername || loginUsername);
      setLoginPassword("");
      setMode("login");
    } finally {
      setBusy(false);
    }
  }

  function renderLoginForm() {
    return (
      <form className="form-stack" onSubmit={handleLoginSubmit}>
        <label>
          <span>Username</span>
          <input
            value={loginUsername}
            onChange={(event) => setLoginUsername(event.target.value)}
            placeholder="Username"
            required
          />
        </label>

        <label>
          <span>Password</span>
          <input
            type="password"
            value={loginPassword}
            onChange={(event) => setLoginPassword(event.target.value)}
            placeholder="Password"
            required
          />
        </label>

        <button className="button button-primary button-block" type="submit" disabled={busy}>
          {busy ? "Please wait..." : "Login"}
        </button>

        <div className="login-link-row">
          <button className="text-link" onClick={() => setMode("forgot-username-request")} type="button">
            Forgot username
          </button>
          <button className="text-link" onClick={() => setMode("forgot-password-request")} type="button">
            Forgot password
          </button>
        </div>
      </form>
    );
  }

  function renderSetupForm() {
    return (
      <form className="form-stack" onSubmit={handleSetupSubmit}>
        <label>
          <span>Email</span>
          <input
            type="email"
            value={setupEmail}
            onChange={(event) => setSetupEmail(event.target.value)}
            placeholder="Registered email"
            required
          />
        </label>

        <label>
          <span>Username</span>
          <input
            value={setupUsername}
            onChange={(event) => setSetupUsername(event.target.value)}
            placeholder="Username"
            required
          />
        </label>

        <label>
          <span>Password</span>
          <input
            type="password"
            value={setupPassword}
            onChange={(event) => setSetupPassword(event.target.value)}
            placeholder="Password"
            required
          />
        </label>

        <label>
          <span>Confirm Password</span>
          <input
            type="password"
            value={setupConfirmPassword}
            onChange={(event) => setSetupConfirmPassword(event.target.value)}
            placeholder="Confirm password"
            required
          />
        </label>

        <button className="button button-primary button-block" type="submit" disabled={busy}>
          {busy ? "Please wait..." : "Sign Up"}
        </button>
      </form>
    );
  }

  function renderForgotUsernameRequest() {
    return (
      <form className="form-stack" onSubmit={handleUsernameRecoveryRequest}>
        {renderRecoveryAvailabilityNote()}
        <label>
          <span>Registered Email</span>
          <input
            type="email"
            value={usernameRecovery.email}
            onChange={(event) => setUsernameRecovery((current) => ({ ...current, email: event.target.value }))}
            placeholder="Registered email"
            required
          />
        </label>

        <div className="button-row compact-actions">
          <button className="button button-primary" type="submit" disabled={busy}>
            {busy ? "Please wait..." : "Send Code"}
          </button>
          <button className="button button-ghost" onClick={() => setMode("login")} type="button">
            Back
          </button>
        </div>
      </form>
    );
  }

  function renderForgotUsernameVerify() {
    return (
      <form className="form-stack" onSubmit={handleUsernameRecoveryVerify}>
        <label>
          <span>Registered Email</span>
          <input
            type="email"
            value={usernameRecovery.email}
            onChange={(event) => setUsernameRecovery((current) => ({ ...current, email: event.target.value }))}
            placeholder="Registered email"
            required
          />
        </label>

        <label>
          <span>Code</span>
          <input
            value={usernameRecovery.code}
            onChange={(event) => setUsernameRecovery((current) => ({ ...current, code: event.target.value }))}
            placeholder="Verification code"
            required
          />
        </label>

        <div className="button-row compact-actions">
          <button className="button button-primary" type="submit" disabled={busy}>
            {busy ? "Please wait..." : "Show Username"}
          </button>
          <button className="button button-ghost" onClick={() => setMode("login")} type="button">
            Back
          </button>
        </div>
      </form>
    );
  }

  function renderForgotPasswordRequest() {
    return (
      <form className="form-stack" onSubmit={handlePasswordRecoveryRequest}>
        {renderRecoveryAvailabilityNote()}
        <label>
          <span>Registered Email</span>
          <input
            type="email"
            value={passwordRecovery.email}
            onChange={(event) => setPasswordRecovery((current) => ({ ...current, email: event.target.value }))}
            placeholder="Registered email"
            required
          />
        </label>

        <div className="button-row compact-actions">
          <button className="button button-primary" type="submit" disabled={busy}>
            {busy ? "Please wait..." : "Send Code"}
          </button>
          <button className="button button-ghost" onClick={() => setMode("login")} type="button">
            Back
          </button>
        </div>
      </form>
    );
  }

  function renderForgotPasswordVerify() {
    return (
      <form className="form-stack" onSubmit={handlePasswordRecoveryVerify}>
        <label>
          <span>Registered Email</span>
          <input
            type="email"
            value={passwordRecovery.email}
            onChange={(event) => setPasswordRecovery((current) => ({ ...current, email: event.target.value }))}
            placeholder="Registered email"
            required
          />
        </label>

        <label>
          <span>Code</span>
          <input
            value={passwordRecovery.code}
            onChange={(event) => setPasswordRecovery((current) => ({ ...current, code: event.target.value }))}
            placeholder="Verification code"
            required
          />
        </label>

        <label>
          <span>New Password</span>
          <input
            type="password"
            value={passwordRecovery.newPassword}
            onChange={(event) => setPasswordRecovery((current) => ({ ...current, newPassword: event.target.value }))}
            placeholder="New password"
            required
          />
        </label>

        <label>
          <span>Confirm Password</span>
          <input
            type="password"
            value={passwordRecovery.confirmPassword}
            onChange={(event) => setPasswordRecovery((current) => ({ ...current, confirmPassword: event.target.value }))}
            placeholder="Confirm password"
            required
          />
        </label>

        <div className="button-row compact-actions">
          <button className="button button-primary" type="submit" disabled={busy}>
            {busy ? "Please wait..." : "Reset Password"}
          </button>
          <button className="button button-ghost" onClick={() => setMode("login")} type="button">
            Back
          </button>
        </div>
      </form>
    );
  }

  function getTitle() {
    if (mode === "setup") {
      return "Sign Up";
    }
    if (mode === "forgot-username-request" || mode === "forgot-username-verify") {
      return "Forgot Username";
    }
    if (mode === "forgot-password-request" || mode === "forgot-password-verify") {
      return "Forgot Password";
    }
    return "Login";
  }

  function showPrimaryToggle() {
    // Sign Up only exists for first-time device setup. Once an account is
    // configured, offering the tab is a dead end ("already configured"), so
    // hide it entirely.
    return (mode === "login" || mode === "setup") && authStatus?.setup_required;
  }

  function renderRecoveryAvailabilityNote() {
    if (authStatus?.email_recovery_enabled) {
      return null;
    }
    return (
      <div className="alert info">
        Email recovery is not set up on this device. To recover access, run
        {" "}
        <code>python app.py reset-admin</code>
        {" "}
        from the device console (SSH), or ask your administrator to configure
        SMTP email settings.
      </div>
    );
  }

  function renderContent() {
    if (mode === "setup") {
      return renderSetupForm();
    }
    if (mode === "forgot-username-request") {
      return renderForgotUsernameRequest();
    }
    if (mode === "forgot-username-verify") {
      return renderForgotUsernameVerify();
    }
    if (mode === "forgot-password-request") {
      return renderForgotPasswordRequest();
    }
    if (mode === "forgot-password-verify") {
      return renderForgotPasswordVerify();
    }
    return renderLoginForm();
  }

  return (
    <div className="login-layout">
      <section className="login-brand-card">
        <div className="login-brand-top">
          <img className="login-logo" src="/tresenso-logo.png" alt="Tresenso Tech logo" />
          <div className="login-brand-copy">
            <div className="login-overline">Made by</div>
            <div className="login-company-name">Tresenso Tech Pvt Ltd</div>
          </div>
        </div>
        <h2 className="factory-name">{factoryName}</h2>
      </section>

      <section className="login-card minimal-login-card">
        <h3>{getTitle()}</h3>

        {showPrimaryToggle() ? (
          <div className="auth-toggle">
            <button
              className={`auth-toggle-button ${mode === "login" ? "active" : ""}`}
              onClick={() => setMode("login")}
              type="button"
            >
              Login
            </button>
            <button
              className={`auth-toggle-button ${mode === "setup" ? "active" : ""}`}
              onClick={() => setMode("setup")}
              type="button"
            >
              Sign Up
            </button>
          </div>
        ) : null}

        {recoveredUsername && mode === "login" ? (
          <div className="alert success">Username: {recoveredUsername}</div>
        ) : null}
        {message ? <div className="alert success">{message}</div> : null}
        {error ? <div className="alert error">{error}</div> : null}

        {renderContent()}
      </section>
    </div>
  );
}
