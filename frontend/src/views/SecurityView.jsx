import { useEffect, useState } from "react";
import Panel from "../components/Panel";
import { apiClient, ApiError } from "../lib/api";

export default function SecurityView({ token, session, onCredentialsChanged, onSessionExpired = () => {} }) {
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState({ tone: "", text: "" });
  const [changeForm, setChangeForm] = useState({
    currentPassword: "",
    newPassword: "",
    confirmPassword: "",
  });
  const [codesRemaining, setCodesRemaining] = useState(null);
  const [codesPassword, setCodesPassword] = useState("");
  const [freshCodes, setFreshCodes] = useState([]);
  const [emailForm, setEmailForm] = useState({
    recoveryEmail: "",
    host: "",
    port: "",
    username: "",
    password: "",
    fromEmail: "",
    useTls: true,
    useSsl: false,
    hasPassword: false,
    configured: false,
    currentPassword: "",
  });

  useEffect(() => {
    let active = true;
    apiClient
      .recoveryCodesStatus(token)
      .then((status) => {
        if (active) {
          setCodesRemaining(status.remaining);
        }
      })
      .catch((requestError) => {
        if (active && requestError instanceof ApiError && requestError.status === 401) {
          onSessionExpired();
        }
      });
    apiClient
      .getEmailSettings(token)
      .then((settings) => {
        if (active) {
          setEmailForm((current) => ({
            ...current,
            recoveryEmail: settings.recovery_email || "",
            host: settings.host || "",
            port: settings.port || "",
            username: settings.username || "",
            fromEmail: settings.from_email || "",
            useTls: settings.use_tls !== false,
            useSsl: Boolean(settings.use_ssl),
            hasPassword: Boolean(settings.has_password),
            configured: Boolean(settings.configured),
          }));
        }
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [token]);

  function showFeedback(tone, text) {
    setFeedback({ tone, text });
  }

  async function handleChangePassword(event) {
    event.preventDefault();
    if (changeForm.newPassword !== changeForm.confirmPassword) {
      showFeedback("error", "New passwords do not match.");
      return;
    }
    setBusy(true);
    try {
      const response = await apiClient.resetCredentials({
        username: session?.username || "",
        current_password: changeForm.currentPassword,
        new_password: changeForm.newPassword,
        confirm_password: changeForm.confirmPassword,
      });
      onCredentialsChanged(response.message);
    } catch (requestError) {
      showFeedback("error", requestError instanceof Error ? requestError.message : "Password change failed.");
    } finally {
      setBusy(false);
    }
  }

  async function handleGenerateCodes(event) {
    event.preventDefault();
    setBusy(true);
    try {
      const response = await apiClient.generateRecoveryCodes(token, codesPassword);
      setFreshCodes(response.codes);
      setCodesRemaining(response.remaining);
      setCodesPassword("");
      showFeedback(
        "success",
        "New recovery codes generated. Save them now — they are shown only once and replace all previous codes.",
      );
    } catch (requestError) {
      if (requestError instanceof ApiError && requestError.status === 401) {
        onSessionExpired();
        return;
      }
      showFeedback("error", requestError instanceof Error ? requestError.message : "Could not generate codes.");
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveEmailSettings(event) {
    event.preventDefault();
    setBusy(true);
    try {
      if (emailForm.recoveryEmail) {
        await apiClient.updateRecoveryEmail(token, emailForm.currentPassword, emailForm.recoveryEmail);
      }
      await apiClient.saveEmailSettings(token, {
        current_password: emailForm.currentPassword,
        host: emailForm.host,
        port: emailForm.port,
        username: emailForm.username,
        password: emailForm.password,
        from_email: emailForm.fromEmail,
        use_tls: emailForm.useTls,
        use_ssl: emailForm.useSsl,
      });
      setEmailForm((current) => ({
        ...current,
        password: "",
        currentPassword: "",
        configured: Boolean(current.host && current.fromEmail),
        hasPassword: current.hasPassword || Boolean(current.password),
      }));
      showFeedback("success", "Email recovery settings saved. Send a test email to confirm delivery.");
    } catch (requestError) {
      if (requestError instanceof ApiError && requestError.status === 401) {
        onSessionExpired();
        return;
      }
      showFeedback("error", requestError instanceof Error ? requestError.message : "Could not save email settings.");
    } finally {
      setBusy(false);
    }
  }

  async function handleTestEmail() {
    setBusy(true);
    try {
      const response = await apiClient.testEmailSettings(token);
      showFeedback("success", response.message);
    } catch (requestError) {
      showFeedback("error", requestError instanceof Error ? requestError.message : "Test email failed.");
    } finally {
      setBusy(false);
    }
  }

  async function copyCodes() {
    try {
      await navigator.clipboard.writeText(freshCodes.join("\n"));
      showFeedback("success", "Recovery codes copied to the clipboard.");
    } catch (_copyError) {
      showFeedback("error", "Could not copy automatically. Write the codes down instead.");
    }
  }

  return (
    <div className="device-stack">
      {feedback.text ? <div className={`alert ${feedback.tone}`}>{feedback.text}</div> : null}

      <Panel eyebrow="Credentials" title="Change Password">
        <form className="form-stack" onSubmit={handleChangePassword}>
          <label>
            <span>Current Password</span>
            <input
              type="password"
              value={changeForm.currentPassword}
              onChange={(event) => setChangeForm((current) => ({ ...current, currentPassword: event.target.value }))}
              placeholder="Current password"
              required
            />
          </label>
          <label>
            <span>New Password</span>
            <input
              type="password"
              value={changeForm.newPassword}
              onChange={(event) => setChangeForm((current) => ({ ...current, newPassword: event.target.value }))}
              placeholder="New password"
              required
            />
          </label>
          <label>
            <span>Confirm New Password</span>
            <input
              type="password"
              value={changeForm.confirmPassword}
              onChange={(event) => setChangeForm((current) => ({ ...current, confirmPassword: event.target.value }))}
              placeholder="Confirm new password"
              required
            />
          </label>
          <button className="button button-primary button-block" type="submit" disabled={busy}>
            {busy ? "Please wait..." : "Change Password"}
          </button>
        </form>
      </Panel>

      <Panel eyebrow="Account Recovery" title="Email Recovery">
        <p className="panel-help-text">
          {emailForm.configured
            ? "Email recovery is configured. Codes for password reset and sign-up verification are sent to the registered email."
            : "Configure an SMTP mail account (for Gmail: enable 2-step verification, then create an App Password) so recovery codes can be emailed."}
        </p>
        <form className="form-stack" onSubmit={handleSaveEmailSettings}>
          <label>
            <span>Registered Recovery Email</span>
            <input
              type="email"
              value={emailForm.recoveryEmail}
              onChange={(event) => setEmailForm((current) => ({ ...current, recoveryEmail: event.target.value }))}
              placeholder="Where recovery codes are sent"
            />
          </label>
          <label>
            <span>SMTP Host</span>
            <input
              value={emailForm.host}
              onChange={(event) => setEmailForm((current) => ({ ...current, host: event.target.value }))}
              placeholder="e.g. smtp.gmail.com"
            />
          </label>
          <label>
            <span>SMTP Port</span>
            <input
              value={emailForm.port}
              onChange={(event) => setEmailForm((current) => ({ ...current, port: event.target.value }))}
              placeholder="587"
            />
          </label>
          <label>
            <span>SMTP Username</span>
            <input
              value={emailForm.username}
              onChange={(event) => setEmailForm((current) => ({ ...current, username: event.target.value }))}
              placeholder="Usually the sending email address"
            />
          </label>
          <label>
            <span>SMTP Password</span>
            <input
              type="password"
              value={emailForm.password}
              onChange={(event) => setEmailForm((current) => ({ ...current, password: event.target.value }))}
              placeholder={emailForm.hasPassword ? "Saved (leave blank to keep)" : "For Gmail, use an App Password"}
            />
          </label>
          <label>
            <span>From Email</span>
            <input
              type="email"
              value={emailForm.fromEmail}
              onChange={(event) => setEmailForm((current) => ({ ...current, fromEmail: event.target.value }))}
              placeholder="Sender address, e.g. the Gmail address"
            />
          </label>
          <label>
            <span>Current Password (to authorize changes)</span>
            <input
              type="password"
              value={emailForm.currentPassword}
              onChange={(event) => setEmailForm((current) => ({ ...current, currentPassword: event.target.value }))}
              placeholder="Your login password"
              required
            />
          </label>
          <div className="button-row compact-actions">
            <button className="button button-primary" type="submit" disabled={busy}>
              {busy ? "Please wait..." : "Save Email Settings"}
            </button>
            <button
              className="button button-secondary"
              onClick={handleTestEmail}
              type="button"
              disabled={busy || !emailForm.configured}
            >
              Send Test Email
            </button>
          </div>
        </form>
      </Panel>

      <Panel eyebrow="Account Recovery" title="Recovery Codes">
        <p className="panel-help-text">
          Recovery codes let you reset a forgotten password from the login screen — no email needed.
          Each code works once. Generating new codes replaces all old ones.
          {codesRemaining !== null ? ` Codes remaining: ${codesRemaining}.` : ""}
        </p>

        {freshCodes.length ? (
          <div className="recovery-codes-box">
            <ul className="recovery-codes-list">
              {freshCodes.map((code) => (
                <li key={code} className="mono">{code}</li>
              ))}
            </ul>
            <button className="button button-secondary button-block" onClick={copyCodes} type="button">
              Copy Codes
            </button>
          </div>
        ) : null}

        <form className="form-stack" onSubmit={handleGenerateCodes}>
          <label>
            <span>Current Password</span>
            <input
              type="password"
              value={codesPassword}
              onChange={(event) => setCodesPassword(event.target.value)}
              placeholder="Confirm your password to generate codes"
              required
            />
          </label>
          <button className="button button-primary button-block" type="submit" disabled={busy}>
            {busy ? "Please wait..." : freshCodes.length || codesRemaining ? "Generate New Codes" : "Generate Recovery Codes"}
          </button>
        </form>
      </Panel>
    </div>
  );
}
