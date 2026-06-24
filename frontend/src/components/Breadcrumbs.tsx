import { ChevronRight } from "lucide-react";
import { Link } from "react-router-dom";

export type BreadcrumbItem = {
  label: string;
  to?: string;
};

export function Breadcrumbs({ items }: { items: BreadcrumbItem[] }) {
  return (
    <nav aria-label="Breadcrumb" className="flex flex-wrap items-center gap-2 text-sm text-slate-500">
      {items.map((item, index) => {
        const isLast = index === items.length - 1;
        return (
          <div key={`${item.label}-${index}`} className="flex items-center gap-2">
            {index > 0 ? <ChevronRight className="h-4 w-4 text-slate-400" /> : null}
            {item.to && !isLast ? (
              <Link className="transition hover:text-ink" to={item.to}>
                {item.label}
              </Link>
            ) : (
              <span className={isLast ? "font-semibold text-ink" : undefined}>{item.label}</span>
            )}
          </div>
        );
      })}
    </nav>
  );
}
