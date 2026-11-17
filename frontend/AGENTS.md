# Frontend instructions

These instructions supplement the repository-level `AGENTS.md` for files under `frontend/`.

## Framework and TypeScript

- Use the existing Next.js App Router structure under `src/app/`, React, and TypeScript. Keep page components, reusable components, and API/types modules in their current roles.
- Prefer typed API models and functions in `src/lib/` rather than duplicating HTTP calls in components.
- Keep credentials and provider configuration on the backend. The browser may use `NEXT_PUBLIC_WORDPILOT_API_URL` to reach the API; never put secret values in `NEXT_PUBLIC_*` variables.

## UI, consistency, and accessibility

- Reuse the existing visual language, CSS variables, buttons, panels, status indicators, and components unless the requested work calls for a deliberate redesign.
- Keep controls keyboard accessible, use semantic HTML and accessible names, retain visible focus styles, and communicate loading, errors, approval, and verification states clearly.
- Do not imply a document was changed, approved, downloaded, or verified unless the backend response supports that state.

## Frontend checks

Run these commands from `frontend/`:

- `npm run typecheck`
- `npm run build`

There is no frontend test script currently declared in `package.json`. Report commands actually run and their results.
