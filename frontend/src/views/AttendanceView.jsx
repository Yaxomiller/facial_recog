import Panel from "../components/Panel";
import DataTable from "../components/DataTable";
import { formatServerDate, formatServerTime } from "../lib/datetime";

export default function AttendanceView({ attendance }) {
  return (
    <Panel eyebrow="Attendance Records" title="Attendance History">
      <DataTable
        headers={["Name", "Employee ID", "Date", "Time", "Alcohol", "Cannabis", "Attendance"]}
        rows={attendance.map((row) => [
          row.name,
          row.employee_code,
          formatServerDate(row.created_at),
          formatServerTime(row.created_at),
          row.alcohol_clear ? "Yes" : "No",
          row.cannabis_clear ? "Yes" : "No",
          row.attendance_marked ? "Marked" : "Not Marked",
        ])}
        emptyLabel="No attendance checks recorded yet."
      />
    </Panel>
  );
}
