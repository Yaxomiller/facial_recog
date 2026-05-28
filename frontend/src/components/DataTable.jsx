export default function DataTable({ headers, rows = [], emptyLabel = "No records found." }) {
  return (
    <div className="table-shell">
      <table className="table">
        <thead>
          <tr>
            {headers.map((header) => <th key={header}>{header}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.length ? rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((cell, cellIndex) => (
                <td key={cellIndex} data-label={headers[cellIndex]}>
                  {cell}
                </td>
              ))}
            </tr>
          )) : (
            <tr>
              <td colSpan={headers.length} data-label="Status">{emptyLabel}</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
