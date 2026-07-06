import { useEffect, useState } from "react";

export default function LoginView({
  authStatus,
  factoryName,
  onLogin,
  onSetup,
  onReregister,
  onRequestReregisterCode,
  onRequestUsernameRecovery,
  onVerifyUsernameRecovery,
  onRequestPasswordRecovery,
  onVerifyPasswordRecovery,
  onRecoveryCodeReset,
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
  const [codeReset, setCodeReset] = useState({
    code: "",
    newPassword: "",
    confirmPassword: "",
  });
  const [reregister, setReregister] = useState({
    registeredEmail: "",
    code: "",
    codeSent: false,
  });

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
      if (authStatus?.configured) {
        await onReregister(
          setupUsername,
          setupEmail,
          setupPassword,
          setupConfirmPassword,
          reregister.code,
        );
      } else {
        await onSetup(setupUsername, setupEmail, setupPassword, setupConfirmPassword);
      }
    } finally {
      setBusy(false);
    }
  }

  async function handleSendReregisterCode() {
    if (!reregister.registeredEmail) {
      return;
    }
    setBusy(true);
    try {
      await onRequestReregisterCode(reregister.registeredEmail);
      setReregister((current) => ({ ...current, codeSent: true }));
    } catch (_requestError) {
      // The error banner is populated by the parent handler.
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

  async function handleRecoveryCodeSubmit(event) {
    event.preventDefault();
    setBusy(true);
    try {
      const response = await onRecoveryCodeReset(
        codeReset.code,
        codeReset.newPassword,
        codeReset.confirmPassword,
      );
      setCodeReset({ code: "", newPassword: "", confirmPassword: "" });
      if (response?.username) {
        setLoginUsername(response.username);
      }
      setLoginPassword("");
      setMode("login");
    } catch (_requestError) {
      // The error banner is populated by the parent handler.
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
    const isReregister = Boolean(authStatus?.configured);
    return (
      <form className="form-stack" onSubmit={handleSetupSubmit}>
        {isReregister ? (
          <div className="alert info">
            This device already has an account. Signing up replaces it, so you
            must verify ownership below with an emailed code or a saved
            recovery code.
          </div>
        ) : null}
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

        {isReregister ? (
          <>
            {authStatus?.email_recovery_enabled ? (
              <label>
                <span>Registered Email (to receive a code)</span>
                <div className="input-with-action">
                  <input
                    type="email"
                    value={reregister.registeredEmail}
                    onChange={(event) =>
                      setReregister((current) => ({ ...current, registeredEmail: event.target.value }))
                    }
                    placeholder="Currently registered email"
                  />
                  <button
                    className="button button-secondary"
                    onClick={handleSendReregisterCode}
                    type="button"
                    disabled={busy || !reregister.registeredEmail}
                  >
                    {reregister.codeSent ? "Resend" : "Send Code"}
                  </button>
                </div>
              </label>
            ) : (
              <div className="alert info">
                Email recovery is not configured, so use one of your saved
                recovery codes as the verification code below.
              </div>
            )}

            <label>
              <span>Verification Code</span>
              <input
                value={reregister.code}
                onChange={(event) => setReregister((current) => ({ ...current, code: event.target.value }))}
                placeholder="Emailed code or recovery code"
                required
              />
            </label>
          </>
        ) : null}

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

        <div className="login-link-row">
          <button className="text-link" onClick={() => setMode("recovery-code")} type="button">
            Use a recovery code instead
          </button>
        </div>
      </form>
    );
  }

  function renderRecoveryCodeReset() {
    return (
      <form className="form-stack" onSubmit={handleRecoveryCodeSubmit}>
        <div className="alert info">
          Enter one of the recovery codes you saved when the account was created
          (or generated from the Security screen). Each code works once.
        </div>
        <label>
          <span>Recovery Code</span>
          <input
            value={codeReset.code}
            onChange={(event) => setCodeReset((current) => ({ ...current, code: event.target.value }))}
            placeholder="e.g. 9F3A-61BC"
            required
          />
        </label>
        <label>
          <span>New Password</span>
          <input
            type="password"
            value={codeReset.newPassword}
            onChange={(event) => setCodeReset((current) => ({ ...current, newPassword: event.target.value }))}
            placeholder="New password"
            required
          />
        </label>
        <label>
          <span>Confirm Password</span>
          <input
            type="password"
            value={codeReset.confirmPassword}
            onChange={(event) => setCodeReset((current) => ({ ...current, confirmPassword: event.target.value }))}
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
    if (mode === "recovery-code") {
      return "Recovery Code";
    }
    return "Login";
  }

  function showPrimaryToggle() {
    return mode === "login" || mode === "setup";
  }

  function renderRecoveryAvailabilityNote() {
    if (authStatus?.email_recovery_enabled) {
      return null;
    }
    return (
      <div className="alert info">
        Email recovery is not set up on this device. Use one of your saved
        recovery codes instead ("Use a recovery code" below). If you have no
        codes, an administrator can reset the login from the device console
        with <code>python app.py reset-admin</code>.
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
    if (mode === "recovery-code") {
      return renderRecoveryCodeReset();
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
