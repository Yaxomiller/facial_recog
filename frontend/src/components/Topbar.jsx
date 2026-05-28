export default function Topbar({ eyebrow, title, subtitle, onRefresh, refreshing, onToggleNav }) {
  return (
    <header className="topbar">
      <div>
        <button className="mobile-nav-toggle" onClick={onToggleNav} aria-label="Open navigation">
          Menu
        </button>
        <div className="eyebrow">{eyebrow}</div>
        <h2>{title}</h2>
        <p>{subtitle}</p>
      </div>
      <button className="button button-ghost" onClick={onRefresh}>
        {refreshing ? "Refreshing..." : "Refresh Data"}
      </button>
    </header>
  );
}
