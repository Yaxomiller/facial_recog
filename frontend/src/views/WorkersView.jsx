import { useState } from "react";
import Panel from "../components/Panel";
import DataTable from "../components/DataTable";
import { apiClient, ApiError } from "../lib/api";

export default function WorkersView({ token, workers, onUpdated, onSessionExpired = () => {} }) {
  const [busyCode, setBusyCode] = useState("");
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState("info");

  async function handleRemove(worker) {
    const confirmed = window.confirm(`Remove ${worker.name} (${worker.employee_code}) from the database?`);
    if (!confirmed) {
      return;
    }

    setBusyCode(worker.employee_code);
    setMessage("");
    setMessageTone("info");
    try {
      await apiClient.del(`/api/v2/workers/${encodeURIComponent(worker.employee_code)}`, token);
      setMessage(`${worker.name} was removed from the database.`);
      setMessageTone("success");
      await onUpdated();
    } catch (requestError) {
      if (requestError instanceof ApiError && requestError.status === 401) {
        onSessionExpired("Session expired while removing a user. Please log in again.");
        return;
      }
      const text = requestError instanceof Error ? requestError.message : "Could not remove the employee.";
      setMessage(text);
      setMessageTone("error");
    } finally {
      setBusyCode("");
    }
  }

  return (
    <Panel eyebrow="Registered Users" title="Employee Database">
      {message ? <div className={`alert ${messageTone}`}>{message}</div> : null}

      <DataTable
        headers={["Employee ID", "Name", "Action"]}
        rows={workers.map((worker) => [
          worker.employee_code,
          worker.name,
          <button
            className="button button-danger button-small"
            key={worker.employee_code}
            onClick={() => handleRemove(worker)}
            disabled={busyCode === worker.employee_code}
          >
            {busyCode === worker.employee_code ? "Removing..." : "Remove"}
          </button>,
        ])}
        emptyLabel="No employees have been added yet."
      />
    </Panel>
  );
}
