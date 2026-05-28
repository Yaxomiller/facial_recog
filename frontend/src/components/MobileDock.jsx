export default function MobileDock({ items, currentView, onChangeView }) {
  return (
    <nav className="mobile-dock" aria-label="Mobile navigation">
      {items.map((item) => (
        <button
          key={item.key}
          className={`mobile-dock-item ${currentView === item.key ? "active" : ""}`}
          onClick={() => onChangeView(item.key)}
        >
          <span>{item.shortLabel || item.label}</span>
        </button>
      ))}
    </nav>
  );
}
