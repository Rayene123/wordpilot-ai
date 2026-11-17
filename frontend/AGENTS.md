# Frontend instructions

These instructions supplement the repository-level `AGENTS.md` for files under `frontend/`.

## Framework and TypeScript

- Use the existing Next.js App Router structure under `src/app/`, React, and TypeScript. Keep page components, reusable components, and API/types modules in their current roles.
- Prefer typed API models and functions in `src/lib/` rather than duplicating HTTP calls in components.
- Keep components focused on presentation and interaction; keep shared state, API behavior, and data transformation in the existing hooks or library modules. Avoid oversized components and duplicate sources of truth.
- Use narrow types for API states and responses. Handle loading, empty, error, and success states explicitly; do not use `any` to bypass contract mismatches.
- Keep credentials and provider configuration on the backend. The browser may use `NEXT_PUBLIC_WORDPILOT_API_URL` to reach the API; never put secret values in `NEXT_PUBLIC_*` variables.
- Treat API responses and user-entered text as untrusted. Render text through React's normal escaping; do not inject HTML or construct unsafe URLs from unvalidated data.
- Keep server-only functionality and secrets out of client components and browser bundles. Do not add direct document parsing or editing to the frontend.

## UI, consistency, and accessibility

- Reuse the existing visual language, CSS variables, buttons, panels, status indicators, and components unless the requested work calls for a deliberate redesign.
- Keep controls keyboard accessible, use semantic HTML and accessible names, retain visible focus styles, and communicate loading, errors, approval, and verification states clearly.
- Do not imply a document was changed, approved, downloaded, or verified unless the backend response supports that state.
- Preserve keyboard operation and screen-reader context when changing interactions. Associate labels and errors with controls and avoid relying on color alone to communicate state.
- Keep styling local to the existing CSS/module conventions and reuse design tokens before adding new one-off values.

## Frontend checks

Run these commands from `frontend/`:

- `npm run typecheck`
- `npm run build`

There is no frontend test script currently declared in `package.json`. Report commands actually run and their results.
