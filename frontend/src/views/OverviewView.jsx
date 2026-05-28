const TILES = [
  {
    key: "enroll",
    label: "New User",
    description: "Add employee details.",
  },
  {
    key: "recognize",
    label: "Scan",
    description: "Scan face and run breath test.",
  },
  {
    key: "workers",
    label: "Database",
    description: "View registered employee list.",
  },
  {
    key: "attendance",
    label: "Attendance History",
    description: "Check breath and attendance records.",
  },
];

export default function OverviewView({ onJump }) {
  return (
    <div className="device-stack">
      <section className="home-grid">
        {TILES.map((tile) => (
          <button
            key={tile.key}
            className="home-tile"
            onClick={() => onJump(tile.key)}
            type="button"
          >
            <div className="home-tile-icon">
              <TileIcon name={tile.key} />
            </div>
            <strong>{tile.label}</strong>
            <span>{tile.description}</span>
          </button>
        ))}
      </section>
    </div>
  );
}

function TileIcon({ name }) {
  if (name === "enroll") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M12 5a3 3 0 1 1 0 6a3 3 0 0 1 0-6Zm0 8c3.3 0 6 1.8 6 4v2H6v-2c0-2.2 2.7-4 6-4Zm7-8v3h3v2h-3v3h-2v-3h-3V8h3V5h2Z" />
      </svg>
    );
  }
  if (name === "recognize") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M7 3H5a2 2 0 0 0-2 2v2h2V5h2V3Zm12 0h-2v2h2v2h2V5a2 2 0 0 0-2-2ZM5 17H3v2a2 2 0 0 0 2 2h2v-2H5v-2Zm16 0h-2v2h-2v2h2a2 2 0 0 0 2-2v-2ZM12 8a4 4 0 1 1 0 8a4 4 0 0 1 0-8Zm0 2a2 2 0 1 0 0 4a2 2 0 0 0 0-4Z" />
      </svg>
    );
  }
  if (name === "workers") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M7 6a2.5 2.5 0 1 1 0 5a2.5 2.5 0 0 1 0-5Zm10 0a2.5 2.5 0 1 1 0 5a2.5 2.5 0 0 1 0-5ZM7 13c2.7 0 5 1.4 5 3v2H2v-2c0-1.6 2.3-3 5-3Zm10 0c2.7 0 5 1.4 5 3v2H12v-2c0-1.6 2.3-3 5-3Z" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M7 4h10a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Zm1 4v2h8V8H8Zm0 4v2h8v-2H8Zm0 4v2h5v-2H8Z" />
    </svg>
  );
}
