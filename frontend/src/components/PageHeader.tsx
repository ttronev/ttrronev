export function PageHeader({ title, description }: { title: string; description?: string }) {
  return (
    <header className="mb-6">
      <h1 className="text-2xl font-semibold tracking-tight" data-testid="page-title">
        {title}
      </h1>
      {description && <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{description}</p>}
    </header>
  );
}
