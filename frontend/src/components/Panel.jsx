export default function Panel({ eyebrow, title, actions = null, children, className = "" }) {
  return (
    <section className={`panel ${className}`.trim()}>
      {(eyebrow || title || actions) ? (
        <div className="panel-header">
          <div>
            {eyebrow ? <div className="eyebrow">{eyebrow}</div> : null}
            {title ? <h3>{title}</h3> : null}
          </div>
          {actions}
        </div>
      ) : null}
      {children}
    </section>
  );
}
