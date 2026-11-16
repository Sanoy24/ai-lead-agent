import { Link } from "react-router";

export function NotFound() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center px-4 text-center">
      <p className="text-sm font-medium text-brand-700">404</p>
      <h1 className="mt-2 text-xl font-semibold tracking-tight">Page not found</h1>
      <Link to="/" className="mt-4 text-sm text-zinc-600 underline hover:text-zinc-900">
        Back to the home page
      </Link>
    </div>
  );
}
