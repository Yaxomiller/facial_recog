export default function Sidebar({ items, currentView, onChangeView, username, onLogout, isOpen, onClose }) {
  return (
    <aside className={`sidebar ${isOpen ? "open" : ""}`}>
      <div className="sidebar-mobile-head">
        <div className="sidebar-section-label">Navigation</div>
        <button className="sidebar-close" onClick={onClose} aria-label="Close navigation">Close</button>
      </div>
      <div className="brand-block">
        <img className="brand-logo" src="/tresenso-logo.png" alt="Tresenso Tech logo" />
        <div className="brand-meta">
          <h1>Tresenso Face Attendance</h1>
          <p>Tresenso Tech Private Limited</p>
        </div>
      </div>

      <div className="sidebar-section-label">Menu</div>
      <nav className="nav-list">
        {items.map((item) => (
          <button
            key={item.key}
            className={`nav-item ${currentView === item.key ? "active" : ""}`}
            onClick={() => onChangeView(item.key)}
          >
            <span className="nav-item-label">{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="sidebar-foot">
        <div className="operator-chip">
          <span className="operator-chip-label">Signed in as</span>
          <strong>{username}</strong>
        </div>
        <button className="button button-dark" onClick={onLogout}>Sign out</button>
      </div>
    </aside>
  );
}
